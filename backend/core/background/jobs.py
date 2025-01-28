from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask
import requests
import time
import geopy.distance

from ..models import GPSData, MonthlyStatistic, User
from . import Config
from ..extensions import db



class Job:
    PARAMETERS: dict[str, object] = {}

    def __init__(self):
        """
        Initialize the job with the default configuration.
        """
        self.config = Config
        self.app: Flask = None
        self.start_time = None
        self.progress = 0 # 0-1
        self.running = False
        self.done = False
        self.stop_requested = False

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
                time.sleep(0.1)
                pass








# class QueryPhotonJob(Job):
#     def __init__(self):
#         super().__init__()
#         self.point_ids = []
    
#     def do_request(self, lat, lon):
#         return requests.get(
#             f"{self.config.PHOTON_SERVER_HOST}/reverse?lang=en&lat={lat}&lon={lon}",
#             headers={"X-Api-Key": self.config.PHOTON_SERVER_API_KEY}
#         )
    
#     def do_with_point(self, point_id):
#         with self.app.app_context():
#             point: GPSData = GPSData.query.get(point_id)

#             if not point:
#                 return
            
#             response = self.do_request(point.latitude, point.longitude)
#             data = response.json()
#             if data and "features" in data and data["features"]:
#                 feature = data["features"][0]
#                 point.country = feature["properties"].get("country")
#                 point.city = feature["properties"].get("city")
#                 point.state = feature["properties"].get("state")
#                 point.postal_code = feature["properties"].get("postcode")
#                 point.street = feature["properties"].get("street")
#                 point.street_number = feature["properties"].get("housenumber")
#                 db.session.commit()
    
#     def run(self):
#         total_count = len(self.point_ids)
#         if total_count == 0:
#             self.done = True
#             return

#         with ThreadPoolExecutor(max_workers=int(self.config.BACKGROUND_MAX_THREADS_PER_JOB)) as executor:
#             future_to_point = {executor.submit(self.do_with_point, point_id): point_id for point_id in self.point_ids}
#             completed_count = 0
#             for future in as_completed(future_to_point):
#                 completed_count += 1
#                 self.progress = completed_count / total_count
#                 future.result()  # Raise exceptions if any occurred
        
#         self.done = True

class QueryPhotonJob(Job):
    def __init__(self):
        super().__init__()
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

            point: GPSData = GPSData.query.get(point_id)
            if not point:
                continue

            buffer.append(self.do_with_point(point))
            
            i += 1
            self.progress = i / total_count

            if len(buffer) >= buffer_dump_interval:
                while buffer:
                    point_id, data = buffer.pop(0)
                    if data and "features" in data:
                        feature: dict[str, dict] = data["features"][0]
                        point = GPSData.query.get(point_id)
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
        points = GPSData.query.filter_by(user_id=self.user_id, country=None).all()
        point_ids = [point.id for point in points]
        self.point_ids = point_ids
        super().run()






class GenerateFullStatisticsJob(Job):
    PARAMETERS = {
        "user": User
    }

    def __init__(self, user: User):
        super().__init__()
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
        
        # remove all existing monthly statistics
        MonthlyStatistic.query.filter_by(user_id=user.id).delete()
        
        # Create a new MonthlyStatistic object for each month
        i = 0
        monthly_stats: dict[str, MonthlyStatistic] = {}
        total_count = len(gps_data)
        for data in gps_data:
            if self.stop_requested:
                break

            key = f"{data.timestamp.year}-{data.timestamp.month}"
            if key not in monthly_stats:
                monthly_stats[key] = MonthlyStatistic(
                    user_id=user.id,
                    year=data.timestamp.year,
                    month=data.timestamp.month,

                )
                
            if i > 0:
                prev = gps_data[i - 1]
                distance = self.get_distance(prev, data)
                if monthly_stats[key].total_distance_m is None:
                    monthly_stats[key].total_distance_m = 0.0
                monthly_stats[key].total_distance_m += distance

            if data.country:
                if not monthly_stats[key].visited_countries:
                    monthly_stats[key].visited_countries = []
                monthly_stats[key].visited_countries = list(set(monthly_stats[key].visited_countries + [data.country]))
            if data.city:
                if not monthly_stats[key].visited_cities:
                    monthly_stats[key].visited_cities = []
                monthly_stats[key].visited_cities = list(set(monthly_stats[key].visited_cities + [data.city]))

            i += 1

            self.progress = (i / total_count) * 0.9

        # Save the monthly statistics
        i = 0
        total_count = len(monthly_stats)
        for stat in monthly_stats.values():
            if self.stop_requested:
                break

            db.session.add(stat)
            self.progress = 0.9 + (i / total_count) * 0.1

            if i % 100 == 0:
                db.session.commit()

        db.session.commit()

        self.done = True




JOB_TYPES: dict[str, Job] = {
    "full_stats": GenerateFullStatisticsJob,
}

if len(Config.PHOTON_SERVER_HOST) != 0:
    JOB_TYPES["photon_full"] = PhotonFullJob
    JOB_TYPES["photon_fill"] = PhotonFillJob