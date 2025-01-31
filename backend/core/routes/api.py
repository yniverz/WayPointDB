from datetime import datetime
from flask import redirect, request, g, session, url_for
from flask_restx import Resource, fields, Namespace
from flask_restx.reqparse import RequestParser
from ..extensions import db
from ..models import GPSData, User
from ..utils import api_key_required

# Create a dedicated namespace for the GPS routes
api_gps_ns = Namespace("gps", description="GPS Data operations")

gps_model = api_gps_ns.model("GPSData", {
    "timestamp": fields.String(required=True),
    "latitude": fields.Float(required=True),
    "longitude": fields.Float(required=True),
    "horizontal_accuracy": fields.Float(),
    "altitude": fields.Float(),
    "vertical_accuracy": fields.Float(),
    "heading": fields.Float(),
    "heading_accuracy": fields.Float(),
    "speed": fields.Float(),
    "speed_accuracy": fields.Float(),
})

api_key_parser = RequestParser()
api_key_parser.add_argument(
    "api_key",
    type=str,
    required=True,
    help="API key for user authentication",
    location="args"
)

batch_gps_model = api_gps_ns.model("BatchGPSData", {
    "gps_data": fields.List(fields.Nested(gps_model), required=True, description="List of GPS data points"),
})

@api_gps_ns.route("/batch")
class GPSBatch(Resource):
    @api_gps_ns.expect(api_key_parser, batch_gps_model)
    @api_key_required
    def post(self):
        """Submit batch GPS data (requires a valid API key)."""
        data = request.json or {}
        gps_entries = data.get("gps_data")

        if not gps_entries or not isinstance(gps_entries, list):
            return {"error": "Invalid data format"}, 400

        # The user from the valid API key is in g.current_user
        user = g.current_user

        # Save each GPS entry
        for entry in gps_entries:
            ts_str = entry.get("timestamp")
            try:
                # Attempt to parse the timestamp as ISO 8601
                ts = datetime.fromisoformat(ts_str)
            except:
                ts = datetime.now()  # fallback if parse fails

            gps_record = GPSData(
                user_id=user.id,
                timestamp=ts,
                latitude=entry["latitude"],
                longitude=entry["longitude"],
                horizontal_accuracy=entry.get("horizontal_accuracy"),
                altitude=entry.get("altitude"),
                vertical_accuracy=entry.get("vertical_accuracy"),
                heading=entry.get("heading"),
                heading_accuracy=entry.get("heading_accuracy"),
                speed=entry.get("speed"),
                speed_accuracy=entry.get("speed_accuracy"),
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

@api_gps_ns.route("/overland")
class OverlandGPSBatch(Resource):
    """
    Endpoint to accept GeoJSON-style Overland location updates.
    """

    @api_gps_ns.expect(api_key_parser, overland_model)
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
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))  # handle UTC 'Z'
            except Exception:
                ts = datetime.now()

            # Create a GPSData record
            gps_record = GPSData(
                user_id=user.id,
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
        return {"message": "Overland GPS data added successfully"}, 201







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
        # return {"message": "Logged in successfully"}, 200