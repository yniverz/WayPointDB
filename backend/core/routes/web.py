from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
import os
import re
import traceback
import uuid
import time
import psycopg2
import json
from flask import (
    Blueprint, render_template, request, redirect, url_for, 
    session, g, jsonify
)
from flask.views import MethodView
import requests
from sqlalchemy import func

from ..background.jobs import JOB_TYPES, GenerateFullStatisticsJob, ImportJob
from ..background import job_manager
from ..models import DailyStatistic, Import, User, GPSData, db
from ..utils import login_required
from ..config import Config
from werkzeug.utils import secure_filename

web_bp = Blueprint("web", __name__)

class HomeView(MethodView):
    decorators = [login_required]

    def get(self):
        """Simple home route."""
        
        return redirect(url_for("web.map"))

class LoginView(MethodView):
    def get(self):
        return render_template("login.jinja")

    def post(self):
        email = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            session["user_id"] = user.id

            dest = request.args.get('next')
            if dest:
                return redirect(dest)
            
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


        raw_jobs = job_manager.get_jobs()
        jobs = []
        for job in raw_jobs:
            start_time = job[4] or time.time()
            progress = job[3] * 100
            safe_progress = max(0.01, progress)

            time_passed = time.time() - start_time
            time_left = max(0, (100 - job[3]) * ((time.time() - start_time) / safe_progress) - time_passed)

            time_passed_str = time.strftime("%H:%M:%S", time.gmtime(time_passed))
            if time_passed > 60*60*24:
                time_passed_str = f"{time_passed // (60*60*24):.0f}d " + time_passed_str
            
            time_left_str = time.strftime("%H:%M:%S", time.gmtime(time_left))
            if time_left > 60*60*24:
                time_left_str = f"{time_left // (60*60*24):.0f}d " + time_left_str
            
            jobs.append((
                job[0], 
                job[1], 
                job[2], 
                progress, 
                time_passed_str,
                time_left_str
            ))


        if request.args.get("update"):
            return jsonify(jobs)

        return render_template("jobs.jinja", jobs=jobs, photon_active=len(Config.PHOTON_SERVER_HOST) != 0)
    
    def post(self):

        if "newJob" in request.form:
            return self.do_new_job(request.form)

        if "cancelJob" in request.form:
            return self.do_cancel_job(request.form)
        
        return "Invalid request", 405

    def do_cancel_job(self, params: dict):
        job_id = params.get("cancelJob")
        job_manager.cancel_job(job_id, blocking=True)

        return redirect(url_for("web.jobs"))

    def do_new_job(self, params: dict):
        job_name = params.get("newJob")
        
        if job_name not in JOB_TYPES:
            return "Invalid job name", 405
        
        
        job = JOB_TYPES[job_name]

        for param in job.PARAMETERS:
            if param == "user":
                continue

            if param not in params:
                return f"Missing parameter: {param}", 401
        
        needed_params = set(job.PARAMETERS) - {"user"}
            
        job_instance = job(user=g.current_user, **{param: job.PARAMETERS[param](params[param]) for param in needed_params})
        
        job_manager.add_job(job_instance)

        return redirect(url_for("web.jobs"))



class StatsView(MethodView):
    decorators = [login_required]

    def get(self):
        user = g.current_user

        total_points = GPSData.query.filter_by(user_id=user.id).count()
        # total_geocoded = GPSData.query.filter_by(user_id=user.id, reverse_geocoded=True).count()
        total_geocoded = GPSData.query.filter_by(user_id=user.id).filter(GPSData.reverse_geocoded == True).count()
        # where reverse_geocoded = true and city is none
        total_not_geocoded = GPSData.query.filter_by(user_id=user.id).filter(GPSData.reverse_geocoded == True).filter(GPSData.country == None).count()

        stats: list[DailyStatistic] = DailyStatistic.query.filter_by(user_id=user.id).all()

        # We'll group stats by year
        stats_by_year = defaultdict(lambda: {
            "monthly_distances": [0.0] * 12,  # 12 months
            "cities": set(),
            "countries": set(),
            "total_distance": 0.0
        })

        for stat in stats:
            year = stat.year
            month_idx = stat.month - 1  # January -> 0, etc.

            # Accumulate distances (in meters). We'll convert to km later.
            stats_by_year[year]["monthly_distances"][month_idx] += stat.total_distance_m

            # Update visited cities / countries (they are stored in JSON columns)
            if stat.visited_cities:

                stats_by_year[year]["cities"].update([tuple(city) for city in stat.visited_cities])
            if stat.visited_countries:
                stats_by_year[year]["countries"].update(stat.visited_countries)

            # Track total distance in meters for each year
            stats_by_year[year]["total_distance"] += stat.total_distance_m

        # Collect overall unique sets across **all** years
        all_cities = set()
        all_countries = set()
        total_distance = 0.0

        # Convert sets to lists for JSON serialization; also convert meters -> km
        stats_by_year_processed = {}
        for year, data in sorted(stats_by_year.items(), reverse=True):
            all_cities.update(data["cities"])
            all_countries.update(data["countries"])
            total_distance += data["total_distance"]

            stats_by_year_processed[year] = {
                "monthly_distances": [int(dist_m / 1000) for dist_m in data["monthly_distances"]],
                "cities": list(data["cities"]),
                "countries": list(data["countries"]),
                "total_distance": int(data["total_distance"] / 1000.0),  # store in KM
            }

        return render_template(
            "stats.jinja",
            stats_by_year=stats_by_year_processed,   # Dict of years → aggregated data
            total_cities=sorted(list(all_cities)),
            total_countries=sorted(list(all_countries)),
            total_distance=f"{total_distance / 1000.0:,.0f}",  # Convert to KM
            total_points=f"{total_points:,}",
            is_photon_connected=len(Config.PHOTON_SERVER_HOST) != 0,
            total_geocoded=f"{total_geocoded:,}",
            total_not_geocoded=f"{total_not_geocoded:,}",
            MIN_COUNTRY_VISIT_DURATION_FOR_STATS=self.formatTimeDelta(Config.MIN_COUNTRY_VISIT_DURATION_FOR_STATS),
            MIN_CITY_VISIT_DURATION_FOR_STATS=self.formatTimeDelta(Config.MIN_CITY_VISIT_DURATION_FOR_STATS)
        )
    
    def formatTimeDelta(self, seconds: int):
        
        if seconds < 60:
            return f"{seconds} second" + ("s" if seconds > 1 else "")
        elif seconds < 60 * 60:
            return f"{seconds // 60} minute" + ("s" if seconds // 60 > 1 else "")
        elif seconds < 60 * 60 * 24:
            return f"{seconds // (60 * 60)} hour" + ("s" if seconds // (60 * 60) > 1 else "")
        else:
            return f"{seconds // (60 * 60 * 24)} day" + ("s" if seconds // (60 * 60 * 24) > 1 else "")
    

class YearlyStatsView(MethodView):
    decorators = [login_required]

    def get(self, year):
        user = g.current_user

        if not year:
            return "Missing year", 400
        
        total_points = GPSData.query.filter_by(user_id=user.id).filter(func.extract("year", GPSData.timestamp) == year).count()

        stats = DailyStatistic.query.filter_by(user_id=user.id, year=year).all()

        # We'll group stats by month
        stats_by_month = defaultdict(lambda: {
            "monthly_distances": [0.0] * 31,  # 31 days
            "cities": set(),
            "countries": set(),
            "total_distance": 0.0
        })

        for stat in stats:
            month = stat.month
            day = stat.day

            # Accumulate distances (in meters). We'll convert to km later.
            stats_by_month[month]["monthly_distances"][day - 1] = stat.total_distance_m

            # Update visited cities / countries (they are stored in JSON columns)
            if stat.visited_cities:
                stats_by_month[month]["cities"].update([tuple(city) for city in stat.visited_cities])
            if stat.visited_countries:
                stats_by_month[month]["countries"].update(stat.visited_countries)

            # Track total distance in meters for each month
            stats_by_month[month]["total_distance"] += stat.total_distance_m

        # Collect overall unique sets across **all** months
        all_cities = set()
        all_countries = set()
        total_distance = 0.0

        # Convert sets to lists for JSON serialization; also convert meters -> km
        stats_by_month_processed = {}
        for month, data in sorted(stats_by_month.items()):
            all_cities.update(data["cities"])
            all_countries.update(data["countries"])
            total_distance += data["total_distance"]

            stats_by_month_processed[month] = {
                "monthly_distances": [int(dist_m / 1000) for dist_m in data["monthly_distances"]],
                "cities": list(data["cities"]),
                "countries": list(data["countries"]),
                "total_distance": int(data["total_distance"] / 1000.0),  # store in KM
            }

        return render_template(
            "stats_yearly.jinja",
            year=year,
            stats_by_month=stats_by_month_processed,   # Dict of months → aggregated data
            total_cities=sorted(list(all_cities)),
            total_countries=sorted(list(all_countries)),
            total_distance=f"{total_distance / 1000.0:,.0f}",  # Convert to KM
            total_points=f"{total_points:,}",
            is_photon_connected=len(Config.PHOTON_SERVER_HOST) != 0,
        )


# class PointsView(MethodView):
#     decorators = [login_required]  # or your own login decorator

#     def get(self):
#         user = g.current_user

#         # Single-date parameter
#         the_date_str = request.args.get("date")
#         import_id = request.args.get("import")
#         now = datetime.now()

#         if not the_date_str:
#             # Default to today
#             start_date = datetime(now.year, now.month, now.day, 0, 0, 0)
#         else:
#             # Parse user-provided date
#             try:
#                 parsed = datetime.strptime(the_date_str, "%Y-%m-%d")
#                 start_date = datetime(parsed.year, parsed.month, parsed.day, 0, 0, 0)
#             except ValueError:
#                 # Fallback to today if error
#                 start_date = datetime(now.year, now.month, now.day, 0, 0, 0)

#         end_date = start_date + timedelta(hours=24)

#         # Query user’s points from start_date to end_date
#         points_query = GPSData.query.filter_by(user_id=user.id).filter(
#             GPSData.timestamp >= start_date,
#             GPSData.timestamp < end_date
#         )

#         if import_id:
#             points_query = points_query.filter_by(import_id=import_id)

#         # Sort newest -> oldest
#         points = points_query.order_by(GPSData.timestamp.desc()).all()

#         raw_imports = Import.query.filter_by(user_id=user.id).all()

#         imports = []
#         for imp in raw_imports:
#             imports.append({
#                 "id": imp.id,
#                 "name": imp.original_filename,
#             })

#         return render_template("points.jinja", points=points, imports=imports)

#     def post(self):
#         user = g.current_user

#         action = request.form.get("action", None)

#         # if action == "delete_point":
#         #     # Single-delete logic
#         #     point_id = request.form.get("point_id")
#         #     if not point_id:
#         #         return "Missing point_id", 400

#         #     point = GPSData.query.filter_by(id=point_id, user_id=user.id).first()
#         #     if not point:
#         #         return "Point not found or not yours", 404

#         #     db.session.delete(point)
#         #     db.session.commit()

#         if action == "batch_delete":
#             # Batch delete: get all selected point IDs
#             selected_ids = request.form.getlist("selected_points")
#             if selected_ids:
#                 # Convert to int if needed
#                 selected_ids = [int(x) for x in selected_ids if x.isdigit()]
#                 # Delete all points matching these IDs for this user
#                 GPSData.query.filter(
#                     GPSData.user_id == user.id,
#                     GPSData.id.in_(selected_ids)
#                 ).delete(synchronize_session=False)
#                 db.session.commit()

#         # Keep the date param in the redirect so we stay on the same day
#         return redirect(url_for("web.points", date=request.args.get("date", "")))

class PointsView(MethodView):
    decorators = [login_required]  # Ensure the user is logged in

    def get(self):
        user = g.current_user

        # Retrieve query parameters
        the_date_str = request.args.get("date")
        import_id = request.args.get("import")
        page = request.args.get("page", 1)
        per_page = request.args.get("per_page", 100)

        # Validate and parse 'page'
        try:
            page = int(page)
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        # Validate and parse 'per_page'
        try:
            per_page = int(per_page)
            if per_page not in [10, 20, 50, 150]:
                per_page = 20
        except ValueError:
            per_page = 20

        now = datetime.now()

        if not the_date_str:
            # Default to today
            start_date = datetime(now.year, now.month, now.day, 0, 0, 0)
        else:
            # Parse user-provided date
            try:
                parsed = datetime.strptime(the_date_str, "%Y-%m-%d")
                start_date = datetime(parsed.year, parsed.month, parsed.day, 0, 0, 0)
            except ValueError:
                # Fallback to today if error
                start_date = datetime(now.year, now.month, now.day, 0, 0, 0)

        end_date = start_date + timedelta(days=1)  # Use days=1 for clarity

        # Build the base query
        points_query = GPSData.query.filter_by(user_id=user.id).filter(
            GPSData.timestamp >= start_date,
            GPSData.timestamp < end_date
        )

        if import_id:
            points_query = points_query.filter_by(import_id=import_id)

        # Calculate total points for pagination
        total_points = points_query.count()

        # Calculate max_pages with ceiling division
        max_pages = math.ceil(total_points / per_page) if total_points > 0 else 1

        # Ensure the current page isn't beyond the maximum
        if page > max_pages:
            page = max_pages

        # Calculate offset
        offset = (page - 1) * per_page

        # Fetch points for the current page, sorted newest to oldest
        points = points_query.order_by(GPSData.timestamp.desc()).offset(offset).limit(per_page).all()

        # Fetch imports for the selector
        raw_imports = Import.query.filter_by(user_id=user.id).all()

        imports = []
        for imp in raw_imports:
            imports.append({
                "id": imp.id,
                "name": imp.original_filename,
            })

        return render_template(
            "points.jinja",
            points=points,
            imports=imports,
            current_page=page,
            per_page=per_page,
            max_pages=max_pages
        )

    def post(self):
        user = g.current_user

        action = request.form.get("action", None)

        if action == "batch_delete":
            # Batch delete: get all selected point IDs
            selected_ids = request.form.getlist("selected_points")
            if selected_ids:
                # Convert to integers and filter out invalid entries
                try:
                    selected_ids = [int(x) for x in selected_ids if x.isdigit()]
                except ValueError:
                    selected_ids = []

                if selected_ids:
                    # Delete all points matching these IDs for this user
                    GPSData.query.filter(
                        GPSData.user_id == user.id,
                        GPSData.id.in_(selected_ids)
                    ).delete(synchronize_session=False)
                    db.session.commit()

        # Retrieve current query parameters to maintain state after action
        the_date_str = request.args.get("date", "")
        import_id = request.args.get("import", "")
        page = request.args.get("page", 1)
        per_page = request.args.get("per_page", 100)

        return redirect(url_for(
            "web.points",
            date=the_date_str,
            importid=import_id,
            page=page,
            per_page=per_page
        ))



class ImportsView(MethodView):
    decorators = [login_required]
    ALLOWED_EXTENSIONS = {"json"}

    def get(self):
        user = g.current_user

        # Fetch all 'Import' records for this user
        raw_imports: list[Import] = Import.query.filter_by(user_id=user.id).order_by(Import.created_at.desc()).all()

        imports = []
        for imp in raw_imports:
            imports.append({
                "id": imp.id,
                "name": imp.original_filename,
                "created_at": imp.created_at,
                "total_entries": imp.total_entries,
                "total_imported": GPSData.query.filter_by(user_id=user.id, import_id=imp.id).count(),
                "done_importing": imp.done_importing,
            })


        return render_template("imports.jinja", imports=imports)

    def post(self):
        user = g.current_user

        action = request.form.get("action")
        if not action:
            return "Missing action", 400

        if action == "upload_json":
            return self.upload_json_file(user)
        elif action == "start_import":
            return self.start_import_job(user)
        elif action == "delete_import":
            return self.delete_import(user)
        else:
            return "Unknown action", 400

    def upload_json_file(self, user):
        """Handle the form submission where a user uploads a JSON file."""
        file = request.files.get("json_file")
        if not file or file.filename == "":
            return "No file selected", 400

        # Optional check for allowed extension
        filename = secure_filename(file.filename)
        name = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        if ext.replace(".", "") not in self.ALLOWED_EXTENSIONS:
            return "Invalid file type. Only .json allowed.", 400

        # Save file to disk with a unique name to avoid collisions
        # Example: "user_<filename>_<user_id>_<uuid>.json"
        unique_name = f"user_{name}_{user.id}_{uuid.uuid4()}{ext}"
        save_path = os.path.join(Config.UPLOAD_FOLDER, unique_name)
        file.save(save_path)

        # parse file
        with open(save_path, "r") as f:
            data = f.read()

        try:
            json_data = json.loads(data)
        except json.JSONDecodeError:
            return "Invalid JSON file", 400
        

        # Create a new Import record
        new_import = Import(
            user_id=user.id,
            filename=unique_name,
            original_filename=filename,
            created_at=datetime.now(timezone.utc),
            total_entries=len(json_data)
        )
        db.session.add(new_import)
        db.session.commit()

        # Redirect back to /imports
        return redirect(url_for("web.imports"))

    def start_import_job(self, user):
        """Queue a background job to parse and insert data from the specified import."""
        import_id = request.form.get("import_id")
        if not import_id:
            return "Missing import_id", 400

        import_record = Import.query.filter_by(id=import_id, user_id=user.id).first()
        if not import_record:
            return "Import not found or not yours", 404

        # Add job to job manager
        # The job itself (ImportJob) will handle reading the file from disk,
        # validating JSON, inserting GPSData, etc.
        job_instance = ImportJob(user=user, import_obj=import_record)
        job_manager.add_job(job_instance)

        return redirect(url_for("web.imports"))

    def delete_import(self, user):
        """Deletes an import record AND all associated GPSData. Asks user for confirmation in the HTML form."""
        import_id = request.form.get("import_id")
        if not import_id:
            return "Missing import_id", 400

        import_record = Import.query.filter_by(id=import_id, user_id=user.id).first()
        if not import_record:
            return "Import not found or not yours", 404

        # First delete associated GPSData by this import_id
        # Note the "import_id" in GPSData is a string field, so match accordingly
        GPSData.query.filter_by(user_id=user.id, import_id=str(import_record.id)).delete(synchronize_session=False)

        # Remove the import record itself
        db.session.delete(import_record)
        db.session.commit()

        # Optional: remove the file from disk
        file_path = os.path.join(Config.UPLOAD_FOLDER, import_record.filename)
        if os.path.exists(file_path):
            os.remove(file_path)

        return redirect(url_for("web.imports"))



class MapView(MethodView):
    decorators = [login_required]

    def get_geocode(self, query):
        url = "https://" if Config.PHOTON_SERVER_HTTPS else "http://"
        url += f"{Config.PHOTON_SERVER_HOST}/api/?q={query}&limit=1"
        res = requests.get(url, headers={"X-Api-Key": Config.PHOTON_SERVER_API_KEY})
        return res.json()

    def get(self):
        user = g.current_user

        if request.args.get("q") and Config.PHOTON_SERVER_HOST:
            try:
                res = self.get_geocode(request.args.get("q"))

                if res.get("features"):
                    feature = res["features"][0]
                    geometry = feature.get("geometry", {})
                    coords = geometry.get("coordinates", [])
                    if len(coords) == 2:
                        return redirect(url_for("web.map")+f"?lat={coords[1]}&lng={coords[0]}&zoom=13")
            except Exception as e:
                traceback.print_exc()
                pass
                

        point_id = request.args.get("point_id")

        if point_id:
            last_point = GPSData.query.filter_by(id=point_id, user_id=user.id).first()
        else:
            last_point = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp.desc()).first()

        if not last_point:
            # Some default coords
            last_point = {"id": -1, "lat": 52.516310, "lon": 13.378208}

        else:
            last_point = {
                "id": last_point.id,
                "lat": last_point.latitude,
                "lng": last_point.longitude
            }

        return render_template("map.jinja", last_point=last_point)

    def is_valid_date_format(self, s):
        pattern = r"^\d{4}-\d{2}-\d{2}$"
        if not bool(re.fullmatch(pattern, s)):
            raise ValueError("Invalid date format")
        
        return s
    
    def post(self):
        """Fetch GPS data using psycopg2 and return JSON."""

        data: dict = request.json

        try:
            ne_lat = float(data.get("ne_lat"))
            ne_lng = float(data.get("ne_lng"))
            sw_lat = float(data.get("sw_lat"))
            sw_lng = float(data.get("sw_lng"))
            # zoom = int(data.get("zoom", 10))

            ne_lat += 0.01
            ne_lng += 0.01
            sw_lat -= 0.01
            sw_lng -= 0.01
        except TypeError:
            ne_lat = ne_lng = sw_lat = sw_lng = None
        
        try:
            start_date = self.is_valid_date_format(data.get("start_date"))
            end_date = self.is_valid_date_format(data.get("end_date"))

        except ValueError:
            return jsonify({"error": "Invalid bounds"}), 400

        # if None in [ne_lat, ne_lng, sw_lat, sw_lng]:
        #     return jsonify({"error": "Invalid or missing bounds"}), 400
        

        user = g.current_user
        
        time1 = time.time()

        conn = psycopg2.connect(
            dbname=Config.DB_NAME,
            user=Config.DB_USER,
            password=Config.DB_PASS,
            host=Config.DB_HOST
        )
        cursor = conn.cursor()

        max_points_count = 3000
        filters = ""

        if end_date:
            end_date = end_date + " 23:59:59"
        
        if start_date and end_date:
            filters = f"AND timestamp BETWEEN '{start_date}' AND '{end_date}'"
        elif start_date:
            filters = f"AND timestamp >= '{start_date}'"
        elif end_date:
            filters = f"AND timestamp <= '{end_date}'"

        # filters += " AND horizontal_accuracy < 20"

        # calculate time delta
        time_delta = 0
        if start_date and end_date:
            time_delta = (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).total_seconds()

        if ne_lat is None and ne_lng is None and sw_lat is None and sw_lng is None:
            print("LOLOLOL")
            query = f"""
                WITH filtered_data AS (
                    SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                        altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
                        ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num,
                        COUNT(*) OVER () AS total
                    FROM gps_data
                    WHERE user_id = '{user.id}'
                    {filters}
                )
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM filtered_data
                WHERE total <= {max_points_count} 
                OR row_num % CEIL(total::FLOAT / {max_points_count})::INTEGER = 1
                ORDER BY timestamp;
            """
        
        elif time_delta != 0 and time_delta < 60 * 60 * 25:
            # query = f"""
            #     WITH filtered_data AS (
            #         SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
            #             altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
            #             ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num
            #         FROM gps_data
            #         WHERE user_id = '{user.id}'
            #         {date_filter}
            #     ),
            #     row_count AS (
            #         SELECT COUNT(*) AS total FROM filtered_data
            #     )
            #     SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
            #         altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
            #     FROM filtered_data
            #     WHERE row_num % (SELECT GREATEST(1, total / {max_points_count}) FROM row_count) = 1
            #     OR (SELECT total FROM row_count) < {max_points_count}
            #     ORDER BY timestamp;
            # """

            query = f"""
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM gps_data
                WHERE user_id = '{user.id}'
                AND latitude BETWEEN {sw_lat} AND {ne_lat}
                AND longitude BETWEEN {sw_lng} AND {ne_lng}
                {filters}
                ORDER BY timestamp;
            """

        #     # query = f"""
        #     #     SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
        #     #         altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
        #     #         ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num,
        #     #         COUNT(*) OVER () AS total
        #     #     FROM gps_data
        #     #     WHERE user_id = '{user.id}'
        #     #     AND latitude BETWEEN {sw_lat} AND {ne_lat}
        #     #     AND longitude BETWEEN {sw_lng} AND {ne_lng}
        #     #     {date_filter}
        #     #     ORDER BY timestamp;
        #     # """

        # elif zoom > 18:
        #     query = f"""
        #         WITH filtered_data AS (
        #             SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
        #                 altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
        #                 ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num,
        #                 COUNT(*) OVER () AS total
        #             FROM gps_data
        #             WHERE user_id = '{user.id}'
        #             AND latitude BETWEEN {sw_lat} AND {ne_lat}
        #             AND longitude BETWEEN {sw_lng} AND {ne_lng}
        #             {filters}
        #         )
        #         SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
        #             altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
        #         FROM filtered_data
        #         WHERE total <= {max_points_count} 
        #         OR row_num % CEIL(total::FLOAT / {max_points_count})::INTEGER = 1
        #         ORDER BY timestamp;
        #     """
        else:
            query = f"""
                WITH filtered_data AS (
                    SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                        altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
                        ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num,
                        COUNT(*) OVER () AS total
                    FROM gps_data
                    WHERE user_id = '{user.id}'
                    AND latitude BETWEEN {sw_lat} AND {ne_lat}
                    AND longitude BETWEEN {sw_lng} AND {ne_lng}
                    {filters}
                )
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM filtered_data
                WHERE total <= {max_points_count} 
                OR row_num % CEIL(total::FLOAT / {max_points_count})::INTEGER = 1
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
                "lng": row[4],
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
        user = g.current_user
        
        point_id = request.args.get("id")
        if not point_id:
            return "Missing point_id", 400
        point = GPSData.query.filter_by(id=point_id, user_id=user.id).first()
        if not point:
            return "Point not found", 404
        db.session.delete(point)
        db.session.commit()
        return "OK", 200
    
class HeatMapDataView(MethodView):
    decorators = [login_required]

    def get(self):
        user = g.current_user

        heatmap_query = GPSData.query.with_entities(
            func.json_agg(func.json_build_array(GPSData.latitude, GPSData.longitude))
        ).filter_by(user_id=user.id).scalar()

        return heatmap_query, 200, {"Content-Type": "application/json"}
    
class SpeedMapView(MethodView):
    decorators = [login_required]

    def get(self):
        # get last point coordinates ordered by timestamp
        user = g.current_user

        last_point: GPSData = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp.desc()).first()

        return render_template("speed_map.jinja", latitude=last_point.latitude, longitude=last_point.longitude)

    def post(self):
        #param point id will allways be set, will define the viewed point.
        # will return all the points around the point with a defined margin amount of points in each direction

        user = g.current_user

        # point_id = request.form.get("point_id")
        # date = request.form.get("date") # format: "YYYY-MM-DD"
        # time = request.form.get("time") # format: "HH:MM:SS"

        # get them from post body json
        data: dict = request.json
        point_id = data.get("point_id")
        date = data.get("date")
        time = data.get("time")
        
        if point_id:
            point = GPSData.query.filter_by(id=point_id, user_id=user.id).first()
        elif date and time:
            # find point closest to timestamp
            dateTimeObj = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
            # Convert the datetime to a Unix timestamp (a float value representing seconds).
            ts = dateTimeObj.timestamp()

            # Order by the absolute difference in seconds between the point's timestamp
            # (converted to epoch seconds using extract('epoch', ...)) and ts.
            point = (
                GPSData.query.filter_by(user_id=user.id)
                .order_by(func.abs(func.extract('epoch', GPSData.timestamp) - ts))
                .first()
            )
        else:
            return "Missing point_id or date and time", 400

        if not point:
            return "Point not found", 404
        
        margin = 100
        if "margin" in request.args:
            margin = min(int(request.args.get("margin")), 1000)

        # select all points within the margin of the point, so margin amount of points (not time) before and after the point
        before_points = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp.desc()).filter(GPSData.timestamp < point.timestamp).limit(margin).all()
        after_points = GPSData.query.filter_by(user_id=user.id).order_by(GPSData.timestamp.asc()).filter(GPSData.timestamp > point.timestamp).limit(margin).all()

        all_points: list[GPSData] = before_points[::-1] + [point] + after_points

        gps_data = []
        for row in all_points:
            gps_data.append({
                "id": row.id,
                "uid": row.user_id,
                "t": row.timestamp.isoformat() if row.timestamp else None,
                "lat": row.latitude,
                "lng": row.longitude,
                "ha": row.horizontal_accuracy,
                "a": row.altitude,
                "va": row.vertical_accuracy,
                "h": row.heading,
                "ha2": row.heading_accuracy,
                "s": row.speed,
                "sa": row.speed_accuracy,
            })

        return jsonify(gps_data)



class ManageUsersView(MethodView):
    decorators = [login_required]

    def get(self):
        user = g.current_user
        if not user.is_admin:
            return "Access denied", 403
        users = User.query.all()
        return render_template("manage_users.jinja", users=users)
    
    def post(self):
        user = g.current_user
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
            is_admin = request.form.get("is_admin").lower() == "true"

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
        user = g.current_user
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
# web_bp.add_url_rule("/dashboard", view_func=DashboardView.as_view("dashboard"))
web_bp.add_url_rule("/map", view_func=MapView.as_view("map"), methods=["GET", "POST", "DELETE"])
web_bp.add_url_rule("/map/heatmap_data.json", view_func=HeatMapDataView.as_view("heatmap_data"))
web_bp.add_url_rule("/map/speed", view_func=SpeedMapView.as_view("speed_map"), methods=["GET", "POST"])
web_bp.add_url_rule("/points", view_func=PointsView.as_view("points"), methods=["GET", "POST"])
web_bp.add_url_rule("/stats", view_func=StatsView.as_view("stats"))
web_bp.add_url_rule("/stats/<int:year>", view_func=YearlyStatsView.as_view("yearly_stats"))
web_bp.add_url_rule("/manage_users", view_func=ManageUsersView.as_view("manage_users"), methods=["GET", "POST"])
web_bp.add_url_rule("/account", view_func=AccountView.as_view("account"), methods=["GET", "POST"])
web_bp.add_url_rule("/jobs", view_func=JobsView.as_view("jobs"), methods=["GET", "POST"])
web_bp.add_url_rule("/imports", view_func=ImportsView.as_view("imports"), methods=["GET", "POST"])
