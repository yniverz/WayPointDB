from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
import os
from threading import Thread
import time
from typing import Iterator, Optional
import uuid

from flask import Flask
import requests
from sqlalchemy import delete, func, insert, select, text, update

from ..models import AdditionalTrace, DailyStatistic, GPSData, Import, User
from . import Config
from ..extensions import db


EARTH_RADIUS_M = 6371000.0

# Great-circle distance in meters between (lat1, lon1) and (lat2, lon2), as SQL.
SQL_DISTANCE = f"""
    2 * {EARTH_RADIUS_M} * ASIN(LEAST(1, SQRT(
        POWER(SIN(RADIANS({{lat2}} - {{lat1}}) / 2), 2)
        + COS(RADIANS({{lat1}})) * COS(RADIANS({{lat2}})) * POWER(SIN(RADIANS({{lon2}} - {{lon1}}) / 2), 2)
    )))
"""

DELETE_CHUNK_SIZE = 10000


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


class Lane:
    """
    Jobs of the same user in the same lane run one after another. Geocoding only
    touches the geocoding columns, so it can run next to the other jobs.
    """
    DATA = "data"
    GEOCODE = "geocode"


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCancelled(Exception):
    pass


class Job:
    PARAMETERS: dict[str, object] = {}
    LABEL = "Job"
    LANE = Lane.DATA
    # At most this many jobs of this class run at once across all users (None: no limit).
    GLOBAL_LIMIT: Optional[int] = None

    def __init__(self, user: Optional[User] = None):
        self.config = Config
        self.app: Flask = None
        self.id = uuid.uuid4().hex
        # Keep plain values: the user object belongs to the session of the request that created the job.
        self.user_id = user.id if user else None
        self.user_email = user.email if user else None
        self.thread: Thread = None
        self.status = JobStatus.QUEUED
        self.progress = 0.0  # 0-1
        self.message: Optional[str] = None
        self.error: Optional[str] = None
        self.created_time = time.time()
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.stop_requested = False

    @property
    def done(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)

    def set_config(self, config: Config):
        self.config = config

    def set_web_app(self, app: Flask):
        self.app = app

    def run(self):
        """Run the job. Overridden by subclasses; raise to fail the job."""
        pass

    def stop(self):
        self.stop_requested = True

    def check_stop(self):
        if self.stop_requested:
            raise JobCancelled()

    def series(self) -> list[tuple[str, object]]:
        """
        The point series this job works on: the user's main trace and every trace
        they own, as (column name, id) pairs. Each series is a separate timeline.
        """
        series = [("user_id", self.user_id)]
        trace_ids = db.session.execute(
            select(AdditionalTrace.id).where(AdditionalTrace.owner_id == self.user_id)
        ).scalars().all()
        series += [("trace_id", trace_id) for trace_id in trace_ids]
        return series

    def user_points_filter(self):
        """SQL filter for all points of the user, including traces they own."""
        owned_traces = select(AdditionalTrace.id).where(AdditionalTrace.owner_id == self.user_id)
        return (GPSData.user_id == self.user_id) | GPSData.trace_id.in_(owned_traces)

    def stream_series(self, column: str, series_id, *columns) -> Iterator:
        """Stream the given columns of one series in time order, without loading it all at once."""
        stmt = (
            select(*columns)
            .where(getattr(GPSData, column) == series_id)
            .order_by(GPSData.timestamp, GPSData.id)
            .execution_options(yield_per=20000)
        )
        return db.session.execute(stmt)

    def delete_points(self, point_ids: list[int]) -> int:
        for start in range(0, len(point_ids), DELETE_CHUNK_SIZE):
            db.session.execute(delete(GPSData).where(GPSData.id.in_(point_ids[start:start + DELETE_CHUNK_SIZE])))
            db.session.commit()
        return len(point_ids)




class PhotonError(Exception):
    pass


class QueryPhotonJob(Job):
    """Reverse geocodes points with the configured Photon server."""
    LANE = Lane.GEOCODE
    GLOBAL_LIMIT = 1

    BATCH_SIZE = 500
    # Points are looked up at about 11 m precision, so a stationary cluster costs one request.
    COORDINATE_DECIMALS = 4
    MAX_CACHE_SIZE = 200000

    def points_filter(self):
        raise NotImplementedError

    def reverse_geocode(self, session: requests.Session, lat: float, lon: float) -> dict:
        scheme = "https" if self.config.PHOTON_SERVER_HTTPS else "http"
        try:
            response = session.get(
                f"{scheme}://{self.config.PHOTON_SERVER_HOST}/reverse",
                params={"lang": "en", "lat": lat, "lon": lon},
                timeout=(5, 30),
            )
        except requests.RequestException as e:
            raise PhotonError(f"Could not reach the Photon server: {e}") from e

        if response.status_code != 200:
            raise PhotonError(f"The Photon server answered with HTTP {response.status_code}")

        try:
            features = response.json().get("features") or []
        except ValueError as e:
            raise PhotonError("The Photon server did not return JSON") from e

        properties = features[0].get("properties", {}) if features else {}
        return {
            "reverse_geocoded": True,
            "country": properties.get("country"),
            "city": properties.get("city"),
            "state": properties.get("state"),
            "postal_code": properties.get("postcode"),
            "street": properties.get("street"),
            "street_number": properties.get("housenumber"),
        }

    def run(self):
        points_filter = self.points_filter()
        total = db.session.execute(select(func.count()).select_from(GPSData).where(points_filter)).scalar()
        if not total:
            self.message = "No points to geocode"
            return

        session = requests.Session()
        if self.config.PHOTON_SERVER_API_KEY:
            session.headers["X-Api-Key"] = self.config.PHOTON_SERVER_API_KEY

        cache: dict[tuple[float, float], dict] = {}
        last_id = 0
        processed = 0
        failed_points = 0

        with ThreadPoolExecutor(max_workers=self.config.PHOTON_SERVER_CONCURRENCY) as pool:
            while True:
                self.check_stop()

                # Keyset pagination, so points that fail to geocode are not fetched again.
                rows = db.session.execute(
                    select(GPSData.id, GPSData.latitude, GPSData.longitude)
                    .where(points_filter, GPSData.id > last_id)
                    .order_by(GPSData.id)
                    .limit(self.BATCH_SIZE)
                ).all()
                if not rows:
                    break
                last_id = rows[-1].id

                if len(cache) > self.MAX_CACHE_SIZE:
                    cache.clear()

                def key(row):
                    return (round(row.latitude, self.COORDINATE_DECIMALS), round(row.longitude, self.COORDINATE_DECIMALS))

                missing = {key(row) for row in rows} - cache.keys()
                futures = {pool.submit(self.reverse_geocode, session, lat, lon): (lat, lon) for lat, lon in missing}
                last_error = None
                for future in as_completed(futures):
                    try:
                        cache[futures[future]] = future.result()
                    except PhotonError as e:
                        last_error = e

                updates = [{"id": row.id, **cache[key(row)]} for row in rows if key(row) in cache]
                if not updates and last_error:
                    # Not a single lookup worked, so the server is down or rejecting us.
                    raise last_error

                if updates:
                    db.session.execute(update(GPSData), updates)
                    db.session.commit()

                failed_points += len(rows) - len(updates)
                processed += len(rows)
                self.progress = min(1.0, processed / total)

        if failed_points:
            self.message = f"{failed_points} points could not be geocoded"


class PhotonFullJob(QueryPhotonJob):
    LABEL = "Geocode all points"
    PARAMETERS = {
        "user": User
    }

    def points_filter(self):
        return self.user_points_filter()


class PhotonFillJob(QueryPhotonJob):
    LABEL = "Geocode new points"
    PARAMETERS = {
        "user": User
    }

    def points_filter(self):
        return self.user_points_filter() & GPSData.reverse_geocoded.isnot(True)




class ResetPointsWithNoGeocodingJob(Job):
    LABEL = "Reset points without geocoding"
    PARAMETERS = {
        "user": User
    }

    def run(self):
        result = db.session.execute(
            update(GPSData)
            .where(self.user_points_filter(), GPSData.reverse_geocoded.is_(True), GPSData.country.is_(None))
            .values(reverse_geocoded=False)
        )
        db.session.commit()
        self.message = f"Reset {result.rowcount} points"




class GenerateSpeedDataJob(Job):
    LABEL = "Generate speed data"
    PARAMETERS = {
        "user": User
    }

    # Fills in the speed of points that have none (NULL, or negative as reported by
    # iOS for "unknown"), from the distance and time to the previous point.
    SQL = """
        UPDATE gps_data AS g
        SET speed = s.speed
        FROM (
            SELECT id, {distance} / EXTRACT(EPOCH FROM timestamp - prev_ts) AS speed
            FROM (
                SELECT id, speed, timestamp, latitude, longitude,
                       LAG(timestamp) OVER w AS prev_ts,
                       LAG(latitude) OVER w AS prev_lat,
                       LAG(longitude) OVER w AS prev_lon
                FROM gps_data
                WHERE {column} = :series_id
                WINDOW w AS (ORDER BY timestamp, id)
            ) AS p
            WHERE (speed IS NULL OR speed < 0) AND prev_ts IS NOT NULL AND timestamp > prev_ts
        ) AS s
        WHERE g.id = s.id
    """

    def run(self):
        distance = SQL_DISTANCE.format(lat1="prev_lat", lon1="prev_lon", lat2="latitude", lon2="longitude")
        series = self.series()
        updated = 0
        for i, (column, series_id) in enumerate(series):
            self.check_stop()
            result = db.session.execute(text(self.SQL.format(distance=distance, column=column)), {"series_id": series_id})
            db.session.commit()
            updated += result.rowcount
            self.progress = (i + 1) / len(series)

        self.message = f"Added speed to {updated} points"




class GenerateFullStatisticsJob(Job):
    LABEL = "Generate statistics"
    PARAMETERS = {
        "user": User
    }

    # Per day of one series: the distance travelled, and the time spent in each
    # country and city, counted between consecutive points in the same place.
    SQL = """
        WITH pts AS (
            SELECT timestamp::date AS day, timestamp, latitude, longitude, country, city,
                   LAG(timestamp) OVER w AS prev_ts,
                   LAG(latitude) OVER w AS prev_lat,
                   LAG(longitude) OVER w AS prev_lon,
                   LAG(country) OVER w AS prev_country,
                   LAG(city) OVER w AS prev_city
            FROM gps_data
            WHERE {column} = :series_id
            WINDOW w AS (ORDER BY timestamp, id)
        )
        SELECT 'distance' AS kind, day, NULL::text AS name, NULL::text AS country,
               COALESCE(SUM(CASE WHEN prev_ts IS NOT NULL THEN {distance} END), 0) AS value
        FROM pts
        GROUP BY day
        UNION ALL
        SELECT 'country', day, prev_country, NULL, SUM(EXTRACT(EPOCH FROM timestamp - prev_ts))
        FROM pts
        WHERE prev_country IS NOT NULL AND prev_country = country
        GROUP BY day, prev_country
        HAVING SUM(EXTRACT(EPOCH FROM timestamp - prev_ts)) > :min_country_duration
        UNION ALL
        SELECT 'city', day, prev_city, prev_country, SUM(EXTRACT(EPOCH FROM timestamp - prev_ts))
        FROM pts
        WHERE prev_city IS NOT NULL AND prev_city = city
        GROUP BY day, prev_city, prev_country
        HAVING SUM(EXTRACT(EPOCH FROM timestamp - prev_ts)) > :min_city_duration
    """

    def run(self):
        distance = SQL_DISTANCE.format(lat1="prev_lat", lon1="prev_lon", lat2="latitude", lon2="longitude")
        series = self.series()

        for i, (column, series_id) in enumerate(series):
            self.check_stop()

            rows = db.session.execute(text(self.SQL.format(distance=distance, column=column)), {
                "series_id": series_id,
                "min_country_duration": self.config.MIN_COUNTRY_VISIT_DURATION_FOR_STATS,
                "min_city_duration": self.config.MIN_CITY_VISIT_DURATION_FOR_STATS,
            }).all()

            days: dict = {}
            for kind, day, name, country, value in rows:
                stat = days.setdefault(day, {
                    column: series_id,
                    "year": day.year,
                    "month": day.month,
                    "day": day.day,
                    "total_distance_m": 0.0,
                    "visited_countries": [],
                    "visited_cities": [],
                })
                if kind == "distance":
                    stat["total_distance_m"] = float(value)
                elif kind == "country":
                    stat["visited_countries"].append(name)
                else:
                    stat["visited_cities"].append([name, country])

            # Replace the series' statistics in one transaction, so the stats page never sees them half done.
            db.session.execute(delete(DailyStatistic).where(getattr(DailyStatistic, column) == series_id))
            if days:
                db.session.execute(insert(DailyStatistic), list(days.values()))
            db.session.commit()

            self.progress = (i + 1) / len(series)




class FilterLargeAccuracyJob(Job):
    LABEL = "Filter large accuracy"
    PARAMETERS = {
        "user": User,
        "maximum_accuracy": float
    }

    def __init__(self, user: User, maximum_accuracy: float):
        super().__init__(user)
        self.maximum_accuracy = maximum_accuracy

    def run(self):
        result = db.session.execute(
            delete(GPSData).where(self.user_points_filter(), GPSData.horizontal_accuracy > self.maximum_accuracy)
        )
        db.session.commit()
        self.message = f"Deleted {result.rowcount} points"



class FilterLargeSpeedJob(Job):
    LABEL = "Filter large speed"
    PARAMETERS = {
        "user": User,
        "maximum_speed_kmh": float
    }

    # Beyond this gap between points, a jump is treated as travel with tracking off, not an outlier.
    MAX_GAP_SECONDS = 60 * 60
    # This many consecutive rejected points that agree with each other mean the
    # last accepted point was the outlier, not them.
    REANCHOR_AFTER = 5

    def __init__(self, user: User, maximum_speed_kmh: float):
        super().__init__(user)
        self.maximum_speed = maximum_speed_kmh / 3.6

    def too_fast(self, a, b) -> bool:
        seconds = (b.timestamp - a.timestamp).total_seconds()
        if seconds > self.MAX_GAP_SECONDS:
            return False
        return distance_m(a.latitude, a.longitude, b.latitude, b.longitude) / max(seconds, 1.0) > self.maximum_speed

    def outliers(self, points) -> list[int]:
        """
        Points that could only be reached from the last plausible point by moving
        faster than the maximum speed. Comparing against the last plausible point
        instead of the previous point means a single GPS jump removes just the
        jumped point, not also the correct point after it.
        """
        to_delete = []
        anchor = None
        rejected = []
        for point in points:
            if anchor is None or not self.too_fast(anchor, point):
                to_delete += [p.id for p in rejected]
                rejected = []
                anchor = point
                continue

            rejected.append(point)
            if len(rejected) >= self.REANCHOR_AFTER and not any(self.too_fast(a, b) for a, b in zip(rejected, rejected[1:])):
                to_delete.append(anchor.id)
                anchor = rejected[-1]
                rejected = []

        return to_delete + [p.id for p in rejected]

    def run(self):
        series = self.series()
        deleted = 0
        for i, (column, series_id) in enumerate(series):
            self.check_stop()
            points = self.stream_series(column, series_id, GPSData.id, GPSData.timestamp, GPSData.latitude, GPSData.longitude)
            deleted += self.delete_points(self.outliers(points))
            self.progress = (i + 1) / len(series)

        self.message = f"Deleted {deleted} points"




class FilterClustersJob(Job):
    LABEL = "Reduce clusters"
    PARAMETERS = {
        "user": User,
        "maximum_distance": float
    }

    def __init__(self, user: User, maximum_distance: float):
        super().__init__(user)
        self.maximum_distance = maximum_distance

    def redundant(self, points) -> list[int]:
        """
        While staying within the distance of where a stay started, only the first
        and last point of the stay are kept, so the time spent there is preserved.
        """
        to_delete = []
        anchor = None
        pending = None
        for point in points:
            if anchor is not None and distance_m(anchor.latitude, anchor.longitude, point.latitude, point.longitude) < self.maximum_distance:
                if pending is not None:
                    to_delete.append(pending.id)
                pending = point
            else:
                anchor = point
                pending = None
        return to_delete

    def run(self):
        series = self.series()
        deleted = 0
        for i, (column, series_id) in enumerate(series):
            self.check_stop()
            points = self.stream_series(column, series_id, GPSData.id, GPSData.latitude, GPSData.longitude)
            deleted += self.delete_points(self.redundant(points))
            self.progress = (i + 1) / len(series)

        self.message = f"Deleted {deleted} points"





class ImportJob(Job):
    LABEL = "Import"
    PARAMETERS = {
        "user": User,
        "import": Import,
        "trace": Optional[AdditionalTrace]
    }

    BATCH_SIZE = 5000

    def __init__(self, user: User, import_obj: Import, trace: Optional[AdditionalTrace] = None):
        super().__init__(user)
        self.import_id = import_obj.id
        self.filename = import_obj.filename
        self.trace_id = trace.id if trace else None
        self.LABEL = f"Import {import_obj.original_filename or import_obj.filename}"

    @staticmethod
    def parse_timestamp(value) -> datetime:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
        ts = datetime.fromisoformat(value)
        if ts.tzinfo is not None:
            # Stored as naive UTC, like the API stores points.
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts

    def run(self):
        with open(os.path.join(self.config.UPLOAD_FOLDER, self.filename), "r") as f:
            entries = json.load(f)

        if not isinstance(entries, list):
            raise ValueError("The import file must contain a JSON list of points")

        owner = {"trace_id": self.trace_id} if self.trace_id else {"user_id": self.user_id}

        # Points of earlier runs of this import, so running it again only adds what is missing.
        existing = set(db.session.execute(
            select(GPSData.timestamp, GPSData.latitude, GPSData.longitude).where(GPSData.import_id == self.import_id)
        ).tuples())

        batch = []
        added = 0
        invalid = 0
        for i, entry in enumerate(entries):
            if i % 1000 == 0:
                self.check_stop()
                self.progress = i / len(entries)

            try:
                ts = self.parse_timestamp(entry["timestamp"])
                lat = float(entry["latitude"])
                lon = float(entry["longitude"])
            except (KeyError, TypeError, ValueError):
                invalid += 1
                continue

            if (ts, lat, lon) in existing:
                continue
            existing.add((ts, lat, lon))

            batch.append({
                **owner,
                "import_id": self.import_id,
                "timestamp": ts,
                "latitude": lat,
                "longitude": lon,
                "horizontal_accuracy": entry.get("horizontal_accuracy"),
                "altitude": entry.get("altitude"),
                "vertical_accuracy": entry.get("vertical_accuracy"),
                "heading": entry.get("heading"),
                "heading_accuracy": entry.get("heading_accuracy"),
                "speed": entry.get("speed"),
                "speed_accuracy": entry.get("speed_accuracy"),
                "reverse_geocoded": False,
            })

            if len(batch) >= self.BATCH_SIZE:
                db.session.execute(insert(GPSData), batch)
                db.session.commit()
                added += len(batch)
                batch = []

        if batch:
            db.session.execute(insert(GPSData), batch)
            added += len(batch)

        import_obj = db.session.get(Import, self.import_id)
        if import_obj:
            import_obj.done_importing = True
        db.session.commit()

        self.message = f"Added {added} points"
        if invalid:
            self.message += f", skipped {invalid} invalid entries"





class DeleteDuplicatesJob(Job):
    LABEL = "Delete duplicates"
    PARAMETERS = {
        "user": User
    }

    # Keeps the oldest of each set of points with the same time and position in a series.
    SQL = """
        DELETE FROM gps_data
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY timestamp, latitude, longitude ORDER BY id) AS n
                FROM gps_data
                WHERE {column} = :series_id
            ) AS d
            WHERE n > 1
        )
    """

    def run(self):
        series = self.series()
        deleted = 0
        for i, (column, series_id) in enumerate(series):
            self.check_stop()
            result = db.session.execute(text(self.SQL.format(column=column)), {"series_id": series_id})
            db.session.commit()
            deleted += result.rowcount
            self.progress = (i + 1) / len(series)

        self.message = f"Deleted {deleted} points"








JOB_TYPES: dict[str, type[Job]] = {
    "full_stats": GenerateFullStatisticsJob,
    "speed_data": GenerateSpeedDataJob,
    "filter_accuracy": FilterLargeAccuracyJob,
    "filter_speed": FilterLargeSpeedJob,
    "filter_clusters": FilterClustersJob,
    "delete_duplicates": DeleteDuplicatesJob,
    "reset_no_geocoding": ResetPointsWithNoGeocodingJob,
}

if len(Config.PHOTON_SERVER_HOST) != 0:
    JOB_TYPES["photon_full"] = PhotonFullJob
    JOB_TYPES["photon_fill"] = PhotonFillJob
