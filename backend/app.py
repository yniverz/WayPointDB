import os
import time
import uuid
import json
from functools import wraps
from datetime import datetime
from flask_restx import reqparse

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    session,
    g  # we'll use g to store the authenticated user for API requests
)
import flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_restx import Api, Resource, fields
from jinja2 import ChoiceLoader, FileSystemLoader
import psycopg2
from werkzeug.security import generate_password_hash, check_password_hash

#
# ----------------- Flask App Setup ----------------- #
#

app = Flask(__name__)
# Generate a random secret key for session encryption
app.config["SECRET_KEY"] = "test" # uuid.uuid4().hex

# Custom Jinja loader to allow .jinja suffix
class CustomLoader(FileSystemLoader):
    def get_source(self, environment, template):
        # If the template has no extension, assume .html
        if not template.endswith(".html") and not template.endswith(".jinja"):
            template += ".html"  # Default

        # Convert .html requests to .jinja
        possible_template = template.replace(".html", ".jinja")

        # Check if .jinja version exists
        if os.path.exists(os.path.join(self.searchpath[0], possible_template)):
            template = possible_template  # Use .jinja version

        return super().get_source(environment, template)

app.jinja_loader = ChoiceLoader([CustomLoader("templates")])

#
# ----------------- Database Configuration ----------------- #
#

DB_HOST = os.getenv("POSTGRES_HOST", "db")
DB_NAME = os.getenv("POSTGRES_DB", "mydatabase")
DB_USER = os.getenv("POSTGRES_USER", "user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "password")

app.config["SQLALCHEMY_DATABASE_URI"] = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

#
# ----------------- Models ----------------- #
#

class User(db.Model):
    """Simple User model with is_admin boolean and a single API key."""
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    api_key = db.Column(db.String(255), unique=True, nullable=True)  # Store user's API key

class GPSData(db.Model):
    """Stores GPS data tied to a user."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    horizontal_accuracy = db.Column(db.Float)
    altitude = db.Column(db.Float)
    vertical_accuracy = db.Column(db.Float)
    heading = db.Column(db.Float)
    heading_accuracy = db.Column(db.Float)
    speed = db.Column(db.Float)
    speed_accuracy = db.Column(db.Float)

    user = db.relationship("User", backref=db.backref("gps_data", lazy=True))

    def to_dict(self):
        """Return a dictionary representation of this GPSData record."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "horizontal_accuracy": self.horizontal_accuracy,
            "altitude": self.altitude,
            "vertical_accuracy": self.vertical_accuracy,
            "heading": self.heading,
            "heading_accuracy": self.heading_accuracy,
            "speed": self.speed,
            "speed_accuracy": self.speed_accuracy,
        }
    
    @staticmethod
    def list_to_json(gps_data_list):
        """Convert a list of GPSData objects to a JSON string."""
        return json.dumps([data.to_dict() for data in gps_data_list])

#
# ----------------- Helpers & Decorators ----------------- #
#

def create_default_user():
    """
    Ensures tables exist and creates a default admin user if not present.
    This function is called manually in the main block.
    """
    db.create_all()

    admin_email = "admin@example.com"
    admin_pass = "password"
    admin_user = User.query.filter_by(email=admin_email).first()
    if not admin_user:
        hashed = generate_password_hash(admin_pass)
        admin_user = User(email=admin_email, password=hashed, is_admin=True)
        db.session.add(admin_user)
        db.session.commit()

def get_current_user():
    """Return the currently logged-in user object (or None) using the session."""
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None

# def login_required(f):
#     """Decorator to ensure the user is logged in (session-based) for HTML routes."""
#     @wraps(f)
#     def decorated_function(*args, **kwargs):
#         if "user_id" not in session:
#             return redirect(url_for("login"))
#         return f(*args, **kwargs)
#     return decorated_function

def login_required(f):
    """Decorator to ensure the user is logged in (session-based) for HTML routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        
        # Store on Flask's g object so we can inject into templates
        g.current_user = get_current_user()
        
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_user():
    """
    This context processor runs before every template render.
    It returns a dict of variables to add to the Jinja context.
    """
    return {
        "current_user": getattr(g, "current_user", None)
    }

#
# --- API Key Authentication for the REST Endpoints --- #
#

def get_user_by_api_key(api_key):
    """Returns a User object given a valid api_key, else None."""
    if not api_key:
        return None
    return User.query.filter_by(api_key=api_key).first()

def api_key_required(f):
    """
    REST endpoints should use this decorator to require an API key.
    The API key can be passed as a query parameter: ?api_key=<value>
    # The key must be passed in request header:  X-API-KEY: <value>
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = flask.request.args.get("api_key")
        # api_key = request.headers.get("X-API-KEY")
        user = get_user_by_api_key(api_key)
        if not user:
            return {"error": "Invalid or missing API key"}, 401
        # Store the user in Flask's global context so we can access it inside the route
        g.current_user = user
        return f(*args, **kwargs)
    return decorated

#
# ----------------- Flask-RESTx Setup ----------------- #
#

api = Api(
    app,
    version="1.0",
    title="GPS Tracker API V1",
    description="API V1 documentation for GPS tracking",
    prefix="/api/v1",
    doc="/api/v1/docs"
)

ns = api.namespace("gps", description="GPS Data operations")

gps_model = api.model("GPSData", {
    "timestamp": fields.String(required=True, description="Timestamp of GPS point (ISO 8601 or similar)"),
    "latitude": fields.Float(required=True, description="Latitude"),
    "longitude": fields.Float(required=True, description="Longitude"),
    "horizontal_accuracy": fields.Float(description="Horizontal accuracy"),
    "altitude": fields.Float(description="Altitude"),
    "vertical_accuracy": fields.Float(description="Vertical accuracy"),
    "heading": fields.Float(description="Heading"),
    "heading_accuracy": fields.Float(description="Heading accuracy"),
    "speed": fields.Float(description="Speed"),
    "speed_accuracy": fields.Float(description="Speed accuracy"),
})

batch_parser = api.parser()
batch_parser.add_argument(
    "api_key",
    type=str,
    required=True,
    help="API key for user authentication",
    location="args"
)

batch_gps_model = api.model("BatchGPSData", {
    "gps_data": fields.List(fields.Nested(gps_model), required=True, description="List of GPS data points"),
})

@ns.route("/batch")
class GPSBatch(Resource):
    def get(self):
        return "Please use POST to submit GPS data", 405

    @api.expect(batch_parser, batch_gps_model)
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

#
# ----------------- HTML Routes ----------------- #
#

@app.route("/")
def home():
    """Simple home route. If logged in, show stuff, else prompt login."""
    user = get_current_user()
    if user:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    """Simple login form that sets session data on success."""
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            return redirect(url_for("home"))
        else:
            return "Invalid credentials", 401

    return render_template("login.jinja")

@app.route("/map")
@login_required
def map():
    """Displays a Map with a polyline of the user's GPS points."""

    # get the last point based on timestamp for the user
    user = get_current_user()
    if not user:
        return "Unauthorized", 401
    
    last_point = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp.desc()).first()
    if not last_point:
        last_point = {"latitude": 52.516310, "longitude": 13.378208}
    
    return render_template("map.jinja", last_point=last_point)

@app.route("/gps_data")
@login_required
def get_gps_data():
    """Fetch GPS data efficiently using psycopg2 for querying."""
    
    time0 = time.time()

    # Get bounds from request
    ne_lat = request.args.get("ne_lat", type=float)
    ne_lng = request.args.get("ne_lng", type=float)
    sw_lat = request.args.get("sw_lat", type=float)
    sw_lng = request.args.get("sw_lng", type=float)
    zoom = request.args.get("zoom", type=int, default=10)

    if None in [ne_lat, ne_lng, sw_lat, sw_lng]:
        return jsonify({"error": "Invalid or missing bounds"}), 400
    
    time1 = time.time()

    user = get_current_user()  # Uses session authentication
    if not user:
        return jsonify({"error": "Unauthorized"}), 401

    time2 = time.time()

    # **Connect to the PostgreSQL database using psycopg2**
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        host=DB_HOST
    )
    cursor = conn.cursor()

    # **Optimized SQL query for fetching points within bounds**
    query = """
        SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy, 
               altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
        FROM gps_data
        WHERE user_id = %s 
        AND latitude BETWEEN %s AND %s 
        AND longitude BETWEEN %s AND %s
        ORDER BY timestamp;
    """
    cursor.execute(query, (user.id, sw_lat, ne_lat, sw_lng, ne_lng))
    rows = cursor.fetchall()

    time3 = time.time()

    # **Convert rows into dictionary format**
    gps_data = []
    for row in rows:
        gps_data.append({
            "id": row[0],
            "user_id": row[1],
            "timestamp": row[2].isoformat() if row[2] else None,
            "latitude": row[3],
            "longitude": row[4],
            "horizontal_accuracy": row[5],
            "altitude": row[6],
            "vertical_accuracy": row[7],
            "heading": row[8],
            "heading_accuracy": row[9],
            "speed": row[10],
            "speed_accuracy": row[11],
        })

    # **Reduce data if zoomed out to prevent overload**
    if zoom < 8:  # Example threshold for zoom level
        filtered_data = []
        step = max(1, len(gps_data) // 100)  # Keep ~100 points max
        for i in range(0, len(gps_data), step):
            filtered_data.append(gps_data[i])
        gps_data = filtered_data

    time4 = time.time()

    # **Close database connection**
    cursor.close()
    conn.close()

    res = jsonify(gps_data)

    time5 = time.time()

    print(f"Time taken: {time5 - time0:.3f}s")
    print(f"Time breakdown: {time1 - time0:.3f}s, {time2 - time1:.3f}s, {time3 - time2:.3f}s, {time4 - time3:.3f}s, {time5 - time4:.3f}s")

    return res

@app.route("/logout")
def logout():
    """Clear session to log out."""
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    """Example protected route."""
    return render_template("dashboard.jinja")

@app.route("/manage_users")
@login_required
def manage_users():
    """Admin-only page listing all users."""
    user = get_current_user()
    if not user.is_admin:
        return "Access denied", 403

    users = User.query.all()
    return render_template("manage_users.jinja", users=users)

@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """
    Let the user view or regenerate their API key, and also update their email/password.
    """
    user = get_current_user()

    if request.method == "POST":
        # If user requested a new API key
        if "generate_key" in request.form:
            # Generate a new key (replace any existing)
            new_key = uuid.uuid4().hex
            user.api_key = new_key
            db.session.commit()

        # If user updated email or password
        if "update_account" in request.form:
            new_email = request.form.get("new_email")
            new_password = request.form.get("new_password")

            # Update email if provided
            if new_email:
                # Optional check: ensure not taken by another user, etc.
                user.email = new_email

            # Update password if provided
            if new_password:
                hashed = generate_password_hash(new_password)
                user.password = hashed

            db.session.commit()

        return redirect(url_for("account"))

    return render_template("account.jinja")

#
# ----------------- WSGI Entry Point ----------------- #
#

if __name__ == "__main__":
    from waitress import serve

    # MANUALLY CREATE TABLES & DEFAULT USER
    with app.app_context():
        create_default_user()

    # Start the server
    serve(app, host="0.0.0.0", port=8500)
