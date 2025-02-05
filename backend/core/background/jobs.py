from datetime import datetime
import json
from threading import Thread
import traceback
import uuid
from flask import Flask
import requests
import time
import geopy.distance

from ..models import DailyStatistic, GPSData, Import, User
from . import Config
from ..extensions import db



class ConcurrencyLimitType:
    GLOBAl = None
    PHOTON = "photon"
    GENERATE_STATS = "generate_stats"


class Job:
    PARAMETERS: dict[str, object] = {}

    def __init__(self):
        """
        Initialize the job with the default configuration.
        """
        self.config = Config
        self.app: Flask = None
        self.user: User = None
        self.thread: Thread = None
        self.start_time = None
        self.progress = 0 # 0-1
        self.running = False
        self.done = False
        self.stop_requested = False
        self.id = uuid.uuid4().hex
        self.concurrency_limit_type: ConcurrencyLimitType = None

    def set_config(self, config: Config):
        self.config = config

    def set_web_app(self, app: Flask):
        self.app = app

    def run(self):
        """
        This method should be overridden by the subclass.
        Run the main job loop here and return when the job is done.
        """
        pass

    def stop(self, blocking=False):
        self.stop_requested = True

        if blocking:
            while not self.done:
                print("Waiting for job to stop...")
                time.sleep(0.1)
                pass




class QueryPhotonJob(Job):
    def __init__(self):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.PHOTON
        self.point_ids = []
    
    def do_request(self, lat, lon):

        url = f"http{'s' if self.config.PHOTON_SERVER_HTTPS else ''}://{self.config.PHOTON_SERVER_HOST}/reverse?lang=en&lat={lat}&lon={lon}"
        print(url)
        res = requests.get(
            url,
            headers={"X-Api-Key": self.config.PHOTON_SERVER_API_KEY}
        )

        print(res)
        return res
    
    
    def do_with_point(self, point: GPSData):
        response = self.do_request(point.latitude, point.longitude)

        data = response.json()

        return (point.id, data)

    def run(self):
        buffer_dump_interval = 100

        total_count = len(self.point_ids)
        i = 0
        buffer: list[tuple[str, dict]] = []
        for point_id in self.point_ids:
            if self.stop_requested:
                break
            
            i += 1
            self.progress = i / total_count

            point: GPSData = GPSData.query.get(point_id)
            if not point:
                continue

            try:
                buffer.append(self.do_with_point(point))
            except requests.exceptions.RequestException as e:
                print(traceback.format_exc())
                continue

            if len(buffer) >= buffer_dump_interval:
                while buffer:
                    point_id, data = buffer.pop(0)
                    if data and "features" in data:
                        point = GPSData.query.get(point_id)
                        if len(data["features"]) == 0:
                            point.reverse_geocoded = True
                            continue

                        feature: dict[str, dict] = data["features"][0]
                        point.reverse_geocoded = True
                        point.country = feature["properties"].get("country")
                        point.city = feature["properties"].get("city")
                        point.state = feature["properties"].get("state")
                        point.postal_code = feature["properties"].get("postcode")
                        point.street = feature["properties"].get("street")
                        point.street_number = feature["properties"].get("housenumber")
                        
                db.session.commit()

        self.done = True



class PhotonFullJob(QueryPhotonJob):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.user = user

    def run(self):
        points = GPSData.query.filter_by(user_id=self.user.id).all()
        point_ids = [point.id for point in points]
        self.point_ids = point_ids
        super().run()

class PhotonFillJob(QueryPhotonJob):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.user = user

    def run(self):
        points = GPSData.query.filter_by(user_id=self.user.id, reverse_geocoded=False).all()
        point_ids = [point.id for point in points]
        self.point_ids = point_ids
        super().run()




class ResetPointsWithNoGeocodingJob(Job):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.user = user

    def run(self):
        points: list[GPSData] = GPSData.query.filter_by(user_id=self.user.id, reverse_geocoded=True, country=None).all()

        i = 0
        total_count = len(points)
        for point in points:
            if self.stop_requested:
                break

            point.reverse_geocoded = False

            i += 1
            self.progress = i / total_count

            if i % 1000 == 0:
                db.session.commit()

        db.session.commit()

        self.done = True




class GenerateSpeedDataJob(Job):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.user = user

    def run(self):
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=self.user.id).order_by(GPSData.timestamp).all()
        if not gps_data:
            self.done = True
            return
        
        i = 0
        total_count = len(gps_data)
        added = 1
        for data in gps_data:
            if self.stop_requested:
                break

            if data.speed is not None and data.speed > 0:
                i += 1
                self.progress = (i / total_count)
                continue

            if i > 0:
                prev = gps_data[i - 1]
                distance = geopy.distance.distance((prev.latitude, prev.longitude), (data.latitude, data.longitude)).m
                time_diff = (data.timestamp - prev.timestamp).total_seconds()
                if time_diff > 0:
                    data.speed = distance / time_diff
                    added += 1

            if added % 1000 == 0:
                db.session.commit()
                added = 1

            i += 1
            self.progress = (i / total_count)

        db.session.commit()

        self.done = True




class GenerateFullStatisticsJob(Job):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.GENERATE_STATS
        self.user = user

    def get_distance(self, point1: GPSData, point2: GPSData):
        coords1 = (point1.latitude, point1.longitude)
        coords2 = (point2.latitude, point2.longitude)
        # return geopy.distance.distance(coords1, coords2).m # most accurate
        return geopy.distance.great_circle(coords1, coords2).m # about 20 times faster

    def run(self):
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=self.user.id).order_by(GPSData.timestamp).all()
        if not gps_data:
            self.done = True
            return
        
        DailyStatistic.query.filter_by(user_id=self.user.id).delete()

        
        i = 0
        daily_stats: dict[str, DailyStatistic] = {}
        # daily_stats_country_city_count: dict[str, dict[tuple[str, str], int]] = {}
        daily_stats_country_city_count: dict[str, tuple[dict[str, int], dict[tuple[str, str], int]]] = {}
        total_count = len(gps_data)
        last_point: GPSData = None
        for point in gps_data:
            if self.stop_requested:
                break

            key = f"{point.timestamp.year}-{point.timestamp.month}-{point.timestamp.day}"
            if key not in daily_stats:
                daily_stats[key] = DailyStatistic(
                    user_id=self.user.id,
                    year=point.timestamp.year,
                    month=point.timestamp.month,
                    day=point.timestamp.day,
                )
                
            if i > 0:
                prev = gps_data[i - 1]
                distance = self.get_distance(prev, point)
                if daily_stats[key].total_distance_m is None:
                    daily_stats[key].total_distance_m = 0.0
                daily_stats[key].total_distance_m += distance
                
            if key not in daily_stats_country_city_count:
                daily_stats_country_city_count[key] = ({}, {})

            if last_point and last_point.country and last_point.country == point.country:
                duration = (point.timestamp - last_point.timestamp).total_seconds()

                if last_point.country not in daily_stats_country_city_count[key][0]:
                    daily_stats_country_city_count[key][0][last_point.country] = 0

                daily_stats_country_city_count[key][0][last_point.country] += duration

            if last_point and last_point.city and last_point.city == point.city:
                city_country_key = (last_point.city, last_point.country)
                duration = (point.timestamp - last_point.timestamp).total_seconds()
                
                if city_country_key not in daily_stats_country_city_count[key][1]:
                    daily_stats_country_city_count[key][1][city_country_key] = 0

                daily_stats_country_city_count[key][1][city_country_key] += duration

            last_point = point

            i += 1
            self.progress = (i / total_count) * 0.9

        i = 0
        total_count = len(daily_stats)
        for stat in daily_stats.values():
            if self.stop_requested:
                break

            self.progress = 0.8 + (i / total_count) * 0.1

            key = f"{stat.year}-{stat.month}-{stat.day}"
            if key in daily_stats_country_city_count:
                country_count, city_count = daily_stats_country_city_count[key]
                if country_count:
                    stat.visited_countries = [country for country, duration in country_count.items() if duration > self.config.MIN_COUNTRY_VISIT_DURATION_FOR_STATS]
                if city_count:
                    stat.visited_cities = [(city, country) for (city, country), duration in city_count.items() if duration > self.config.MIN_CITY_VISIT_DURATION_FOR_STATS]

        i = 0
        total_count = len(daily_stats)
        for stat in daily_stats.values():
            if self.stop_requested:
                break

            db.session.add(stat)
            self.progress = 0.9 + (i / total_count) * 0.1

            if i % 500 == 0:
                db.session.commit()

            i += 1

        print("COMMITTING", i)

        db.session.commit()

        self.done = True




class FilterLargeAccuracyJob(Job):
    PARAMETERS = {
        "user": User,
        "maximum_accuracy": float
    }

    def __init__(self, user: User, maximum_accuracy: float):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.GLOBAl
        self.user = user
        self.maximum_accuracy = maximum_accuracy

    def run(self):
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=self.user.id).order_by(GPSData.timestamp).all()
        if not gps_data:
            self.done = True
            return
        
        i = 0
        total_count = len(gps_data)
        deleted = 1
        for data in gps_data:
            if self.stop_requested:
                break

            if data.horizontal_accuracy is not None and data.horizontal_accuracy > self.maximum_accuracy:
                db.session.delete(data)
                deleted += 1

            i += 1
            self.progress = (i / total_count)

            if deleted % 1000 == 0:
                db.session.commit()
                deleted = 1

        db.session.commit()

        self.done = True



class FilterLargeSpeedJob(Job):
    PARAMETERS = {
        "user": User,
        "maximum_speed_kmh": float
    }

    def __init__(self, user: User, maximum_speed_kmh: float):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.GLOBAl
        self.user = user
        self.maximum_speed = maximum_speed_kmh / 3.6

    def run(self):
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=self.user.id).order_by(GPSData.timestamp).all()
        if not gps_data:
            self.done = True
            return
        
        i = 0
        total_count = len(gps_data)
        deleted = 1
        delete_buffer = []
        for data in gps_data:
            if self.stop_requested:
                break

            if data.speed is not None and data.speed > self.maximum_speed:
                delete_buffer.append(data)
            else:
                while delete_buffer:
                    db.session.delete(delete_buffer.pop(0))
                    deleted += 1

            i += 1
            self.progress = (i / total_count)

            if deleted % 1000 == 0:
                db.session.commit()
                deleted = 1

        db.session.commit()

        self.done = True




class FilterClustersJob(Job):
    PARAMETERS = {
        "user": User,
        "maximum_distance": float
    }

    def __init__(self, user: User, maximum_distance: float):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.GLOBAl
        self.user = user
        self.maximum_distance = maximum_distance

    def run(self):
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=self.user.id).order_by(GPSData.timestamp).all()
        if not gps_data:
            self.done = True
            return
        
        i = 0
        total_count = len(gps_data)
        deleted = 1
        for data in gps_data:
            if self.stop_requested:
                break

            if i > 0:
                prev = gps_data[i - 1]
                distance = geopy.distance.great_circle((prev.latitude, prev.longitude), (data.latitude, data.longitude)).m
                if distance < self.maximum_distance:
                    db.session.delete(prev)
                    deleted += 1

            i += 1
            self.progress = (i / total_count)

            if deleted % 1000 == 0:
                db.session.commit()
                deleted = 1

        db.session.commit()

        self.done = True





class ImportJob(Job):
    PARAMETERS = {
        "user": User,
        "import": Import
    }

    def __init__(self, user: User, import_obj: Import):
        super().__init__()
        self.user = user
        self.import_obj = import_obj

    def run(self):
        # Read the file and parse the GPS data
        with open(Config.UPLOAD_FOLDER + "/" + self.import_obj.filename, "r") as f:
            data = f.read()

        json_data = json.loads(data)
        if not json_data:
            self.done = True
            return

        # Load existing GPS data in memory for fast lookup
        existing_points = set(
            (r.timestamp, r.latitude, r.longitude)
            # for r in GPSData.query.filter(GPSData.import_id == self.import_obj.id).all()
            for r in GPSData.query.filter_by(import_id=self.import_obj.id).all()
        )

        i = 0
        new_records = []  # Store new records in batch
        for entry in json_data:
            ts_str = entry.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_str)  # Parse timestamp as ISO 8601
            except:
                ts = datetime.now()  # Fallback to current timestamp

            lat = entry["latitude"]
            lon = entry["longitude"]

            # Skip if record already exists
            if (ts, lat, lon) in existing_points:
                continue

            gps_record = GPSData(
                user_id=self.user.id,
                import_id=self.import_obj.id,
                timestamp=ts,
                latitude=lat,
                longitude=lon,
                horizontal_accuracy=entry.get("horizontal_accuracy"),
                altitude=entry.get("altitude"),
                vertical_accuracy=entry.get("vertical_accuracy"),
                heading=entry.get("heading"),
                heading_accuracy=entry.get("heading_accuracy"),
                speed=entry.get("speed"),
                speed_accuracy=entry.get("speed_accuracy"),
            )

            new_records.append(gps_record)

            # Batch insert every 1000 records
            if len(new_records) >= 1000:
                db.session.bulk_save_objects(new_records)
                db.session.commit()
                new_records = []  # Clear batch after commit

            i += 1
            self.progress = i / len(json_data)

        # Final commit for remaining records
        if new_records:
            db.session.bulk_save_objects(new_records)
            db.session.commit()
        
        self.import_obj.done_importing = True

        self.done = True





class DeleteDuplicatesJob(Job):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.GLOBAl
        self.user = user

    def run(self):
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=self.user.id).order_by(GPSData.timestamp).all()
        if not gps_data:
            self.done = True
            return

        i = 0
        total_count = len(gps_data)
        deleted = 1
        for data in gps_data:
            if self.stop_requested:
                break

            if i > 0:
                prev = gps_data[i - 1]
                if prev.timestamp == data.timestamp and prev.latitude == data.latitude and prev.longitude == data.longitude:
                    db.session.delete(prev)
                    deleted += 1

            i += 1
            self.progress = (i / total_count)

            if deleted % 1000 == 0:
                deleted = 1
                db.session.commit()

        db.session.commit()

        self.done = True








JOB_TYPES: dict[str, Job] = {
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