import requests
import time
import geopy.distance

from ..models import GPSData, MonthlyStatistic, User
from . import Config
from ..extensions import db


class Job:
    def __init__(self):
        self.config = Config
        self.running = False
        self.done = False
        self.stop_requested = False

    def set_config(self, config: Config):
        self.config = config

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




class QueryPhotonJob(Job):
    def __init__(self, point_ids: list[int]):
        super().__init__()
        self.point_ids = point_ids
    
    def do_request(self, lat, lon):
        return requests.get(
            f"{self.config.PHOTON_SERVER_HOST}/reverse?lang=en&lat={lat}&lon={lon}",
            headers={"X-Api-Key": self.config.PHOTON_SERVER_API_KEY}
        )

    def run(self):
        for point_id in self.point_ids:
            point: GPSData = GPSData.query.get(point_id)
            if not point:
                continue

            response = self.do_request(point.latitude, point.longitude)

            class nil:
                pass
                # {
                #     "features": [
                #         {
                #             "geometry": {
                #                 "coordinates": [9.998645,51.9982968],
                #                 "type":"Point"
                #             },
                #             "type":"Feature",
                #             "properties": {
                #                 "osm_id":693697564,
                #                 "country":"Germany",
                #                 "city":"Lamspringe",
                #                 "countrycode":"DE",
                #                 "postcode":"31195",
                #                 "county":"Landkreis Hildesheim",
                #                 "type":"house",
                #                 "osm_type":"N",
                #                 "osm_key":"tourism",
                #                 "street":"Evensener Dorfstraße",
                #                 "district":"Sehlem",
                #                 "osm_value":"information",
                #                 "name":"Geographischer Punkt",
                #                 "state":"Lower Saxony"
                #             }
                #         }
                #     ],
                #     "type":"FeatureCollection"
                # }

                # {
                #     "features":[{
                #         "geometry":{
                #             "coordinates":[--,--],
                #             "type":"Point"
                #         },
                #         "type":"Feature",
                #         "properties":{
                #             "osm_id":548867164,
                #             "extent":[--,--,--,--],
                #             "country":"Germany",
                #             "city":"--",
                #             "countrycode":"DE",
                #             "postcode":"--",
                #             "locality":"--",
                #             "county":"--",
                #             "type":"house",
                #             "osm_type":"W",
                #             "osm_key":"building",
                #             "housenumber":"--",
                #             "street":"--",
                #             "district":"--",
                #             "osm_value":"residential",
                #             "state":"--"
                #         }
                #     }],
                #     "type":"FeatureCollection"
                # }

            data = response.json()
            if data and "features" in data:
                feature: dict[str, dict] = data["features"][0]
                point.country = feature["properties"].get("country")
                point.city = feature["properties"].get("city")
                point.state = feature["properties"].get("state")
                point.postal_code = feature["properties"].get("postcode")
                point.street = feature["properties"].get("street")
                point.street_number = feature["properties"].get("housenumber")
                db.session.commit()

        self.done = True


class GenerateFullStatisticsJob(Job):
    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

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
        for data in gps_data:
            key = f"{data.timestamp.year}-{data.timestamp.month}"
            if key not in monthly_stats:
                monthly_stats[key] = MonthlyStatistic(
                    user_id=user.id,
                    year=data.timestamp.year,
                    month=data.timestamp.month
                )
                
            if i > 0:
                prev = gps_data[i - 1]
                distance = self.get_distance(prev, data)
                monthly_stats[key].total_distance_m += distance
            i += 1

        # Save the monthly statistics
        for stat in monthly_stats.values():
            db.session.add(stat)
        db.session.commit()


        self.done = True