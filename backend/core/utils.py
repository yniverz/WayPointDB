import os
import traceback
import flask

from flask import session, redirect, url_for, g
from functools import wraps

import psycopg2
from .models import User, AdditionalTrace
from .config import Config
from .models import GPSData, Import, User, db

def login_required(f):
    """Decorator to ensure the user is logged in (session-based) for HTML routes."""
    def login_with_old_page():
        # path with get parameter
        return redirect(url_for("web.login", next=flask.request.path+"?"+flask.request.query_string.decode("utf-8")))

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return login_with_old_page()
        
        g.current_user = get_current_user()
        if not g.current_user:
            return login_with_old_page()

        g.current_trace = get_current_trace()

        g.trace_query = {"user_id": g.current_user.id}
        if g.current_trace:
            g.trace_query = {"trace_id": g.current_trace.id}



        g.available_traces = []
        for trace in AdditionalTrace.query.all():
            if str(g.current_user.id) in trace.share_with_list or trace.owner_id == g.current_user.id:
                g.available_traces.append(trace)

        return f(*args, **kwargs)
    return decorated_function

def api_key_required(f):
    """Decorator for REST endpoints that requires an API key."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = flask.request.args.get("api_key")
        user = User.get_user_from_api_key(api_key)
        trace = User.get_trace_from_api_key(api_key)

        if not user:
            return {"error": "Invalid or missing API key"}, 401
        
        g.current_user = user
        g.current_trace = trace

        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """Return the currently logged-in user object (or None) using the session."""
    if "user_id" in session:
        try:
            return User.query.get(session["user_id"])
        except:
            print(traceback.format_exc())
    return None

def get_current_trace():
    """Return the currently selected trace object (or None) using the session."""
    if "trace_id" in session:
        try:
            return AdditionalTrace.query.get(session["trace_id"])
        except:
            print(traceback.format_exc())
    return None

def create_default_user():
    """Create a default admin user if none exists."""


    # Code to manually update the database schema
    conn = psycopg2.connect(
        dbname=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASS,
        host=Config.DB_HOST
    )
    cursor = conn.cursor()

    # #user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("user.id"), nullable=True) # change nullable to true
    # cursor.execute("ALTER TABLE gps_data ALTER COLUMN user_id DROP NOT NULL")

    # #trace_id = db.Column(UUID(as_uuid=True), db.ForeignKey("trace.id"), nullable=True)
    # cursor.execute("ALTER TABLE gps_data ADD COLUMN trace_id UUID")

    # # api_key = db.Column(db.String(255), unique=True, nullable=True)
    # api_keys = db.Column(db.JSON, default=[])




    # cursor.execute("ALTER TABLE \"user\" DROP COLUMN api_key")
    # cursor.execute("ALTER TABLE \"user\" ADD COLUMN api_keys JSON DEFAULT '[]'")
    # cursor.execute("ALTER TABLE daily_statistic ALTER COLUMN user_id DROP NOT NULL")
    # cursor.execute("ALTER TABLE daily_statistic ADD COLUMN trace_id UUID")
    # cursor.execute("ALTER TABLE additional_trace ADD COLUMN owner_id UUID NOT NULL")
    # cursor.execute("ALTER TABLE additional_trace RENAME COLUMN user_id_list TO share_with_list")
    # cursor.execute("ALTER TABLE import ALTER COLUMN user_id DROP NOT NULL")
    # cursor.execute("ALTER TABLE import ADD COLUMN trace_id UUID")



    # cursor.execute("UPDATE import SET done_importing = TRUE")

    # cursor.execute("UPDATE \"user\" SET is_admin = FALSE WHERE email NOT LIKE '%admin%'")

    conn.commit()
    cursor.close()
    conn.close()




    db.create_all()

    # make sure the tables conform to the latest schema
    db.session.commit()

    # if no user with is_admin=True exists, create one
    existing_admin = User.query.filter_by(is_admin=True).first()
    if not existing_admin:
        admin_email = "admin@example.com"
        admin_pass = "password"

        user = User(
            email=admin_email,
            is_admin=True
        )
        user.set_password(admin_pass)
        db.session.add(user)
        db.session.commit()

    # existing = User.query.filter_by(email=admin_email).first()
    # if not existing:
    #     admin_email = "admin@example.com"
    #     admin_pass = "password"
    #     user = User(
    #         email=admin_email,
    #         is_admin=True
    #     )
    #     user.set_password(admin_pass)
    #     db.session.add(user)
    #     db.session.commit()

def check_db():
    """Check if the database is empty and create a default user."""

    # check for any files in the upload folder that are not in the database
    for file in os.listdir(Config.UPLOAD_FOLDER):
        if not Import.query.filter_by(filename=file).first():
            os.remove(Config.UPLOAD_FOLDER + "/" + file)

    # check all import ids in the gps data and if no import for that id exists, add it
    import_ids = db.session.query(GPSData.import_id).distinct().all()
    for (import_id,) in import_ids:
        if not Import.query.filter_by(id=import_id).first():
            # get the userid associated with the import_id
            # user_id = GPSData.query.filter_by(import_id=import_id).first().user_id
            # total_entries = GPSData.query.filter_by(import_id=import_id).count()

            data = GPSData.query.filter_by(import_id=import_id)
            total_entries = data.count()
            user_id = data.first().user_id

            import_obj = Import(
                user_id=user_id,
                filename="",
                total_entries=total_entries
            )

    
    for import_obj in Import.query.all():
        if import_obj.filename == "":
            continue

        # make sure file exists
        if not os.path.exists(Config.UPLOAD_FOLDER + "/" + import_obj.filename):
            db.session.delete(import_obj)
            db.session.commit()
            continue