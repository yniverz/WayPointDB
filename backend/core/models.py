from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from .extensions import db

class User(db.Model):
    """Simple User model with is_admin boolean and a single API key."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    api_key = db.Column(db.String(255), unique=True, nullable=True)

    def set_password(self, raw_password):
        self.password = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password, raw_password)


class GPSData(db.Model):
    """Stores GPS data tied to a user."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.now(timezone.utc))
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    horizontal_accuracy = db.Column(db.Float)
    altitude = db.Column(db.Float)
    vertical_accuracy = db.Column(db.Float)
    heading = db.Column(db.Float)
    heading_accuracy = db.Column(db.Float)
    speed = db.Column(db.Float)
    speed_accuracy = db.Column(db.Float)

    country = db.Column(db.String(255))
    city = db.Column(db.String(255))
    state = db.Column(db.String(255))
    postal_code = db.Column(db.String(255))
    street = db.Column(db.String(255))
    street_number = db.Column(db.String(255))


    user = db.relationship("User", backref=db.backref("gps_data", lazy=True))


class MonthlyStatistic(db.Model):
    """Stores monthly statistics for a user."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Date, nullable=False)
    total_distance_m = db.Column(db.Float, default=0.0)
    visited_countries = db.Column(db.JSON, default=[])
    visited_cities = db.Column(db.JSON, default=[])

    user = db.relationship("User", backref=db.backref("monthly_stats", lazy=True))