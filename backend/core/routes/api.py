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
    def get(self):
        return "Please use POST to submit GPS data", 405

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