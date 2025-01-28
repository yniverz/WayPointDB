from collections import defaultdict
import uuid
import time
import psycopg2
from flask import (
    Blueprint, render_template, request, redirect, url_for, 
    session, g, jsonify
)
from flask.views import MethodView

from ..background.jobs import GenerateFullStatisticsJob
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
    
class JobsView(MethodView):
    decorators = [login_required]

    def get(self):
        """Example protected dashboard."""

        return render_template("jobs.jinja", jobs = job_manager.get_jobs())
    
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
        for year, data in sorted(stats_by_year.items()):
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

class GPSDataView(MethodView):
    decorators = [login_required]

    def get(self):
        """Fetch GPS data using psycopg2 and return JSON."""

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
        
        time1 = time.time()

        conn = psycopg2.connect(
            dbname=Config.DB_NAME,
            user=Config.DB_USER,
            password=Config.DB_PASS,
            host=Config.DB_HOST
        )
        cursor = conn.cursor()


        max_points_count = 1000

        if zoom > 18:
            query = f"""
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM gps_data
                WHERE user_id = {user.id}
                AND latitude BETWEEN {sw_lat} AND {ne_lat}
                AND longitude BETWEEN {sw_lng} AND {ne_lng}
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
                "ha2": row[9],  # note: changed key since we used "ha" above
                "s": row[10],
                "sa": row[11],
            })

        cursor.close()
        conn.close()

        print(f"/gps_data: Time to data: {time2 - time1:.3f}s")

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
web_bp.add_url_rule("/jobs", view_func=JobsView.as_view("jobs"))
web_bp.add_url_rule("/login", view_func=LoginView.as_view("login"), methods=["GET", "POST"])
web_bp.add_url_rule("/logout", view_func=LogoutView.as_view("logout"))
web_bp.add_url_rule("/dashboard", view_func=DashboardView.as_view("dashboard"))
web_bp.add_url_rule("/map", view_func=MapView.as_view("map"))
web_bp.add_url_rule("/stats", view_func=StatsView.as_view("stats"))
web_bp.add_url_rule("/gps_data", view_func=GPSDataView.as_view("gps_data"))
web_bp.add_url_rule("/delete_point", view_func=DeletePointView.as_view("delete_point"), methods=["DELETE"])
web_bp.add_url_rule("/manage_users", view_func=ManageUsersView.as_view("manage_users"))
web_bp.add_url_rule("/account", view_func=AccountView.as_view("account"), methods=["GET", "POST"])
