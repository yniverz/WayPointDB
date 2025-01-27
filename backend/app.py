import os
import uuid
from functools import wraps
from datetime import datetime

from flask import (
    Flask,
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
from werkzeug.security import generate_password_hash, check_password_hash

#
# ----------------- Flask App Setup ----------------- #
#

app = Flask(__name__)
# Generate a random secret key for session encryption
app.config["SECRET_KEY"] = uuid.uuid4().hex

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

def login_required(f):
    """Decorator to ensure the user is logged in (session-based) for HTML routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

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
    The key must be passed in request header:  X-API-KEY: <value>
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        # api_key = request.headers.get("X-API-KEY")
        api_key = flask.request.args.get("api_key")
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
    title="GPS Tracker API",
    description="API documentation for GPS tracking",
    prefix="/api",
    doc="/api/docs"
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

batch_gps_model = api.model("BatchGPSData", {
    "gps_data": fields.List(fields.Nested(gps_model), required=True, description="List of GPS data points"),
    "api_key": fields.String(required=True, description="API key for authentication")
})

@ns.route("/batch")
class GPSBatch(Resource):
    def get(self):
        return "Please use POST to submit GPS data", 405

    @api.expect(batch_gps_model)
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
    """Simple home route. If logged in, show dashboard, else prompt login."""
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
            return redirect(url_for("dashboard"))
        else:
            return "Invalid credentials", 401

    return render_template("login.jinja")

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

    return render_template("account.jinja", user=user)

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
