from collections import defaultdict
from datetime import datetime
import uuid
import time
import psycopg2
from flask import (
    Blueprint, render_template, request, redirect, url_for, 
    session, g, jsonify
)
from flask.views import MethodView

from ..background.jobs import JOB_TYPES, GenerateFullStatisticsJob
from ..background import job_manager
from ..models import MonthlyStatistic, User, GPSData, db
from ..utils import login_required, get_current_user
from ..config import Config

web_bp = Blueprint("web", __name__)

class HomeView(MethodView):
    def get(self):
        """Simple home route."""
        user = get_current_user()
        if user:
            return redirect(url_for("web.map"))
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

# class DashboardView(MethodView):
#     decorators = [login_required]

#     def get(self):
#         """Example protected dashboard."""
#         return render_template("dashboard.jinja")
    
class JobsView(MethodView):
    decorators = [login_required]

    def get(self):
        """Example protected dashboard."""

        return render_template("jobs.jinja", jobs=job_manager.get_jobs(), time_now=time.time(), photon_active=len(Config.PHOTON_SERVER_HOST) != 0)
    
    def post(self):

        job_name = request.form.get("jobName")

        if job_name not in JOB_TYPES:
            return "Invalid job name", 405
        
        
        job = JOB_TYPES[job_name]

        for param in job.PARAMETERS:
            if param == "user":
                continue

            if param not in request.form:
                return f"Missing parameter: {param}", 401
        
        needed_params = set(job.PARAMETERS) - {"user"}
            
        job_instance = job(user=get_current_user(), **{param: request.form[param] for param in needed_params})
        
        job_manager.add_job(job_instance)

        return redirect(url_for("web.jobs"))



    
class StatsView(MethodView):
    decorators = [login_required]

    def get(self):
        user = get_current_user()

        stats = MonthlyStatistic.query.filter_by(user_id=user.id).all()

        # We'll group stats by year
        stats_by_year = defaultdict(lambda: {
            "monthly_distances": [0.0] * 12,  # 12 months
            "cities": set(),
            "countries": set(),
            "total_distance": 0.0
        })

        for stat in stats:
            year = stat.year
            # stat.month is a date; we want the month index [0..11]
            month_idx = stat.month - 1  # January -> 0, etc.

            # Accumulate distances (in meters). We'll convert to km later.
            stats_by_year[year]["monthly_distances"][month_idx] += stat.total_distance_m

            # Update visited cities / countries (they are stored in JSON columns)
            if stat.visited_cities:
                stats_by_year[year]["cities"].update(stat.visited_cities)
            if stat.visited_countries:
                stats_by_year[year]["countries"].update(stat.visited_countries)

            # Track total distance in meters for each year
            stats_by_year[year]["total_distance"] += stat.total_distance_m

        # Collect overall unique sets across **all** years
        all_cities = set()
        all_countries = set()

        # Convert sets to lists for JSON serialization; also convert meters -> km
        stats_by_year_processed = {}
        for year, data in sorted(stats_by_year.items(), reverse=True):
            all_cities.update(data["cities"])
            all_countries.update(data["countries"])

            stats_by_year_processed[year] = {
                "monthly_distances": [dist_m / 1000 for dist_m in data["monthly_distances"]],
                "cities": list(data["cities"]),
                "countries": list(data["countries"]),
                "total_distance": data["total_distance"] / 1000.0,  # store in KM
            }

        print(stats)
        print(stats_by_year)
        print(stats_by_year_processed)
        print(all_cities)
        print(all_countries)

        return render_template(
            "stats.jinja",
            stats_by_year=stats_by_year_processed,   # Dict of years → aggregated data
            total_cities=list(all_cities),
            total_countries=list(all_countries),
        )
    
class MapView(MethodView):
    decorators = [login_required]

    def get(self):
        user = get_current_user()
        last_point = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp.desc()).first()
        if not last_point:
            # Some default coords
            last_point = {"latitude": 52.516310, "longitude": 13.378208}
        return render_template("map.jinja", last_point=last_point)
    
    def post(self):
        """Fetch GPS data using psycopg2 and return JSON."""

        data: dict = request.json
        ne_lat = data.get("ne_lat")
        ne_lng = data.get("ne_lng")
        sw_lat = data.get("sw_lat")
        sw_lng = data.get("sw_lng")
        zoom = data.get("zoom", 10)
        start_date = data.get("start_date")
        end_date = data.get("end_date")

        if None in [ne_lat, ne_lng, sw_lat, sw_lng]:
            return jsonify({"error": "Invalid or missing bounds"}), 400
        
        ne_lat += 0.01
        ne_lng += 0.01
        sw_lat -= 0.01
        sw_lng -= 0.01

        user = get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        
        time1 = time.time()

        conn = psycopg2.connect(
            dbname=Config.DB_NAME,
            user=Config.DB_USER,
            password=Config.DB_PASS,
            host=Config.DB_HOST
        )
        cursor = conn.cursor()

        max_points_count = 2000
        date_filter = ""

        if end_date:
            end_date = end_date + " 23:59:59"
        
        if start_date and end_date:
            date_filter = f"AND timestamp BETWEEN '{start_date}' AND '{end_date}'"
        elif start_date:
            date_filter = f"AND timestamp >= '{start_date}'"
        elif end_date:
            date_filter = f"AND timestamp <= '{end_date}'"

        # calculate time delta
        time_delta = 0
        if start_date and end_date:
            time_delta = (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).total_seconds()
        
        if time_delta != 0 and time_delta < 60 * 60 * 24 * 3:
            query = f"""
                WITH filtered_data AS (
                    SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                        altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
                        ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num
                    FROM gps_data
                    WHERE user_id = {user.id}
                    {date_filter}
                ),
                row_count AS (
                    SELECT COUNT(*) AS total FROM filtered_data
                )
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM filtered_data
                WHERE row_num % (SELECT GREATEST(1, total / {max_points_count}) FROM row_count) = 1
                OR (SELECT total FROM row_count) < {max_points_count}
                ORDER BY timestamp;
            """

        elif zoom > 18:
            query = f"""
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM gps_data
                WHERE user_id = {user.id}
                AND latitude BETWEEN {sw_lat} AND {ne_lat}
                AND longitude BETWEEN {sw_lng} AND {ne_lng}
                {date_filter}
                ORDER BY timestamp;
            """
        else:
            query = f"""
                WITH filtered_data AS (
                    SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                        altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
                        ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num
                    FROM gps_data
                    WHERE user_id = {user.id}
                    AND latitude BETWEEN {sw_lat} AND {ne_lat}
                    AND longitude BETWEEN {sw_lng} AND {ne_lng}
                    {date_filter}
                ),
                row_count AS (
                    SELECT COUNT(*) AS total FROM filtered_data
                )
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM filtered_data
                WHERE row_num % (SELECT GREATEST(1, total / {max_points_count}) FROM row_count) = 1
                OR (SELECT total FROM row_count) < {max_points_count}
                ORDER BY timestamp;
            """

        cursor.execute(query)

        time2 = time.time()

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
                "ha2": row[9],
                "s": row[10],
                "sa": row[11],
            })

        cursor.close()
        conn.close()

        print(f"/gps_data: Time to data: {time2 - time1:.3f}s")

        return jsonify(gps_data)

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
    
    def post(self):
        user = get_current_user()
        if not user.is_admin:
            return "Access denied", 403
        
        action = request.form.get("action")

        print(request.form)

        if action == "remove_user":
            user_id = request.form.get("user_id")

            if not user_id:
                return "Missing user_id", 400
            
            if user_id == user.id:
                return "Cannot delete yourself", 400
            
            user = User.query.filter_by(id=user_id).first()
            if not user:
                return "User not found", 404
            db.session.delete(user)
            db.session.commit()

        elif action == "add_user":
            email = request.form.get("email")
            password = request.form.get("password")
            is_admin = "is_admin" in request.form

            if not email or not password:
                return "Missing email or password", 400

            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                return "User already exists", 400

            new_user = User(email=email, is_admin=is_admin)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()

        return redirect(url_for("web.manage_users"))

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
web_bp.add_url_rule("/jobs", view_func=JobsView.as_view("jobs"), methods=["GET", "POST"])
web_bp.add_url_rule("/login", view_func=LoginView.as_view("login"), methods=["GET", "POST"])
web_bp.add_url_rule("/logout", view_func=LogoutView.as_view("logout"))
# web_bp.add_url_rule("/dashboard", view_func=DashboardView.as_view("dashboard"))
web_bp.add_url_rule("/map", view_func=MapView.as_view("map"), methods=["GET", "POST", "DELETE"])
web_bp.add_url_rule("/stats", view_func=StatsView.as_view("stats"))
web_bp.add_url_rule("/manage_users", view_func=ManageUsersView.as_view("manage_users"), methods=["GET", "POST"])
web_bp.add_url_rule("/account", view_func=AccountView.as_view("account"), methods=["GET", "POST"])
