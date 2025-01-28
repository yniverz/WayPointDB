import uuid
import time
import psycopg2
from flask import (
    Blueprint, render_template, request, redirect, url_for, 
    session, g, jsonify
)
from flask.views import MethodView
from ..models import User, GPSData, db
from ..utils import login_required, get_current_user
from ..config import Config

web_bp = Blueprint("web", __name__)

class HomeView(MethodView):
    def get(self):
        """Simple home route."""
        user = get_current_user()
        if user:
            return redirect(url_for("web.dashboard"))
        return redirect(url_for("web.login"))

class LoginView(MethodView):
    def get(self):
        return render_template("login.jinja")

    def post(self):
        email = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            session["user_id"] = user.id
            return redirect(url_for("web.home"))
        return "Invalid credentials", 401

class LogoutView(MethodView):
    def get(self):
        session.clear()
        return redirect(url_for("web.login"))

class DashboardView(MethodView):
    decorators = [login_required]

    def get(self):
        """Example protected dashboard."""
        return render_template("dashboard.jinja")

class MapView(MethodView):
    decorators = [login_required]

    def get(self):
        user = get_current_user()
        last_point = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp.desc()).first()
        if not last_point:
            # Some default coords
            last_point = {"latitude": 52.516310, "longitude": 13.378208}
        return render_template("map.jinja", last_point=last_point)

class GPSDataView(MethodView):
    decorators = [login_required]

    def get(self):
        """Fetch GPS data using psycopg2 and return JSON."""
        time0 = time.time()

        ne_lat = request.args.get("ne_lat", type=float)
        ne_lng = request.args.get("ne_lng", type=float)
        sw_lat = request.args.get("sw_lat", type=float)
        sw_lng = request.args.get("sw_lng", type=float)
        zoom = request.args.get("zoom", type=int, default=10)

        if None in [ne_lat, ne_lng, sw_lat, sw_lng]:
            return jsonify({"error": "Invalid or missing bounds"}), 400

        user = get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401

        conn = psycopg2.connect(
            dbname=Config.DB_NAME,
            user=Config.DB_USER,
            password=Config.DB_PASS,
            host=Config.DB_HOST
        )
        cursor = conn.cursor()
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
        gps_data = []
        for row in rows:
            gps_data.append({
                "id": row[0],
                "uid": row[1],
                "t": row[2].isoformat() if row[2] else None,
                "lat": row[3],
                "lon": row[4],
                "ha": row[5],
                "a": row[6],
                "va": row[7],
                "h": row[8],
                "ha2": row[9],  # note: changed key since we used "ha" above
                "s": row[10],
                "sa": row[11],
            })

        # Reduce data if zoomed out
        if zoom < 12:
            filtered_data = []
            step = max(1, len(gps_data) // 200)
            for i in range(0, len(gps_data), step):
                filtered_data.append(gps_data[i])
            gps_data = filtered_data

        cursor.close()
        conn.close()
        return jsonify(gps_data)

class DeletePointView(MethodView):
    decorators = [login_required]

    def delete(self):
        user = get_current_user()
        if not user:
            return "Unauthorized", 401
        point_id = request.args.get("id")
        if not point_id:
            return "Missing point_id", 400
        point = GPSData.query.filter_by(id=point_id, user_id=user.id).first()
        if not point:
            return "Point not found", 404
        db.session.delete(point)
        db.session.commit()
        return "OK", 200

class ManageUsersView(MethodView):
    decorators = [login_required]

    def get(self):
        user = get_current_user()
        if not user.is_admin:
            return "Access denied", 403
        users = User.query.all()
        return render_template("manage_users.jinja", users=users)

class AccountView(MethodView):
    decorators = [login_required]

    def get(self):
        return render_template("account.jinja")

    def post(self):
        user = get_current_user()
        if "generate_key" in request.form:
            # Generate new API key
            user.api_key = uuid.uuid4().hex
            db.session.commit()

        if "update_account" in request.form:
            new_email = request.form.get("new_email")
            new_password = request.form.get("new_password")
            if new_email:
                user.email = new_email
            if new_password:
                user.set_password(new_password)
            db.session.commit()
        return redirect(url_for("web.account"))

# Register the class-based views with the Blueprint
web_bp.add_url_rule("/", view_func=HomeView.as_view("home"))
web_bp.add_url_rule("/login", view_func=LoginView.as_view("login"), methods=["GET", "POST"])
web_bp.add_url_rule("/logout", view_func=LogoutView.as_view("logout"))
web_bp.add_url_rule("/dashboard", view_func=DashboardView.as_view("dashboard"))
web_bp.add_url_rule("/map", view_func=MapView.as_view("map"))
web_bp.add_url_rule("/gps_data", view_func=GPSDataView.as_view("gps_data"))
web_bp.add_url_rule("/delete_point", view_func=DeletePointView.as_view("delete_point"), methods=["DELETE"])
web_bp.add_url_rule("/manage_users", view_func=ManageUsersView.as_view("manage_users"))
web_bp.add_url_rule("/account", view_func=AccountView.as_view("account"), methods=["GET", "POST"])
