from collections import defaultdict
from datetime import datetime
from flask import jsonify, redirect, request, g, session, url_for
from flask_restx import Resource, fields, Namespace
from flask_restx.reqparse import RequestParser
from sqlalchemy import func

from ..config import Config
from ..extensions import db
from ..models import DailyStatistic, GPSData, User
from ..utils import api_key_required

# Create a dedicated namespace for the GPS routes
api_gps_ns = Namespace("gps", description="GPS Data operations")

gps_model = api_gps_ns.model("GPSData", {
    "timestamp": fields.String(
        required=True,
        description="Timestamp in Second since epoch",
        example="1672531199"
    ),
    "latitude": fields.Float(
        required=True,
        description="Latitude of the GPS point",
        example=52.605508
    ),
    "longitude": fields.Float(
        required=True,
        description="Longitude of the GPS point",
        example=13.407278
    ),
    "horizontal_accuracy": fields.Float(
        description="Horizontal accuracy in meters",
        example=10.0
    ),
    "altitude": fields.Float(
        description="Altitude in meters",
        example=309.0
    ),
    "vertical_accuracy": fields.Float(
        description="Vertical accuracy in meters",
        example=2.0
    ),
    "heading": fields.Float(
        description="Heading in degrees",
        example=-1.0
    ),
    "heading_accuracy": fields.Float(
        description="Heading accuracy in degrees",
        example=-1.0
    ),
    "speed": fields.Float(
        description="Speed in meters per second",
        example=0.0
    ),
    "speed_accuracy": fields.Float(
        description="Speed accuracy in meters per second",
        example=0.36
    ),
})

api_key_parser = RequestParser()
api_key_parser.add_argument(
    "api_key",
    type=str,
    required=True,
    help="API key for user authentication",
    location="args"
)

unauthorized_response_model = api_gps_ns.model("UnauthorizedResponse", {
    "error": fields.String(
        description="Error message",
        example="Invalid or missing API key"
    )
})




batch_gps_model = api_gps_ns.model("BatchGPSData", {
    "gps_data": fields.List(fields.Nested(gps_model), required=True, description="List of GPS data points"),
})

@api_gps_ns.route("/batch")
class GPSBatch(Resource):
    @api_gps_ns.expect(api_key_parser, batch_gps_model)
    @api_gps_ns.response(201, "Success")
    @api_key_required
    def post(self):
        """Submit batch GPS data (requires a valid API key)."""
        data = request.json or {}
        gps_entries = data.get("gps_data")

        if not gps_entries or not isinstance(gps_entries, list):
            return {"error": "Invalid data format"}, 400

        # The user from the valid API key is in g.current_user
        user = g.current_user
        trace = g.current_trace

        user_id = user.id
        trace_id = None
        if trace:
            user_id = None
            trace_id = trace.id

        # Save each GPS entry
        for entry in gps_entries:
            ts_str = entry.get("timestamp")
            try:
                # Attempt to parse the timestamp as ISO 8601
                ts = datetime.fromtimestamp(int(ts_str), tz=datetime.now().astimezone().tzinfo)  # handle UTC 'Z'
            except:
                ts = datetime.now()  # fallback if parse fails

            gps_record = GPSData(
                user_id=user_id,
                trace_id=trace_id,
                timestamp=ts,
                latitude=round(float(entry["latitude"]), 8),
                longitude=round(float(entry["longitude"]), 8),
                horizontal_accuracy=round(float(entry.get("horizontal_accuracy")), 8),
                altitude=round(float(entry.get("altitude")), 8),
                vertical_accuracy=round(float(entry.get("vertical_accuracy")), 8),
                heading=round(float(entry.get("heading")), 8),
                heading_accuracy=round(float(entry.get("heading_accuracy")), 8),
                speed=round(float(entry.get("speed")), 8),
                speed_accuracy=round(float(entry.get("speed_accuracy")), 8),
            )
            db.session.add(gps_record)

        db.session.commit()
        return {"message": "GPS data added successfully"}, 201


# GeoJSON Geometry Model
geometry_model = api_gps_ns.model("Geometry", {
    "type": fields.String(
        required=True,
        description="The geometry type, always 'Point'",
        example="Point"
    ),
    "coordinates": fields.List(
        fields.Float,
        required=True,
        description="[longitude, latitude] of the GPS point",
        example=[13.407278, 52.605508] 
    ),
})

# GeoJSON Properties Model (Containing Overland-specific data)
properties_model = api_gps_ns.model("Properties", {
    "timestamp": fields.String(
        required=True,
        description="Timestamp in ISO 8601 format",
        example="2025-01-31T09:32:11Z"
    ),
    "speed": fields.Float(
        description="Speed in meters per second",
        example=0.0
    ),
    "speed_accuracy": fields.Float(
        description="Speed accuracy in meters per second",
        example=0.36
    ),
    "horizontal_accuracy": fields.Float(
        description="Horizontal accuracy in meters",
        example=10.0
    ),
    "vertical_accuracy": fields.Float(
        description="Vertical accuracy in meters",
        example=2.0
    ),
    "altitude": fields.Float(
        description="Altitude in meters",
        example=309.0
    ),
    "course": fields.Float(
        description="Course (heading) in degrees",
        example=-1.0
    ),
    "course_accuracy": fields.Float(
        description="Course accuracy in degrees",
        example=-1.0
    ),
    "battery_state": fields.String(
        description="Battery state of the device",
        example="unplugged"
    ),
    "battery_level": fields.Float(
        description="Battery level as a fraction (0-1)",
        example=0.65
    ),
    "motion": fields.List(
        fields.String,
        description="Motion types detected (e.g., 'stationary', 'walking')",
        example=["stationary"]
    ),
    "wifi": fields.String(
        description="Connected WiFi network name",
        example="Main"
    ),
})

# Full GeoJSON Feature Model (Overland Location)
feature_model = api_gps_ns.model("Feature", {
    "type": fields.String(
        required=True,
        description="The feature type, always 'Feature'",
        example="Feature"
    ),
    "geometry": fields.Nested(geometry_model),
    "properties": fields.Nested(properties_model),
})

# Overland GPS Batch Request Model
overland_model = api_gps_ns.model("OverlandGPSRequest", {
    "locations": fields.List(
        fields.Nested(feature_model),
        required=True,
        description="List of location features in GeoJSON Feature format"
    )
})

overland_success_response_model = api_gps_ns.model("OverlandSuccessResponse", {
    "result": fields.String(
        description="Result of the operation",
        example="ok"
    )
})

@api_gps_ns.route("/overland")
class OverlandGPSBatch(Resource):
    """
    Endpoint to accept GeoJSON-style Overland location updates.
    """

    @api_gps_ns.expect(api_key_parser, overland_model)
    @api_gps_ns.response(201, "Success", overland_success_response_model)
    @api_gps_ns.response(401, "Unauthorized", unauthorized_response_model)
    @api_key_required
    def post(self):
        """
        Submit GeoJSON-style GPS data from Overland (requires a valid API key).
        """
        # Parse request JSON
        data = request.json or {}
        locations = data.get("locations")

        if not locations or not isinstance(locations, list):
            return {"error": "Invalid data format: 'locations' should be a list"}, 400

        user = g.current_user  # Retrieved from @api_key_required decorator
        trace = g.current_trace

        user_id = user.id
        trace_id = None
        if trace:
            user_id = None
            trace_id = trace.id

        for feature in locations:
            # Validate that it follows the GeoJSON Feature structure
            if feature.get("type") != "Feature":
                continue  # or return an error if you'd rather fail early

            geometry = feature.get("geometry", {})
            properties = feature.get("properties", {})

            # Ensure geometry is a "Point" with [longitude, latitude]
            coords = geometry.get("coordinates", [])
            if len(coords) != 2 or geometry.get("type") != "Point":
                continue  # or handle invalid geometry

            longitude, latitude = coords[0], coords[1]

            # Parse timestamp, default to now if parsing fails
            ts_str = properties.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_str) # e.g. "2025-04-19T20:12:44Z"
            except Exception:
                ts = datetime.now()

            # Create a GPSData record
            gps_record = GPSData(
                user_id=user_id,
                trace_id=trace_id,
                timestamp=ts,
                latitude=latitude,
                longitude=longitude,
                horizontal_accuracy=properties.get("horizontal_accuracy"),
                vertical_accuracy=properties.get("vertical_accuracy"),
                altitude=properties.get("altitude"),
                speed=properties.get("speed"),
                speed_accuracy=properties.get("speed_accuracy"),
                heading=properties.get("course"),  # "course" is equivalent to heading
                heading_accuracy=properties.get("course_accuracy"),
                # Additional fields can be added as needed
            )

            db.session.add(gps_record)

        db.session.commit()
        return {"result": "ok"}, 201


@api_gps_ns.route("/owntracks")
class OwntracksGPS(Resource):
    """
    Endpoint to accept OwnTracks-style GPS data. Only retains fields
    consistent with the existing GPSData model and ignores others.
    """

    @api_gps_ns.expect(api_key_parser)
    @api_gps_ns.response(201, "Success", overland_success_response_model)
    @api_gps_ns.response(401, "Unauthorized", unauthorized_response_model)
    @api_key_required
    def post(self):
        """
        Submit OwnTracks-style GPS data (requires a valid API key).
        """
        # Parse request JSON
        data = request.json or {}

        user = g.current_user
        trace = g.current_trace

        # In your existing logic: if a trace object is present,
        # you store GPS data under trace_id with no user_id; otherwise you store under user_id.
        user_id = user.id
        trace_id = None
        if trace:
            user_id = None
            trace_id = trace.id

        # The OwnTracks payload uses epoch timestamps in "tst"
        # and lat/lon in "lat"/"lon", etc.
        # We'll parse only the data your system tracks.

        # 1) Timestamp
        #    - primary is "tst" (epoch seconds)
        #    - if needed, fallback to other fields (e.g., "isotst"), or default to now on parse error
        timestamp = None
        if "tst" in data:
            try:
                timestamp = datetime.fromtimestamp(int(data["tst"]), tz=datetime.now().astimezone().tzinfo)
            except:
                timestamp = datetime.now()
        else:
            # Optional fallback if "tst" is missing
            timestamp = datetime.now()

        # 2) Latitude / Longitude
        latitude = data.get("lat")
        longitude = data.get("lon")

        # 3) Horizontal accuracy ("acc")
        horizontal_accuracy = data.get("acc")

        # 4) Altitude ("alt")
        altitude = data.get("alt")

        # 5) Vertical accuracy ("vac")
        vertical_accuracy = data.get("vac")

        # 6) Heading / speed / heading_accuracy / speed_accuracy are
        #    not provided in this example payload, so they're set to None.
        heading = None
        heading_accuracy = None
        speed = None
        speed_accuracy = None

        # Create and store the GPSData record
        gps_record = GPSData(
            user_id=user_id,
            trace_id=trace_id,
            timestamp=timestamp,
            latitude=latitude,
            longitude=longitude,
            horizontal_accuracy=horizontal_accuracy,
            altitude=altitude,
            vertical_accuracy=vertical_accuracy,
            heading=heading,
            heading_accuracy=heading_accuracy,
            speed=speed,
            speed_accuracy=speed_accuracy,
        )
        db.session.add(gps_record)
        db.session.commit()

        return {"result": "ok"}, 201





api_account_ns = Namespace("account", description="Account operations")

@api_account_ns.route("/login")
class AccountLogin(Resource):
    @api_account_ns.expect(api_key_parser)
    @api_key_required
    def get(self):
        """Login with an API key."""
        user = g.current_user
        session["user_id"] = str(user.id)

        return redirect(url_for("web.home"))




months_stats_model = api_gps_ns.model("MonthlyStats", {
    "january": fields.Float(description="Distance in January", example=0.0),
    "february": fields.Float(description="Distance in February", example=0.0),
    "march": fields.Float(description="Distance in March", example=0.0),
    "april": fields.Float(description="Distance in April", example=0.0),
    "may": fields.Float(description="Distance in May", example=0.0),
    "june": fields.Float(description="Distance in June", example=0.0),
    "july": fields.Float(description="Distance in July", example=0.0),
    "august": fields.Float(description="Distance in August", example=0.0),
    "september": fields.Float(description="Distance in September", example=0.0),
    "october": fields.Float(description="Distance in October", example=0.0),
    "november": fields.Float(description="Distance in November", example=0.0),
    "december": fields.Float(description="Distance in December", example=0.0)
})

yearly_stats_model = api_gps_ns.model("YearlyStats", {
    "year": fields.Integer(description="Year", example=2023),
    "totalDistanceKm": fields.Float(description="Total distance in km", example=0.0),
    "totalCountriesVisited": fields.Integer(description="Total countries visited", example=0),
    "totalCitiesVisited": fields.Integer(description="Total cities visited", example=0),
    "countries": fields.List(fields.String(description="List of countries visited", example=["Germany", "France"])),
    "cities": fields.List(fields.String(description="List of cities visited", example=["Berlin", "Paris"])),
    "monthlyDistanceKm": fields.Nested(months_stats_model)
})

stats_model = api_gps_ns.model("AllStats", {
    "totalDistanceKm": fields.Float(description="Total distance in km", example=0.0),
    "totalPointsTracked": fields.Integer(description="Total points tracked", example=0),
    "totalReverseGeocodedPoints": fields.Integer(description="Total reverse geocoded points", example=0),
    "totalCountriesVisited": fields.Integer(description="Total countries visited", example=0),
    "totalCitiesVisited": fields.Integer(description="Total cities visited", example=0),
    "countries": fields.List(fields.String(description="List of countries visited", example=["Germany", "France"])),
    "cities": fields.List(fields.String(description="List of cities visited", example=["Berlin", "Paris"])),
    "yearlyStats": fields.List(fields.Nested(yearly_stats_model)),
    "MIN_COUNTRY_VISIT_DURATION_FOR_STATS": fields.String(description="Minimum duration for country visit statistics", example="1 day"),
    "MIN_CITY_VISIT_DURATION_FOR_STATS": fields.String(description="Minimum duration for city visit statistics", example="1 day")
})

@api_account_ns.route("/stats")
class AccountStats(Resource):
    """Get user statistics."""

    @api_account_ns.expect(api_key_parser)
    @api_account_ns.response(200, "Success", stats_model)
    @api_account_ns.response(401, "Unauthorized", unauthorized_response_model)
    @api_key_required
    def get(self):
        """Get user statistics (requires a valid API key)."""
        
        total_points = GPSData.query.filter_by(**g.trace_query).count()
        total_geocoded = GPSData.query.filter_by(**g.trace_query).filter(GPSData.reverse_geocoded == True).count()
        total_not_geocoded = GPSData.query.filter_by(**g.trace_query).filter(GPSData.reverse_geocoded == True).filter(GPSData.country == None).count()
        stats: list[DailyStatistic] = DailyStatistic.query.filter_by(**g.trace_query).all()

        # We'll group stats by year
        stats_by_year = defaultdict(lambda: {
            "monthly_distances": [0.0] * 12,  # 12 months
            "cities": set(),
            "countries": set(),
            "total_distance": 0.0
        })

        stats.sort(key=lambda x: (x.year, x.month, x.day))

        last_visit_cities = dict()
        last_visit_countries = dict()

        for stat in stats:
            year = stat.year
            month_idx = stat.month - 1  # January -> 0, etc.

            # Accumulate distances (in meters). We'll convert to km later.
            stats_by_year[year]["monthly_distances"][month_idx] += stat.total_distance_m

            # Update visited cities / countries (they are stored in JSON columns)
            if stat.visited_cities:
                stats_by_year[year]["cities"].update([tuple(city) for city in stat.visited_cities])
                last_visit_cities.update([(tuple(city), f"{stat.day:02d}-{stat.month:02d}-{stat.year}") for city in stat.visited_cities])
            if stat.visited_countries:
                stats_by_year[year]["countries"].update(stat.visited_countries)
                last_visit_countries.update([(country, f"{stat.day:02d}-{stat.month:02d}-{stat.year}") for country in stat.visited_countries])

            # Track total distance in meters for each year
            stats_by_year[year]["total_distance"] += stat.total_distance_m
            

        # Collect overall unique sets across **all** years
        all_cities = set()
        all_countries = set()
        total_distance = 0.0

        # Convert sets to lists for JSON serialization; also convert meters -> km
        stats_by_year_processed = {}
        for year, data in sorted(stats_by_year.items(), reverse=True):
            all_cities.update(data["cities"])
            all_countries.update(data["countries"])
            total_distance += data["total_distance"]

            stats_by_year_processed[year] = {
                "monthly_distances": [int(dist_m / 1000) for dist_m in data["monthly_distances"]],
                "cities": list(data["cities"]),
                "countries": list(data["countries"]),
                "total_distance": int(data["total_distance"] / 1000.0),  # store in KM
            }

        # Convert all_cities and all_countries to lists for JSON serialization
        all_cities = sorted(list(all_cities))
        all_countries = sorted(list(all_countries))

        yearly_stats = []
        for year, data in sorted(stats_by_year.items(), reverse=True):
            yearly_stats.append({
                "year": year,
                "totalDistanceKm": int(data["total_distance"] / 1000.0),
                "totalCountriesVisited": len(data["countries"]),
                "totalCitiesVisited": len(data["cities"]),
                "countries": list(data["countries"]),
                "cities": list(data["cities"]),
                "monthlyDistanceKm": {
                    month: int(dist_m / 1000) for month, dist_m in zip(
                        ["january", "february", "march", "april", "may", "june",
                         "july", "august", "september", "october", "november", "december"],
                        data["monthly_distances"]
                    )
                }
            })

        stats = {
            "totalDistanceKm": int(total_distance / 1000.0),
            "totalPointsTracked": total_points,
            "totalReverseGeocodedPoints": total_geocoded,
            "totalCountriesVisited": len(all_countries),
            "totalCitiesVisited": len(all_cities),
            "countries": list(all_countries),
            "cities": list(all_cities),
            "yearlyStats": yearly_stats,
            "MIN_COUNTRY_VISIT_DURATION_FOR_STATS": self.formatTimeDelta(Config.MIN_COUNTRY_VISIT_DURATION_FOR_STATS),
            "MIN_CITY_VISIT_DURATION_FOR_STATS": self.formatTimeDelta(Config.MIN_CITY_VISIT_DURATION_FOR_STATS)
        }

        return jsonify(stats)
    
    def formatTimeDelta(self, seconds: int):
        
        if seconds < 60:
            return f"{seconds} second" + ("s" if seconds > 1 else "")
        elif seconds < 60 * 60:
            return f"{seconds // 60} minute" + ("s" if seconds // 60 > 1 else "")
        elif seconds < 60 * 60 * 24:
            return f"{seconds // (60 * 60)} hour" + ("s" if seconds // (60 * 60) > 1 else "")
        else:
            return f"{seconds // (60 * 60 * 24)} day" + ("s" if seconds // (60 * 60 * 24) > 1 else "")
    


monthly_stats_model = api_gps_ns.model("MonthStats", {
    "month": fields.Integer(description="Month", example=10),
    "totalDistanceKm": fields.Float(description="Total distance in km", example=0.0),
    "totalCountriesVisited": fields.Integer(description="Total countries visited", example=0),
    "totalCitiesVisited": fields.Integer(description="Total cities visited", example=0),
    "countries": fields.List(fields.String(description="List of countries visited", example=["Germany", "France"])),
    "cities": fields.List(fields.String(description="List of cities visited", example=["Berlin", "Paris"])),
    "dailyDistanceKm": fields.List(fields.Float(description="Daily distance in km", example=0.0)),
})

year_stats_model = api_gps_ns.model("YearStats", {
    "totalDistanceKm": fields.Float(description="Total distance in km", example=0.0),
    "totalPointsTracked": fields.Integer(description="Total points tracked", example=0),
    "totalReverseGeocodedPoints": fields.Integer(description="Total reverse geocoded points", example=0),
    "totalCountriesVisited": fields.Integer(description="Total countries visited", example=0),
    "totalCitiesVisited": fields.Integer(description="Total cities visited", example=0),
    "countries": fields.List(fields.String(description="List of countries visited", example=["Germany", "France"])),
    "cities": fields.List(fields.String(description="List of cities visited", example=["Berlin", "Paris"])),
    "monthlyStats": fields.List(fields.Nested(monthly_stats_model)),
    "MIN_COUNTRY_VISIT_DURATION_FOR_STATS": fields.String(description="Minimum duration for country visit statistics", example="1 day"),
    "MIN_CITY_VISIT_DURATION_FOR_STATS": fields.String(description="Minimum duration for city visit statistics", example="1 day")
})

@api_account_ns.route("/stats/<int:year>")
class AccountYearStats(Resource):
    """
    Endpoint to get yearly statistics for a user.
    """

    @api_account_ns.expect(api_key_parser)
    @api_account_ns.response(200, "Success", year_stats_model)
    @api_account_ns.response(401, "Unauthorized", unauthorized_response_model)
    @api_key_required
    def get(self, year):
        """
        Get yearly statistics for the logged-in user (requires a valid API key).
        """
        total_points = GPSData.query.filter_by(**g.trace_query).filter(func.extract("year", GPSData.timestamp) == year).count()

        stats = DailyStatistic.query.filter_by(**g.trace_query, year=year).all()

        months: dict[int, dict] = {}
        for stat in stats:
            if stat.month not in months:
                months[stat.month] = {}

            months[stat.month][stat.day] = stat

        monthly_stats = []

        for month in months:
            month_stats = months[month]
            total_distance = 0.0
            cities = set()
            countries = set()
            daily_distances = []
            for day in sorted(month_stats.keys()):
                stat = month_stats[day]
                daily_distances.append(stat.total_distance_m / 1000.0)
                total_distance += stat.total_distance_m
                if stat.visited_cities:
                    cities.update([tuple(city) for city in stat.visited_cities])
                if stat.visited_countries:
                    countries.update(stat.visited_countries)
            monthly_stats.append({
                "month": month,
                "totalDistanceKm": int(total_distance / 1000.0),
                "totalCountriesVisited": len(countries),
                "totalCitiesVisited": len(cities),
                "countries": list(countries),
                "cities": list(cities),
                "dailyDistanceKm": [int(daily_distance) for daily_distance in daily_distances]
            })
        # Convert all_cities and all_countries to lists for JSON serialization
        all_cities = set()
        all_countries = set()
        total_distance = 0.0
        for month in monthly_stats:
            all_cities.update(month["cities"])
            all_countries.update(month["countries"])
            total_distance += month["totalDistanceKm"]
        all_cities = sorted(list(all_cities))
        all_countries = sorted(list(all_countries))
        stats = {
            "totalDistanceKm": int(total_distance),
            "totalPointsTracked": total_points,
            "totalReverseGeocodedPoints": 0,
            "totalCountriesVisited": len(all_countries),
            "totalCitiesVisited": len(all_cities),
            "countries": list(all_countries),
            "cities": list(all_cities),
            "monthlyStats": monthly_stats,
            "MIN_COUNTRY_VISIT_DURATION_FOR_STATS": self.formatTimeDelta(Config.MIN_COUNTRY_VISIT_DURATION_FOR_STATS),
            "MIN_CITY_VISIT_DURATION_FOR_STATS": self.formatTimeDelta(Config.MIN_CITY_VISIT_DURATION_FOR_STATS)
        }
        return jsonify(stats)

    def formatTimeDelta(self, seconds: int):
        if seconds < 60:
            return f"{seconds} second" + ("s" if seconds > 1 else "")
        elif seconds < 60 * 60:
            return f"{seconds // 60} minute" + ("s" if seconds // 60 > 1 else "")
        elif seconds < 60 * 60 * 24:
            return f"{seconds // (60 * 60)} hour" + ("s" if seconds // (60 * 60) > 1 else "")
        else:
            return f"{seconds // (60 * 60 * 24)} day" + ("s" if seconds // (60 * 60 * 24) > 1 else "")