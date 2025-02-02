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
    GLOBAl = "global"
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
        self.user_id = user.id

    def run(self):
        points = GPSData.query.filter_by(user_id=self.user_id).all()
        point_ids = [point.id for point in points]
        self.point_ids = point_ids
        super().run()

class PhotonFillJob(QueryPhotonJob):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.user_id = user.id

    def run(self):
        points = GPSData.query.filter_by(user_id=self.user_id, reverse_geocoded=False).all()
        point_ids = [point.id for point in points]
        self.point_ids = point_ids
        super().run()






class GenerateSpeedDataJob(Job):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.user_id = user.id

    def run(self):
        user = User.query.get(self.user_id)
        if not user:
            self.done = True
            return
        
        self.progress = 0.01
        
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp).all()
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
                    db.session.add(data)
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
        self.user_id = user.id

    def get_distance(self, point1: GPSData, point2: GPSData):
        coords1 = (point1.latitude, point1.longitude)
        coords2 = (point2.latitude, point2.longitude)
        return geopy.distance.distance(coords1, coords2).m

    def run(self):
        user = User.query.get(self.user_id)
        if not user:
            self.done = True
            return
        
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp).all()
        if not gps_data:
            self.done = True
            return
        
        DailyStatistic.query.filter_by(user_id=user.id).delete()


        MIN_COUNTRY_COUNT_FOR_STATS = 10
        MIN_CITY_COUNT_FOR_STATS = 100

        
        i = 0
        daily_stats: dict[str, DailyStatistic] = {}
        # daily_stats_country_city_count: dict[str, dict[tuple[str, str], int]] = {}
        daily_stats_country_city_count: dict[str, tuple[dict[str, int], dict[tuple[str, str], int]]] = {}
        total_count = len(gps_data)
        for data in gps_data:
            if self.stop_requested:
                break

            key = f"{data.timestamp.year}-{data.timestamp.month}-{data.timestamp.day}"
            if key not in daily_stats:
                daily_stats[key] = DailyStatistic(
                    user_id=user.id,
                    year=data.timestamp.year,
                    month=data.timestamp.month,
                    day=data.timestamp.day,
                )
                
            if i > 0:
                prev = gps_data[i - 1]
                distance = self.get_distance(prev, data)
                if daily_stats[key].total_distance_m is None:
                    daily_stats[key].total_distance_m = 0.0
                daily_stats[key].total_distance_m += distance
                
            # if data.country:
            #     if not daily_stats[key].visited_countries:
            #         daily_stats[key].visited_countries = []
            #     daily_stats[key].visited_countries = list(set(daily_stats[key].visited_countries + [data.country]))
            # if data.city:
            #     if not daily_stats[key].visited_cities:
            #         daily_stats[key].visited_cities = []
            #     daily_stats[key].visited_cities = list(set(daily_stats[key].visited_cities + [data.city]))

            if key not in daily_stats_country_city_count:
                daily_stats_country_city_count[key] = ({}, {})

            if data.country:
                if data.country not in daily_stats_country_city_count[key][0]:
                    daily_stats_country_city_count[key][0][data.country] = 0
                daily_stats_country_city_count[key][0][data.country] += 1

            if data.city:
                country_city_key = (data.country, data.city)
                if country_city_key not in daily_stats_country_city_count[key][1]:
                    daily_stats_country_city_count[key][1][country_city_key] = 0
                daily_stats_country_city_count[key][1][country_city_key] += 1

            i += 1

            self.progress = (i / total_count) * 0.9

        for stat in daily_stats.values():
            key = f"{stat.year}-{stat.month}-{stat.day}"
            if key in daily_stats_country_city_count:
                country_count, city_count = daily_stats_country_city_count[key]
                if country_count:
                    stat.visited_countries = [country for country, count in country_count.items() if count > MIN_COUNTRY_COUNT_FOR_STATS]
                if city_count:
                    stat.visited_cities = [city for (country, city), count in city_count.items() if count > MIN_CITY_COUNT_FOR_STATS]

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



# class GenerateWeeklyStatisticsJob(Job):
#     PARAMETERS = {
#         "user": User
#     }

#     def __init__(self, user: User):
#         super().__init__()
#         self.concurrency_limit_type = ConcurrencyLimitType.GENERATE_STATS
#         self.user_id = user.id

#     def run(self):
#         user = User.query.get(self.user_id)
#         if not user:
#             self.done = True
#             return
        
#         # regenerate daily stats for past 7 days
#         for i in range(7):
#             date = time.localtime(time.time() - i * 86400)
#             stats = DailyStatistic.query.filter_by(user_id=user.id, year=date.tm_year, month=date.tm_mon, day=date.tm_mday).all()
#             for stat in stats:
#                 db.session.delete(stat)

#             # get all GPS data for the user on that day
#             start = datetime(date.tm_year, date.tm_mon, date.tm_mday, 0, 0, 0)
#             end = datetime(date.tm_year, date.tm_mon, date.tm_mday, 23, 59, 59)
            
#             gps_data: list[GPSData] = GPSData.query.filter_by(user_id=user.id).filter(GPSData.timestamp >= start, GPSData.timestamp <= end).all()
#             if not gps_data:
#                 continue

#             daily_stat = DailyStatistic(
#                 user_id=user.id,
#                 year=date.tm_year,
#                 month=date.tm_mon,
#                 day=date.tm_mday,
#             )

#             i = 0
#             for data in gps_data:
#                 if i > 0:
#                     prev = gps_data[i - 1]
#                     daily_stat.total_distance_m += geopy.distance.distance((prev.latitude, prev.longitude), (data.latitude, data.longitude)).m
#                 i += 1

#             db.session.add(daily_stat)
#             db.session.commit()





class FilterLargeAccuracyJob(Job):
    PARAMETERS = {
        "user": User,
        "maximum_accuracy": float
    }

    def __init__(self, user: User, maximum_accuracy: float):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.GLOBAl
        self.user_id = user.id
        self.maximum_accuracy = maximum_accuracy

    def run(self):
        user = User.query.get(self.user_id)
        if not user:
            self.done = True
            return
        
        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp).all()
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






class ImportJob(Job):
    PARAMETERS = {
        "user": User,
        "import": Import
    }

    def __init__(self, user: User, import_obj: Import):
        super().__init__()
        self.user_id = user.id
        self.import_obj = import_obj

    def run(self):
        user = User.query.get(self.user_id)
        if not user:
            self.done = True
            return

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
                user_id=user.id,
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

        self.done = True





class DeleteDuplicatesJob(Job):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
        self.concurrency_limit_type = ConcurrencyLimitType.GLOBAl
        self.user_id = user.id

    def run(self):
        user = User.query.get(self.user_id)
        if not user:
            self.done = True
            return

        # Get all GPS data for the user sorted by timestamp
        gps_data: list[GPSData] = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp).all()
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
    "delete_duplicates": DeleteDuplicatesJob,
}

if len(Config.PHOTON_SERVER_HOST) != 0:
    JOB_TYPES["photon_full"] = PhotonFullJob
    JOB_TYPES["photon_fill"] = PhotonFillJob