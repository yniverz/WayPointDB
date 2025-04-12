from io import BytesIO
from PIL import Image, ImageDraw, ImageFilter
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import math
import os
import re
import traceback
import uuid
import time
import psycopg2
import json
from flask import (
    Blueprint, Response, make_response, render_template, request, redirect, send_file, stream_with_context, url_for, 
    session, g, jsonify
)
from flask.views import MethodView
import requests
from sqlalchemy import func

from ..background.jobs import JOB_TYPES, ImportJob
from ..background import job_manager
from ..models import DailyStatistic, Import, User, GPSData, db, AdditionalTrace
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
                job[0].id if job[0] else None, 
                job[1], 
                job[2], 
                progress, 
                time_passed_str,
                time_left_str,
                job[0].email if job[0] else None
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
        trace = g.current_trace
        

        total_points = GPSData.query.filter_by(**g.trace_query).count()
        total_geocoded = GPSData.query.filter_by(**g.trace_query).filter(GPSData.reverse_geocoded == True).count()
        total_not_geocoded = GPSData.query.filter_by(**g.trace_query).filter(GPSData.reverse_geocoded == True).filter(GPSData.country == None).count()
        stats: list[DailyStatistic] = DailyStatistic.query.filter_by(**g.trace_query).all()

        # We'll group stats by year
        stats_by_year = defaultdict(lambda: {
            "monthly_distances": [0.0] * 12,  # 12 months
            "cities": set(),
            "countries": set(),
            "total_distance": 0.0
        })

        stats.sort(key=lambda x: (x.year, x.month, x.day))

        last_visit_cities = dict()
        last_visit_countries = dict()

        for stat in stats:
            year = stat.year
            month_idx = stat.month - 1  # January -> 0, etc.

            # Accumulate distances (in meters). We'll convert to km later.
            stats_by_year[year]["monthly_distances"][month_idx] += stat.total_distance_m

            # Update visited cities / countries (they are stored in JSON columns)
            if stat.visited_cities:
                stats_by_year[year]["cities"].update([tuple(city) for city in stat.visited_cities])
                last_visit_cities.update([(tuple(city), f"{stat.day:02d}-{stat.month:02d}-{stat.year}") for city in stat.visited_cities])
            if stat.visited_countries:
                stats_by_year[year]["countries"].update(stat.visited_countries)
                last_visit_countries.update([(country, f"{stat.day:02d}-{stat.month:02d}-{stat.year}") for country in stat.visited_countries])

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
            last_visit_cities=last_visit_cities,
            last_visit_countries=last_visit_countries,
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
        
        total_points = GPSData.query.filter_by(**g.trace_query).filter(func.extract("year", GPSData.timestamp) == year).count()

        stats: list[DailyStatistic] = DailyStatistic.query.filter_by(**g.trace_query, year=year).all()

        # We'll group stats by month
        stats_by_month = defaultdict(lambda: {
            "monthly_distances": [0.0] * 31,  # 31 days
            "cities": set(),
            "countries": set(),
            "total_distance": 0.0
        })

        stats.sort(key=lambda x: (x.year, x.month, x.day))

        last_visit_cities = dict()
        last_visit_countries = dict()

        for stat in stats:
            month = stat.month
            day = stat.day

            # Accumulate distances (in meters). We'll convert to km later.
            stats_by_month[month]["monthly_distances"][day - 1] = stat.total_distance_m

            # Update visited cities / countries (they are stored in JSON columns)
            if stat.visited_cities:
                stats_by_month[month]["cities"].update([tuple(city) for city in stat.visited_cities])
                last_visit_cities.update([(tuple(city), f"{stat.day:02d}-{stat.month:02d}-{stat.year}") for city in stat.visited_cities])
            if stat.visited_countries:
                stats_by_month[month]["countries"].update(stat.visited_countries)
                last_visit_countries.update([(country, f"{stat.day:02d}-{stat.month:02d}-{stat.year}") for country in stat.visited_countries])

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
            last_visit_cities=last_visit_cities,
            last_visit_countries=last_visit_countries,
            total_distance=f"{total_distance / 1000.0:,.0f}",  # Convert to KM
            total_points=f"{total_points:,}",
            is_photon_connected=len(Config.PHOTON_SERVER_HOST) != 0,
        )


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
        points_query = GPSData.query.filter_by(**g.trace_query).filter(
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
        raw_imports = Import.query.filter_by(**g.trace_query).all()

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
        raw_imports: list[Import] = Import.query.filter_by(**g.trace_query).order_by(Import.created_at.desc()).all()

        imports = []
        for imp in raw_imports:
            imports.append({
                "id": imp.id,
                "name": imp.original_filename,
                "created_at": imp.created_at,
                "total_entries": imp.total_entries,
                "total_imported": GPSData.query.filter_by(**g.trace_query, import_id=imp.id).count(),
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
            return self.start_import_job(user, trace=g.current_trace)
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
        
        kwarg = {"user_id": user.id}
        if g.current_trace:
            kwarg = {"trace_id": g.current_trace.id}

        # Create a new Import record
        new_import = Import(
            **kwarg,
            filename=unique_name,
            original_filename=filename,
            created_at=datetime.now(timezone.utc),
            total_entries=len(json_data)
        )
        db.session.add(new_import)
        db.session.commit()

        # Redirect back to /imports
        return redirect(url_for("web.imports"))

    def start_import_job(self, user, trace=None):
        """Queue a background job to parse and insert data from the specified import."""
        import_id = request.form.get("import_id")
        if not import_id:
            return "Missing import_id", 400

        import_record = Import.query.filter_by(id=import_id, user_id=user.id).first()
        if not import_record:
            import_record = Import.query.filter_by(id=import_id, trace_id=trace.id).first()
            if not import_record:
                return "Import not found or not yours", 404

        # Add job to job manager
        # The job itself (ImportJob) will handle reading the file from disk,
        # validating JSON, inserting GPSData, etc.
        job_instance = ImportJob(user=user, import_obj=import_record, trace=trace)
        job_manager.add_job(job_instance)

        return redirect(url_for("web.imports"))

    def delete_import(self, user):
        """Deletes an import record AND all associated GPSData. Asks user for confirmation in the HTML form."""
        import_id = request.form.get("import_id")
        if not import_id:
            return "Missing import_id", 400

        import_record = Import.query.filter_by(id=import_id, user_id=user.id).first()
        if not import_record:
            import_record = Import.query.filter_by(id=import_id, trace_id=g.current_trace.id).first()
            if not import_record:
                return "Import not found or not yours", 404

        # First delete associated GPSData by this import_id
        # Note the "import_id" in GPSData is a string field, so match accordingly
        GPSData.query.filter_by(import_id=str(import_record.id)).delete(synchronize_session=False)

        # Remove the import record itself
        db.session.delete(import_record)
        db.session.commit()

        # Optional: remove the file from disk
        file_path = os.path.join(Config.UPLOAD_FOLDER, import_record.filename)
        if os.path.exists(file_path):
            os.remove(file_path)

        return redirect(url_for("web.imports"))



class ExportsView(MethodView):
    decorators = [login_required]

    def get(self):
        # Render a simple page that has a button to trigger the download
        return render_template("exports.jinja")

    def post(self):
        user = g.current_user

        # Query all GPSData for the current user.
        # Use 'yield_per' to minimize memory usage for large tables:
        points_query = (
            GPSData.query
            .filter_by(**g.trace_query)
            .order_by(GPSData.timestamp.asc())
            .yield_per(1000)
        )

        def generate_json():
            """
            A generator function to stream JSON in a memory-efficient way.
            We'll manually build a JSON array of objects, yielding piece by piece.
            """
            yield '['
            first = True

            for point in points_query:
                if not first:
                    # Separate JSON objects with a comma
                    yield ','
                else:
                    first = False

                # Build a dict for each GPSData row
                item = {
                    "timestamp": point.timestamp.isoformat() if point.timestamp else None,
                    "latitude": point.latitude,
                    "longitude": point.longitude,
                    "horizontal_accuracy": point.horizontal_accuracy,
                    "altitude": point.altitude,
                    "vertical_accuracy": point.vertical_accuracy,
                    "heading": point.heading,
                    "heading_accuracy": point.heading_accuracy,
                    "speed": point.speed,
                    "speed_accuracy": point.speed_accuracy,
                    "reverse_geocoded": point.reverse_geocoded,
                    "city": point.city,
                    "country": point.country,
                }
                yield json.dumps(item, separators=(',', ':'))

            yield ']'

        # Wrap the generator in a streaming response
        response = Response(
            stream_with_context(generate_json()),
            mimetype='application/json'
        )
        # Force a download with the given filename
        response.headers['Content-Disposition'] = 'attachment; filename="gps_export.json"'

        return response





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
                    properties = feature.get("properties", {})
                    type = properties.get("type")

                    zoom = 13
                    if type in ["country", "state"]:
                        zoom = 6

                    if len(coords) == 2:
                        return redirect(url_for("web.map")+f"?lat={coords[1]}&lng={coords[0]}&zoom={zoom}&view_location=true")
            except Exception as e:
                traceback.print_exc()
                pass
                

        point_id = request.args.get("point_id")

        if point_id:
            last_point = GPSData.query.filter_by(id=point_id, **g.trace_query).first()
        else:
            last_point = GPSData.query.filter_by(**g.trace_query).order_by(GPSData.timestamp.desc()).first()

        if not last_point:
            # Some default coords
            last_point = {"id": -1, "lat": 52.516310, "lng": 13.378208}

        else:
            last_point = {
                "id": last_point.id,
                "lat": last_point.latitude,
                "lng": last_point.longitude
            }

        earliest_point: GPSData = GPSData.query.filter_by(**g.trace_query).order_by(GPSData.timestamp.asc()).first()
        earliest_year = earliest_point.timestamp.year if earliest_point else None
        earliest_month = earliest_point.timestamp.month if earliest_point else None

        return render_template("map.jinja", last_point=last_point, earliest_year=earliest_year, earliest_month=earliest_month)

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
        
        fetch_interpolated = bool(data.get("fetch_interpolated", True))

        

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

        user_trace_id = f"trace_id = '{g.current_trace.id}'" if g.current_trace else f"user_id = '{user.id}'"

        if ne_lat is None and ne_lng is None and sw_lat is None and sw_lng is None and fetch_interpolated:
            query = f"""
                WITH filtered_data AS (
                    SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                        altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
                        ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num,
                        COUNT(*) OVER () AS total
                    FROM gps_data
                    WHERE {user_trace_id}
                    {filters}
                )
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM filtered_data
                WHERE total <= {max_points_count} 
                OR row_num % CEIL(total::FLOAT / {max_points_count})::INTEGER = 1
                ORDER BY timestamp;
            """
        
        elif (time_delta != 0 and time_delta < 60 * 60 * 25) or not fetch_interpolated:
            query = f"""
                SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                    altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy
                FROM gps_data
                WHERE {user_trace_id}
                {filters}
                ORDER BY timestamp;
            """
        else:
            query = f"""
                WITH filtered_data AS (
                    SELECT id, user_id, timestamp, latitude, longitude, horizontal_accuracy,
                        altitude, vertical_accuracy, heading, heading_accuracy, speed, speed_accuracy,
                        ROW_NUMBER() OVER (ORDER BY timestamp) AS row_num,
                        COUNT(*) OVER () AS total
                    FROM gps_data
                    WHERE {user_trace_id}
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

        # return jsonify(gps_data)
        response = self.compress(gps_data)
        response.headers["Is-Interpolated"] = len(gps_data) >= max_points_count - 100

        return response

    def delete(self):
        user = g.current_user
        
        point_id = request.args.get("id")
        if not point_id:
            return "Missing point_id", 400
        point = GPSData.query.filter_by(id=point_id, **g.trace_query).first()
        if not point:
            return "Point not found", 404
        db.session.delete(point)
        db.session.commit()
        return "OK", 200
    
    def compress(self, data):
        data = json.dumps(data).encode('utf8')
        content = gzip.compress(data, 9)
        response = make_response(content)
        response.headers['Content-length'] = len(content)
        response.headers['Content-Original-Length'] = len(data)
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Encoding'] = 'gzip'
        return response
    
class HeatMapDataView(MethodView):
    decorators = [login_required]

    def get(self):
        user = g.current_user

        heatmap_query = GPSData.query.with_entities(
            func.json_agg(func.json_build_array(GPSData.latitude, GPSData.longitude))
        ).filter_by(**g.trace_query).scalar()

        return self.compress(heatmap_query)
    
    def compress(self, data):
        data = json.dumps(data).encode('utf8')
        content = gzip.compress(data, 9)
        response = make_response(content)
        response.headers['Content-length'] = len(content)
        response.headers['Content-Original-Length'] = len(data)
        response.headers['Content-Type'] = 'application/json'
        response.headers['Content-Encoding'] = 'gzip'
        return response
    
class SpeedMapView(MethodView):
    decorators = [login_required]

    def get(self):
        # get last point coordinates ordered by timestamp
        user = g.current_user

        last_point: GPSData = GPSData.query.filter_by(**g.trace_query).order_by(GPSData.timestamp.desc()).first()

        if not last_point:
            last_point = {"latitude": 52.516310, "longitude": 13.378208}
        else:
            last_point = {"latitude": last_point.latitude, "longitude": last_point.longitude}

        return render_template("speed_map.jinja", latitude=last_point["latitude"], longitude=last_point["longitude"])
    
    def post(self):
        user = g.current_user

        # get them from post body json
        data: dict = request.json
        point_id = data.get("point_id")
        date = data.get("date")
        time = data.get("time")
        
        if point_id:
            point = GPSData.query.filter_by(id=point_id, **g.trace_query).first()
        elif date and time:
            # find point closest to timestamp
            dateTimeObj = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
            # Convert the datetime to a Unix timestamp (a float value representing seconds).
            ts = dateTimeObj.timestamp()

            # Order by the absolute difference in seconds between the point's timestamp
            # (converted to epoch seconds using extract('epoch', ...)) and ts.
            point = (
                GPSData.query.filter_by(**g.trace_query)
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
        before_points = GPSData.query.filter_by(**g.trace_query).order_by(GPSData.timestamp.desc()).filter(GPSData.timestamp < point.timestamp).limit(margin).all()
        after_points = GPSData.query.filter_by(**g.trace_query).order_by(GPSData.timestamp.asc()).filter(GPSData.timestamp > point.timestamp).limit(margin).all()

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

        elif action == "change_password":
            user_id = request.form.get("user_id")
            password = request.form.get("password")

            if not user_id or not password:
                return "Missing user_id or password", 400

            user: User = User.query.filter_by(id=user_id).first()
            if not user:
                return "User not found", 404

            user.set_password(password)
            db.session.commit()

            return "OK", 200

        return redirect(url_for("web.manage_users"))
    
class ManageTracesView(MethodView):
    decorators = [login_required]

    def get(self):
        user = g.current_user

        available_traces = AdditionalTrace.query.filter_by(owner_id=user.id).all()

        users = [(str(u.id), u.email) for u in User.query.all()]

        return render_template("manage_traces.jinja", available_traces=available_traces, users=users, current_user_id=str(user.id))
    
    def post(self):
        user = g.current_user
        
        action = request.form.get("action")

        print(request.form, action)

        if action == "remove_trace":
            trace_id = request.form.get("trace_id")

            if not trace_id:
                return "Missing trace_id", 400
            
            trace = AdditionalTrace.query.filter_by(id=trace_id).first()
            if not trace:
                return "Trace not found", 404
            db.session.delete(trace)
            db.session.commit()

        elif action == "add_trace":
            name = request.form.get("name")
            description = request.form.get("description")

            if not name:
                return "Missing name", 400

            new_trace = AdditionalTrace(name=name, owner_id=user.id, description=description)
            db.session.add(new_trace)
            db.session.commit()

            print(AdditionalTrace.query.all())

        elif action == "share_trace":
            user_id = request.form.get("user_id")
            trace_id = request.form.get("trace_id")

            print(user_id, trace_id)

            if not user_id or not trace_id:
                return "Missing user_id or trace_id", 400
            
            if user_id == str(user.id):
                return "Cannot share with yourself", 400
            
            trace = AdditionalTrace.query.filter_by(id=trace_id).first()
            if not trace:
                return "Trace not found", 404
            
            if user_id not in trace.share_with_list:
                trace.share_with_list.append(user_id)
                db.session.commit()

        elif action == "unshare_trace":
            user_id = request.form.get("user_id")
            trace_id = request.form.get("trace_id")

            if not user_id or not trace_id:
                return "Missing user_id or trace_id", 400
            
            trace = AdditionalTrace.query.filter_by(id=trace_id).first()
            if not trace:
                return "Trace not found", 404
            
            if user_id in trace.share_with_list:
                trace.share_with_list.remove(user_id)
                db.session.commit()

        elif "transfer_trace" in action:
            user_id = request.form.get("user_id")
            trace_id = request.form.get("trace_id")

            if not user_id or not trace_id:
                return "Missing user_id or trace_id", 400
            
            trace = AdditionalTrace.query.filter_by(id=trace_id).first()
            if not trace:
                return "Trace not found", 404
            
            if user_id != user.id:
                trace.owner_id = user_id
                db.session.commit()

        return redirect(url_for("web.manage_traces"))

class AccountView(MethodView):
    decorators = [login_required]

    def get(self):
        user: User = g.current_user

        if "custom_api_key" in request.args:
            custom_api_key = request.args.get("custom_api_key")
            if custom_api_key:
                key_exists = custom_api_key in [x[0] for x in sum([u.api_keys for u in User.query.all()], [])]
                if not key_exists:
                    user.api_keys.append((custom_api_key, None))
                    db.session.commit()
                    return "OK", 200
                else:
                    return "Key already exists", 400
            else:
                return "Missing custom_api_key", 400

        api_keys = user.api_keys
        traces = AdditionalTrace.query.filter_by(owner_id=user.id).all()

        api_key_list = []
        for key, trace_id in api_keys:
            name = "Main Trace"
            if trace_id:
                trace = AdditionalTrace.query.filter_by(id=trace_id).first()
                if trace:
                    name = trace.name

            api_key_list.append((key, trace_id, name))

        return render_template("account.jinja", api_keys=api_key_list, available_traces=traces)

    def post(self):
        user: User = g.current_user

        if "generate_key" in request.form:
            trace_id = request.form.get("trace_id")
            if len(trace_id) == 0 or not AdditionalTrace.query.filter_by(id=trace_id).first():
                trace_id = None

            user.api_keys.append((uuid.uuid4().hex, trace_id))
            db.session.commit()

        if "delete_key" in request.form:
            api_key = request.form.get("api_key")
            if api_key:
                user.api_keys = [(key, trace_id) for key, trace_id in user.api_keys if key != api_key]
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
    


class SetTraceView(MethodView):
    decorators = [login_required]

    def post(self):
        trace_id = request.form.get("trace_id")
        print(trace_id, request.form)
        if not trace_id:
            session.pop("trace_id", None)
            return "OK", 205

        trace = AdditionalTrace.query.filter_by(id=trace_id).first()
        if not trace:
            return "Trace not found", 404

        session["trace_id"] = trace_id

        return "OK", 200



full_bleed_background_img_store = {}
full_bleed_background_date_store = {}

class FullBleedBackground(MethodView):
    decorators = [login_required]


    def get(self):
        global full_bleed_background_img_store, full_bleed_background_date_store

        if g.current_user.id in full_bleed_background_img_store and full_bleed_background_date_store[g.current_user.id] > datetime.now() - timedelta(hours=12):
            resp = make_response(self.serve_pil_image(full_bleed_background_img_store[g.current_user.id]))
            resp.headers["Cache-Control"] = "public, max-age=43200"  # 12 hours
            resp.headers["Expires"] = (datetime.now() + timedelta(hours=12)).strftime("%a, %d %b %Y %H:%M:%S GMT")
            return resp

        # get last point
        last_point: GPSData = GPSData.query.filter_by(user_id = g.current_user.id).order_by(GPSData.timestamp.desc()).first()
        if not last_point:
            return "No points found", 404
        # get all points within 10000m of the last point
        points = GPSData.query.filter_by(user_id = g.current_user.id).filter(
            GPSData.latitude.between(last_point.latitude - 0.1, last_point.latitude + 0.1),
            GPSData.longitude.between(last_point.longitude - 0.1, last_point.longitude + 0.1)
        ).order_by(GPSData.timestamp.asc()).all()
        if not points:
            return "No points found", 404
        # generate image
        image = self.generateImage(points)

        # store image in memory
        full_bleed_background_img_store[g.current_user.id] = image
        full_bleed_background_date_store[g.current_user.id] = datetime.now()
        
        return self.serve_pil_image(image)
        


    def serve_pil_image(self, pil_img: Image.Image):
        img_io = BytesIO()
        pil_img.save(img_io, 'PNG')
        img_io.seek(0)
        return send_file(img_io, mimetype='image/png')

    # A small "color stop" helper, storing speed in km/h and an RGB color tuple.
    class ColorStop:
        def __init__(self, speed_kmh, color):
            self.SpeedKmh = speed_kmh
            self.Color = color  # (R, G, B)

    def generateImage(self, points):
        # ************************ Configuration Variables ************************

        # We'll no longer hard-code the center lat/lng; computed from the last point.
        global centerLat, centerLng

        # Define the "zoom" as a radius (in meters) representing half of the horizontal width.
        radiusMeters = 5000.0

        # Image dimensions.
        imageWidth = 1600*2
        imageHeight = 900*2

        # Configurable line thickness (pixels in Pillow).
        lineThickness = 1

        # Max distance (meters) between consecutive points to draw a line.
        maxPointDistance = 100.0

        # color stops for speed-based interpolation (in km/h).
        colorStops = [
            self.ColorStop(0,   (0,   0, 255)),  # Blue
            self.ColorStop(30,  (0, 255,   0)),  # Green
            self.ColorStop(60,  (255, 255,  0)), # Yellow
            self.ColorStop(90,  (255,   0,  0)), # Red
        ]

        # 2) Compute center as last point in dataset.
        centerLat = points[-1].latitude
        centerLng = points[-1].longitude

        # 3) Meters-per-degree for lat/lng. 
        #    This is approximate and depends on latitude.
        metersPerDegLat = 111320.0
        metersPerDegLng = 111320.0 * math.cos(math.radians(centerLat))

        # 4) Compute scale (pixels per meter).
        scale = imageWidth / (2.0 * radiusMeters)

        # 5) Create a Pillow image (RGBA or RGB).
        image = Image.new('RGBA', (imageWidth, imageHeight), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # 6) Draw each consecutive line segment between GPS points.
        for i in range(1, len(points)):
            pA = points[i - 1]
            pB = points[i]

            # Skip if distance too large
            dist = self.haversine(pA.latitude, pA.longitude, pB.latitude, pB.longitude)
            if dist > maxPointDistance:
                continue

            # Convert lat/lng to pixel
            x1, y1 = self.map_to_pixel(pA.latitude, pA.longitude,
                                centerLat, centerLng,
                                metersPerDegLat, metersPerDegLng,
                                scale, imageWidth, imageHeight)
            x2, y2 = self.map_to_pixel(pB.latitude, pB.longitude,
                                centerLat, centerLng,
                                metersPerDegLat, metersPerDegLng,
                                scale, imageWidth, imageHeight)

            # Average speed for color
            avgSpeed = (pA.speed + pB.speed) / 2.0
            segmentColor = self.get_color_from_speed(avgSpeed, colorStops)

            # Draw line
            draw.line([(x1, y1), (x2, y2)], fill=segmentColor, width=lineThickness)

        # antialiasing
        image = image.filter(ImageFilter.GaussianBlur(radius=1))

        # halve resolution
        image = image.resize((imageWidth // 2, imageHeight // 2), Image.LANCZOS)

        return image

    def map_to_pixel(self, lat, lng,
                    centerLat, centerLng,
                    metersPerDegLat, metersPerDegLng,
                    scale, imageWidth, imageHeight):
        """
        Convert (lat,lng) to (x,y) in image coords, given:
        centerLat, centerLng as the "map center" in lat/lng
        metersPerDegLat, metersPerDegLng approximations
        scale (pixels per meter)
        imageWidth, imageHeight
        """
        dLat = lat - centerLat
        dLng = lng - centerLng

        # Convert degrees to meters
        dx = dLng * metersPerDegLng
        dy = dLat * metersPerDegLat

        # Then convert meters to pixels
        x = (imageWidth / 2.0) + dx * scale
        # invert y so north is up
        y = (imageHeight / 2.0) - dy * scale

        return (x, y)


    def haversine(self, lat1, lon1, lat2, lon2):
        """
        Calculate distance in meters between two lat/lon points using the Haversine formula.
        """
        R = 6371000.0  # Earth radius in meters
        dLat = math.radians(lat2 - lat1)
        dLon = math.radians(lon2 - lon1)
        a = (math.sin(dLat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
            * math.sin(dLon / 2) ** 2)
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c


    def get_color_from_speed(self, speed_mps, colorStops: list[ColorStop]):
        """
        Interpolate a color based on speed (m/s). colorStops are in km/h.
        Returns an (R,G,B) tuple for Pillow.
        """
        speed_kmh = speed_mps * 3.6
        # If speed is below or equal to first stop:
        if speed_kmh <= colorStops[0].SpeedKmh:
            return colorStops[0].Color

        for i in range(1, len(colorStops)):
            prevStop = colorStops[i - 1]
            nextStop = colorStops[i]
            if speed_kmh <= nextStop.SpeedKmh:
                # Interpolate between prevStop and nextStop
                ratio = ((speed_kmh - prevStop.SpeedKmh)
                        / (nextStop.SpeedKmh - prevStop.SpeedKmh))
                r = int(round(prevStop.Color[0] + (nextStop.Color[0] - prevStop.Color[0]) * ratio))
                g = int(round(prevStop.Color[1] + (nextStop.Color[1] - prevStop.Color[1]) * ratio))
                b = int(round(prevStop.Color[2] + (nextStop.Color[2] - prevStop.Color[2]) * ratio))
                return (r, g, b)

        # If above the highest speed, return the last stop's color
        return colorStops[-1].Color




# Register the class-based views with the Blueprint
web_bp.add_url_rule("/", view_func=HomeView.as_view("home"))
web_bp.add_url_rule("/login", view_func=LoginView.as_view("login"), methods=["GET", "POST"])
web_bp.add_url_rule("/logout", view_func=LogoutView.as_view("logout"))
web_bp.add_url_rule("/set_trace_id", view_func=SetTraceView.as_view("set_trace_id"), methods=["POST"])
web_bp.add_url_rule("/full_bleed_background.png", view_func=FullBleedBackground.as_view("full_bleed_background"))
web_bp.add_url_rule("/map", view_func=MapView.as_view("map"), methods=["GET", "POST", "DELETE"])
web_bp.add_url_rule("/map/heatmap_data.json", view_func=HeatMapDataView.as_view("heatmap_data"))
web_bp.add_url_rule("/map/speed", view_func=SpeedMapView.as_view("speed_map"), methods=["GET", "POST"])
web_bp.add_url_rule("/points", view_func=PointsView.as_view("points"), methods=["GET", "POST"])
web_bp.add_url_rule("/stats", view_func=StatsView.as_view("stats"))
web_bp.add_url_rule("/stats/<int:year>", view_func=YearlyStatsView.as_view("yearly_stats"))
web_bp.add_url_rule("/manage_users", view_func=ManageUsersView.as_view("manage_users"), methods=["GET", "POST"])
web_bp.add_url_rule("/account", view_func=AccountView.as_view("account"), methods=["GET", "POST"])
web_bp.add_url_rule("/manage_traces", view_func=ManageTracesView.as_view("manage_traces"), methods=["GET", "POST"])
web_bp.add_url_rule("/jobs", view_func=JobsView.as_view("jobs"), methods=["GET", "POST"])
web_bp.add_url_rule("/imports", view_func=ImportsView.as_view("imports"), methods=["GET", "POST"])
web_bp.add_url_rule("/exports", view_func=ExportsView.as_view("exports"), methods=["GET", "POST"])
