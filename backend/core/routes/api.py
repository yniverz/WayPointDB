from datetime import datetime
from flask import request, g
from flask_restx import Resource, fields, Namespace
from ..extensions import db
from ..models import GPSData
from ..utils import api_key_required

# Create a dedicated namespace for the GPS routes
api_ns = Namespace("gps", description="GPS Data operations")
# gps_ns = api.namespace("gps", description="GPS Data operations")

gps_model = api_ns.model("GPSData", {
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

batch_parser = api_ns.parser()
batch_parser.add_argument(
    "api_key",
    type=str,
    required=True,
    help="API key for user authentication",
    location="args"
)

batch_gps_model = api_ns.model("BatchGPSData", {
    "gps_data": fields.List(fields.Nested(gps_model), required=True, description="List of GPS data points"),
})

@api_ns.route("/batch")
class GPSBatch(Resource):
    def get(self):
        return "Please use POST to submit GPS data", 405

    @api_ns.expect(batch_parser, batch_gps_model)
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

# Finally, add the gps namespace to the main API
# api.add_namespace(gps_ns, path="gps")
