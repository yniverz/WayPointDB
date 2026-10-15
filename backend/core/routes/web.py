from io import BytesIO
from PIL import Image, ImageDraw, ImageFilter
from collections import OrderedDict, defaultdict, namedtuple
from datetime import datetime, timedelta, timezone
import gzip
import math
import os
import re
import secrets
import threading
import traceback
import uuid
import time
import psycopg2
import json
from urllib.parse import urljoin, urlparse
from flask import (
    Blueprint, Response, make_response, render_template, request, redirect, send_file, stream_with_context, url_for, 
    session, g, jsonify, flash
)
from flask.views import MethodView
import requests
from sqlalchemy import Integer, Numeric, cast, func, select, text
from werkzeug.security import check_password_hash, generate_password_hash

from ..background.jobs import JOB_TYPES, ImportJob, JobStatus
from ..background import apply_schedule_start, job_manager
from ..models import DailyStatistic, Import, ScheduledJob, User, GPSData, db, AdditionalTrace
from ..utils import login_required
from ..config import Config
from werkzeug.utils import secure_filename

web_bp = Blueprint("web", __name__)


# Failed-login tracking. Keyed by email so it is unaffected by the proxy collapsing
# every client onto a single source address. Waitress serves from one process, so a
# plain dict guarded by a lock is sufficient.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
_login_failures: dict[str, tuple[int, float]] = {}
_login_failures_lock = threading.Lock()

# Compared against when the email is unknown so that response time does not reveal
# whether an account exists.
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_hex(32))


def _login_lock_remaining(key: str) -> float:
    """Seconds left before `key` may attempt a login again, or 0 if it may attempt now."""
    now = time.monotonic()
    with _login_failures_lock:
        count, last_failure = _login_failures.get(key, (0, 0.0))
        if count < LOGIN_MAX_ATTEMPTS:
            return 0.0
        remaining = LOGIN_LOCKOUT_SECONDS - (now - last_failure)
        if remaining <= 0:
            _login_failures.pop(key, None)
            return 0.0
        return remaining


def _record_login_failure(key: str) -> None:
    now = time.monotonic()
    with _login_failures_lock:
        # Drop expired entries so attacker-supplied emails cannot grow this unbounded.
        for stale_key, (_, last_failure) in list(_login_failures.items()):
            if now - last_failure > LOGIN_LOCKOUT_SECONDS:
                del _login_failures[stale_key]

        count, _ = _login_failures.get(key, (0, 0.0))
        _login_failures[key] = (count + 1, now)


def _clear_login_failures(key: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(key, None)


def is_safe_redirect_target(target: str) -> bool:
    """Only allow redirects that stay on this host."""
    if not target:
        return False
    host_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and host_url.netloc == test_url.netloc


def delete_series_data(column: str, series_id) -> None:
    """Delete the points, statistics and imports (with their files) of a user's main trace or of a trace."""
    for imp in Import.query.filter(getattr(Import, column) == series_id).all():
        path = os.path.join(Config.UPLOAD_FOLDER, imp.filename)
        if imp.filename and os.path.exists(path):
            os.remove(path)
    GPSData.query.filter(getattr(GPSData, column) == series_id).delete(synchronize_session=False)
    DailyStatistic.query.filter(getattr(DailyStatistic, column) == series_id).delete(synchronize_session=False)
    Import.query.filter(getattr(Import, column) == series_id).delete(synchronize_session=False)


def delete_trace(trace: AdditionalTrace) -> None:
    """Delete a trace with all its data, and the API keys that write to it."""
    delete_series_data("trace_id", trace.id)
    for user in User.query.all():
        keys = [(key, trace_id) for key, trace_id in user.api_keys if trace_id != str(trace.id)]
        if len(keys) != len(user.api_keys):
            user.api_keys = keys
    db.session.delete(trace)


class HomeView(MethodView):
    decorators = [login_required]

    def get(self):
        """Simple home route."""
        
        return redirect(url_for("web.map"))

class LoginView(MethodView):
    def get(self):
        return render_template("login.jinja")

    def post(self):
        email = request.form.get("email") or ""
        password = request.form.get("password") or ""
        throttle_key = email.strip().lower()

        remaining = _login_lock_remaining(throttle_key)
        if remaining > 0:
            response = make_response(render_template(
                "login.jinja", email=email, error="Too many failed login attempts. Try again later."
            ), 429)
            response.headers["Retry-After"] = str(int(remaining) + 1)
            return response

        user = User.query.filter_by(email=email).first()
        if user is None:
            check_password_hash(_DUMMY_PASSWORD_HASH, password)
            password_ok = False
        else:
            password_ok = user.check_password(password)

        if not password_ok:
            _record_login_failure(throttle_key)
            return render_template("login.jinja", email=email, error="Wrong email or password."), 401

        _clear_login_failures(throttle_key)
        session["user_id"] = user.id

        dest = request.args.get('next')
        if is_safe_redirect_target(dest):
            return redirect(dest)

        return redirect(url_for("web.home"))

class LogoutView(MethodView):
    def get(self):
        session.clear()
        return redirect(url_for("web.login"))

JobParam = namedtuple("JobParam", "name label default unit")
JobInfo = namedtuple("JobInfo", "name label group description param", defaults=(None,))

# Jobs offered on the jobs page, to run now or on a schedule, in display order.
JOB_CATALOG = [
    JobInfo("photon_fill", "Geocode new points", "Geocoding",
            "Looks up country, city and address for points that have not been geocoded yet."),
    JobInfo("photon_full", "Geocode all points", "Geocoding",
            "Looks up every point again and overwrites existing location names."),
    JobInfo("full_stats", "Generate statistics", "Statistics",
            "Recalculates daily distances and visited countries and cities."),
    JobInfo("speed_data", "Generate speed data", "Statistics",
            "Calculates a speed for points that have none, from the distance to the previous point."),
    JobInfo("delete_duplicates", "Delete duplicates", "Data cleaning",
            "Removes points with the same time and position as another point."),
    JobInfo("filter_accuracy", "Filter inaccurate points", "Data cleaning",
            "Removes points whose horizontal accuracy is worse than the limit.",
            JobParam("maximum_accuracy", "Max. accuracy", 500, "m")),
    JobInfo("filter_speed", "Filter GPS jumps", "Data cleaning",
            "Removes points that could only be reached faster than the limit, such as GPS jumps.",
            JobParam("maximum_speed_kmh", "Max. speed", 1000, "km/h")),
    JobInfo("filter_clusters", "Reduce clusters", "Data cleaning",
            "Thins out points while standing still, keeping the first and last point of each stay.",
            JobParam("maximum_distance", "Min. distance", 14, "m")),
]


class JobsView(MethodView):
    decorators = [login_required]

    @staticmethod
    def available_jobs() -> list[JobInfo]:
        return [info for info in JOB_CATALOG if info.name in JOB_TYPES]

    def get_schedules(self, user: User):
        """The schedulable jobs available on this server, with the user's saved settings."""
        saved = {s.job_name: s for s in ScheduledJob.query.filter_by(user_id=user.id).all()}

        schedules = []
        for info in self.available_jobs():
            schedule = saved.get(info.name)
            param_value = None
            if info.param:
                param_value = (schedule.params or {}).get(info.param.name, info.param.default) if schedule else info.param.default

            schedules.append({
                "info": info,
                "enabled": bool(schedule and schedule.enabled),
                "hour": schedule.hour if schedule else 0,
                "last_run_date": schedule.last_run_date if schedule else None,
                "param_value": param_value,
            })

        return schedules

    @staticmethod
    def serialize_job(job) -> dict:
        now = time.time()
        elapsed = ((job.end_time or now) - job.start_time) if job.start_time else 0
        remaining = None
        if job.status == JobStatus.RUNNING and job.progress > 0.01:
            remaining = elapsed / job.progress - elapsed

        return {
            "id": job.id,
            "label": job.LABEL,
            "user": job.user_email,
            "status": job.status,
            "progress": job.progress,
            "elapsed": elapsed,
            "remaining": remaining,
            "finished_ago": (now - job.end_time) if job.end_time else None,
            "message": job.message,
            "error": job.error,
            "stopping": job.stop_requested and not job.done,
        }

    def visible_jobs(self):
        # Non-admins only see their own jobs; the full list leaks other users' emails and activity.
        user = g.current_user
        return job_manager.get_jobs(None if user.is_admin else user.id)

    def get(self):
        jobs = [self.serialize_job(job) for job in self.visible_jobs()]

        if request.args.get("update"):
            return jsonify(jobs)

        job_groups = OrderedDict()
        for info in self.available_jobs():
            job_groups.setdefault(info.group, []).append(info)

        return render_template(
            "jobs.jinja",
            jobs=jobs,
            job_groups=job_groups,
            schedules=self.get_schedules(g.current_user),
            server_time=datetime.now().astimezone().strftime("%H:%M %Z"),
        )
    
    def post(self):

        if "newJob" in request.form:
            return self.do_new_job(request.form)

        if "saveSchedules" in request.form:
            return self.do_save_schedules(request.form)

        if "cancelJob" in request.form:
            return self.do_cancel_job(request.form)
        
        return jsonify(error="Invalid request"), 400

    def do_cancel_job(self, params: dict):
        job = job_manager.get_job(params.get("cancelJob"))
        if job is None or (job.user_id != g.current_user.id and not g.current_user.is_admin):
            return jsonify(error="Job not found"), 404

        job_manager.cancel_job(job.id)
        return jsonify(ok=True)

    def do_new_job(self, params: dict):
        job_name = params.get("newJob")
        
        if job_name not in JOB_TYPES:
            return jsonify(error="Unknown job"), 400
        
        job = JOB_TYPES[job_name]

        if job_manager.has_job(g.current_user, job):
            return jsonify(error=f"{job.LABEL} is already queued or running"), 409

        kwargs = {}
        for param, param_type in job.PARAMETERS.items():
            if param == "user":
                continue
            try:
                value = param_type(params[param])
            except (KeyError, ValueError):
                return jsonify(error=f"Missing or invalid value for {param}"), 400
            if isinstance(value, float) and (not math.isfinite(value) or value < 0):
                return jsonify(error=f"Invalid value for {param}"), 400
            kwargs[param] = value

        job_manager.add_job(job(user=g.current_user, **kwargs))

        return jsonify(ok=True)

    def do_save_schedules(self, params: dict):
        user = g.current_user
        now = datetime.now()
        saved = {s.job_name: s for s in ScheduledJob.query.filter_by(user_id=user.id).all()}

        for info in self.available_jobs():
            job_name, param = info.name, info.param

            try:
                hour = int(params.get(f"hour_{job_name}", 0))
            except ValueError:
                return f"Invalid hour for {job_name}", 400
            if not 0 <= hour <= 23:
                return f"Invalid hour for {job_name}", 400

            job_params = {}
            if param:
                try:
                    value = float(params.get(f"param_{job_name}", param.default))
                except ValueError:
                    return f"Invalid value for {job_name}", 400
                if not math.isfinite(value) or value < 0:
                    return f"Invalid value for {job_name}", 400
                job_params[param.name] = value

            schedule = saved.get(job_name)
            if schedule is None:
                schedule = ScheduledJob(user_id=user.id, job_name=job_name)
                db.session.add(schedule)

            schedule.enabled = params.get(f"enabled_{job_name}") == "on"
            schedule.hour = hour
            schedule.params = job_params
            apply_schedule_start(schedule, now)

        db.session.commit()
        flash("Schedule saved")

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
                last_visit_cities.update([(tuple(city), f"{stat.year}-{stat.month:02d}-{stat.day:02d}") for city in stat.visited_cities])
            if stat.visited_countries:
                stats_by_year[year]["countries"].update(stat.visited_countries)
                last_visit_countries.update([(country, f"{stat.year}-{stat.month:02d}-{stat.day:02d}") for country in stat.visited_countries])

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
                "monthly_distances": [round(dist_m / 1000, 1) for dist_m in data["monthly_distances"]],
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
                last_visit_cities.update([(tuple(city), f"{stat.year}-{stat.month:02d}-{stat.day:02d}") for city in stat.visited_cities])
            if stat.visited_countries:
                stats_by_month[month]["countries"].update(stat.visited_countries)
                last_visit_countries.update([(country, f"{stat.year}-{stat.month:02d}-{stat.day:02d}") for country in stat.visited_countries])

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
                "monthly_distances": [round(dist_m / 1000, 1) for dist_m in data["monthly_distances"]],
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
        
        # Retrieve query parameters
        the_date_str = request.args.get("date")
        import_id = request.args.get("importid")
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

        # An import spans many days, so it is shown as a whole instead of per day.
        points_query = GPSData.query.filter_by(**g.trace_query)
        if import_id:
            try:
                points_query = points_query.filter_by(import_id=uuid.UUID(import_id))
            except ValueError:
                import_id = None
        if not import_id:
            points_query = points_query.filter(GPSData.timestamp >= start_date, GPSData.timestamp < end_date)

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
                    GPSData.query\
                        .filter_by(**g.trace_query)\
                        .filter(GPSData.id.in_(selected_ids)
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
        running_imports = {job.import_id for job in job_manager.get_jobs(user.id) if isinstance(job, ImportJob) and not job.done}

        imports = []
        for imp in raw_imports:
            imports.append({
                "id": imp.id,
                "name": imp.original_filename,
                "created_at": imp.created_at,
                "total_entries": imp.total_entries,
                "total_imported": GPSData.query.filter_by(**g.trace_query, import_id=imp.id).count(),
                "done_importing": imp.done_importing,
                "importing": imp.id in running_imports,
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
            flash("Choose a file to upload", "error")
            return redirect(url_for("web.imports"))

        # Optional check for allowed extension
        filename = secure_filename(file.filename)
        name = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        if ext.replace(".", "") not in self.ALLOWED_EXTENSIONS:
            flash("Only .json files can be imported", "error")
            return redirect(url_for("web.imports"))

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
            os.remove(save_path)
            flash("This file is not valid JSON", "error")
            return redirect(url_for("web.imports"))

        if not isinstance(json_data, list):
            os.remove(save_path)
            flash("The file must contain a list of points", "error")
            return redirect(url_for("web.imports"))
        
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
        flash(f"Uploaded {filename} with {len(json_data):,} entries. Start the import when ready.")

        return redirect(url_for("web.imports"))

    def start_import_job(self, user, trace=None):
        """Queue a background job to parse and insert data from the specified import."""
        import_id = request.form.get("import_id")
        if not import_id:
            return "Missing import_id", 400

        import_record = Import.query.filter_by(id=import_id, user_id=user.id).first()
        if not import_record and trace:
            import_record = Import.query.filter_by(id=import_id, trace_id=trace.id).first()
        if not import_record:
            return "Import not found or not yours", 404

        # Add job to job manager
        # The job itself (ImportJob) will handle reading the file from disk,
        # validating JSON, inserting GPSData, etc.
        job_instance = ImportJob(user=user, import_obj=import_record, trace=trace)
        job_manager.add_job(job_instance)
        flash("Import started. You can follow it on the Jobs page.")

        return redirect(url_for("web.imports"))

    def delete_import(self, user):
        """Deletes an import record AND all associated GPSData. Asks user for confirmation in the HTML form."""
        import_id = request.form.get("import_id")
        if not import_id:
            return "Missing import_id", 400

        import_record = Import.query.filter_by(id=import_id, user_id=user.id).first()
        if not import_record and g.current_trace:
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

        flash("Import and its points deleted")
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

        if res.status_code != 200:
            raise Exception(f"Error fetching geocode: {res.status_code} - {res.text}")

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
        
        elif not fetch_interpolated: # or (time_delta != 0 and time_delta < 60 * 60 * 25):
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
                "uid": str(row[1]) if row[1] else None,
                "t": row[2].replace(tzinfo=timezone.utc).isoformat() if row[2] else None,
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

        data = request.get_json(silent=True)
        if not data or "ids" not in data:
            return "Missing 'id' query param or 'ids' array in JSON body", 400

        ids = data["ids"]
        if not isinstance(ids, list) or not ids:
            return "'ids' must be a non-empty array of IDs", 400

        # Bulk-delete all matching points belonging to the user
        # (and matching any extra filters in g.trace_query)
        points = GPSData.query.filter_by(**g.trace_query).filter(GPSData.id.in_(ids)).all()
        if not points:
            return "No points found for the given IDs", 404
        
        for point in points:
            db.session.delete(point)
        db.session.commit()

        return jsonify({"deleted_ids": ids}), 200
    
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

        data = GPSData.query.with_entities(
            func.string_agg(
                func.concat(
                    cast(GPSData.latitude * 10000, Integer), 
                    ',', 
                    cast(GPSData.longitude * 10000, Integer)
                ),
                '\n'
            )
        ).filter_by(**g.trace_query).scalar()

        return self.compress(data)
    
    def compress(self, data):
        data = (data or '').encode('utf-8')
        content = gzip.compress(data, 9)
        response = make_response(content)
        response.headers['Content-length'] = len(content)
        response.headers['Content-Original-Length'] = len(data)
        response.headers['Content-Type'] = 'text/plain'
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
        
        margin = 500
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
                "t": row.timestamp.replace(tzinfo=timezone.utc).isoformat() if row.timestamp else None,
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
        admin = g.current_user
        if not admin.is_admin:
            return "Access denied", 403

        action = request.form.get("action")

        def fail(message, status=400):
            if action == "change_password":
                return jsonify(error=message), status
            flash(message, "error")
            return redirect(url_for("web.manage_users"))

        if action == "add_user":
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password")
            is_admin = (request.form.get("is_admin") or "").lower() == "true"

            if not email or not password:
                return fail("Enter an email and a password")
            if User.query.filter_by(email=email).first():
                return fail("A user with that email already exists")

            new_user = User(email=email, is_admin=is_admin)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            flash(f"User {email} added")
            return redirect(url_for("web.manage_users"))

        target = ManageTracesView.get_target_user(request.form.get("user_id"))
        if not target:
            return fail("User not found", 404)

        if action == "remove_user":
            if target.id == admin.id:
                return fail("You cannot remove your own account")

            for trace in AdditionalTrace.query.filter_by(owner_id=target.id).all():
                delete_trace(trace)
            for trace in AdditionalTrace.query.all():
                if str(target.id) in trace.share_with_list:
                    trace.share_with_list.remove(str(target.id))
            delete_series_data("user_id", target.id)
            db.session.delete(target)
            db.session.commit()
            flash(f"User {target.email} and all their data removed")

        elif action == "change_password":
            password = request.form.get("password")
            if not password:
                return fail("Enter a password")

            target.set_password(password)
            db.session.commit()
            return jsonify(ok=True)

        else:
            return fail("Unknown action")

        return redirect(url_for("web.manage_users"))
    
class ManageTracesView(MethodView):
    decorators = [login_required]

    def get(self):
        user = g.current_user

        available_traces = AdditionalTrace.query.filter_by(owner_id=user.id).all()

        if user.is_admin:
            users = [(str(u.id), u.email) for u in User.query.all()]
        else:
            # Non-admins only see accounts their own traces are already shared with,
            # so the full user directory is not exposed to everyone.
            shared_uuids = []
            for trace in available_traces:
                for shared_id in trace.share_with_list:
                    try:
                        shared_uuids.append(uuid.UUID(str(shared_id)))
                    except (ValueError, TypeError):
                        continue
            users = (
                [(str(u.id), u.email) for u in User.query.filter(User.id.in_(shared_uuids)).all()]
                if shared_uuids else []
            )

        return render_template(
            "manage_traces.jinja",
            available_traces=available_traces,
            users=users,
            current_user_id=str(user.id),
            can_list_users=bool(user.is_admin),
        )

    @staticmethod
    def get_owned_trace(trace_id, owner: User):
        """Return the trace only if it exists and is owned by `owner`, else None."""
        try:
            trace_uuid = uuid.UUID(str(trace_id))
        except (ValueError, AttributeError, TypeError):
            return None
        return AdditionalTrace.query.filter_by(id=trace_uuid, owner_id=owner.id).first()

    @staticmethod
    def get_target_user(user_id):
        try:
            user_uuid = uuid.UUID(str(user_id))
        except (ValueError, AttributeError, TypeError):
            return None
        return User.query.filter_by(id=user_uuid).first()

    @staticmethod
    def resolve_target_user():
        """Resolve the other party from either a user id (admin picker) or an email address."""
        user_id = request.form.get("user_id")
        if user_id:
            return ManageTracesView.get_target_user(user_id)

        email = (request.form.get("user_email") or "").strip()
        if email:
            return User.query.filter_by(email=email).first()

        return None

    def post(self):
        user = g.current_user
        action = request.form.get("action")

        def fail(message):
            flash(message, "error")
            return redirect(url_for("web.manage_traces"))

        if action == "add_trace":
            name = (request.form.get("name") or "").strip()
            if not name:
                return fail("Enter a name for the trace")

            db.session.add(AdditionalTrace(name=name, owner_id=user.id, description=request.form.get("description")))
            db.session.commit()
            flash(f"Trace “{name}” created")
            return redirect(url_for("web.manage_traces"))

        trace = self.get_owned_trace(request.form.get("trace_id"), user)
        if not trace:
            return fail("Trace not found")

        if action == "remove_trace":
            if g.current_trace and g.current_trace.id == trace.id:
                session.pop("trace_id", None)
            delete_trace(trace)
            db.session.commit()
            flash(f"Trace “{trace.name}” deleted")

        elif action == "share_trace":
            target = self.resolve_target_user()
            if not target:
                return fail("No user with that email")
            if target.id == user.id:
                return fail("You already own this trace")

            if str(target.id) not in trace.share_with_list:
                trace.share_with_list.append(str(target.id))
                db.session.commit()
            flash(f"Shared “{trace.name}” with {target.email}")

        elif action == "unshare_trace":
            user_id = request.form.get("user_id")
            if user_id in trace.share_with_list:
                trace.share_with_list.remove(user_id)
                db.session.commit()
                flash("Sharing removed")

        elif action == "transfer_trace":
            target = self.resolve_target_user()
            if not target:
                return fail("No user with that email")
            if target.id == user.id:
                return fail("You already own this trace")

            trace.owner_id = target.id
            db.session.commit()
            flash(f"“{trace.name}” now belongs to {target.email}")

        else:
            return fail("Unknown action")

        return redirect(url_for("web.manage_traces"))

class AccountView(MethodView):
    decorators = [login_required]

    MIN_CUSTOM_API_KEY_LENGTH = 32

    @staticmethod
    def get_accessible_trace(trace_id, user: User):
        """Return the trace only if `user` owns it or it is shared with them, else None."""
        try:
            trace_uuid = uuid.UUID(str(trace_id))
        except (ValueError, AttributeError, TypeError):
            return None

        trace: AdditionalTrace = AdditionalTrace.query.filter_by(id=trace_uuid).first()
        if not trace:
            return None
        if trace.owner_id != user.id and str(user.id) not in trace.share_with_list:
            return None
        return trace

    def get(self):
        user: User = g.current_user

        api_keys = user.api_keys
        traces = AdditionalTrace.query.filter_by(owner_id=user.id).all()

        api_key_list = []
        for key, trace_id in api_keys:
            name = "Main Trace"
            if trace_id:
                trace = self.get_accessible_trace(trace_id, user)
                name = trace.name if trace else "Unavailable Trace"

            api_key_list.append((key, trace_id, name))

        return render_template("account.jinja", api_keys=api_key_list, available_traces=traces)

    def add_custom_api_key(self, user: User):
        """Register a user-supplied API key. Keys must be long enough that they cannot be guessed."""
        custom_api_key = request.form.get("custom_api_key", "")

        if len(custom_api_key) < self.MIN_CUSTOM_API_KEY_LENGTH:
            return f"Custom API key must be at least {self.MIN_CUSTOM_API_KEY_LENGTH} characters", 400

        # API keys must be globally unique so lookups resolve to exactly one user.
        # The response is deliberately generic so this cannot be used to probe other users' keys.
        candidate = custom_api_key.encode("utf-8")
        for other in User.query.all():
            for existing_key, _ in other.api_keys:
                if secrets.compare_digest(str(existing_key).encode("utf-8"), candidate):
                    return "Could not add API key", 400

        user.api_keys.append((custom_api_key, None))
        db.session.commit()
        return "OK", 200

    def post(self):
        user: User = g.current_user

        if "custom_api_key" in request.form:
            return self.add_custom_api_key(user)

        if "generate_key" in request.form:
            trace_id = request.form.get("trace_id") or None
            if trace_id:
                trace = self.get_accessible_trace(trace_id, user)
                trace_id = str(trace.id) if trace else None

            user.api_keys.append((uuid.uuid4().hex, trace_id))
            db.session.commit()
            flash("API key created")

        if "delete_key" in request.form:
            api_key = request.form.get("api_key")
            if api_key:
                user.api_keys = [(key, trace_id) for key, trace_id in user.api_keys if key != api_key]
                db.session.commit()
                flash("API key deleted")

        if "update_account" in request.form:
            new_email = request.form.get("new_email")
            new_password = request.form.get("new_password")
            if new_email and new_email != user.email:
                if User.query.filter_by(email=new_email).first():
                    flash("That email is already used by another account", "error")
                    return redirect(url_for("web.account"))
                user.email = new_email
            if new_password:
                user.set_password(new_password)
            db.session.commit()
            if new_email or new_password:
                flash("Account updated")
        
        return redirect(url_for("web.account"))
    


class SetTraceView(MethodView):
    decorators = [login_required]

    def post(self):
        trace_id = request.form.get("trace_id")
        if not trace_id:
            session.pop("trace_id", None)
            return "OK", 205

        trace = AdditionalTrace.query.filter_by(id=trace_id).first()
        if not trace:
            return "Trace not found", 404

        if trace.owner_id != g.current_user.id and str(g.current_user.id) not in trace.share_with_list:
            return "Access denied", 403

        session["trace_id"] = trace_id

        return "OK", 200



# Rendered page backdrops per user id: (rendered at, PNG bytes).
backdrop_store: dict = {}
BACKDROP_MAX_AGE = timedelta(hours=12)


class FullBleedBackground(MethodView):
    """
    The page backdrop: the user's tracks around their latest position, colored by
    speed, on a transparent background so it suits light and dark mode.
    """
    decorators = [login_required]

    WIDTH, HEIGHT = 1600, 900
    # Thin lines are drawn this many times larger, softened and scaled down.
    SUPERSAMPLE = 2
    LINE_WIDTH = 1
    BLUR_RADIUS = 1
    # Half the width of the image, in meters.
    RADIUS_M = 5000.0
    # Longer steps between points are gaps in tracking, not travelled lines.
    MAX_SEGMENT_M = 100.0
    SPEED_STOPS = [
        (0, (0, 0, 255)),
        (30, (0, 255, 0)),
        (60, (255, 255, 0)),
        (90, (255, 0, 0)),
    ]

    def get(self):
        user_id = g.current_user.id

        cached = backdrop_store.get(user_id)
        if not cached or datetime.now() - cached[0] > BACKDROP_MAX_AGE:
            image = self.render(user_id)
            if image is None:
                return "", 204
            buffer = BytesIO()
            image.save(buffer, "PNG", optimize=True)
            cached = backdrop_store[user_id] = (datetime.now(), buffer.getvalue())

        response = make_response(cached[1])
        response.headers["Content-Type"] = "image/png"
        # Rendered from the user's own location history, so it must never enter a shared cache.
        response.headers["Cache-Control"] = "private, max-age=43200"
        return response

    def render(self, user_id) -> Image.Image | None:
        last = db.session.execute(
            select(GPSData.latitude, GPSData.longitude)
            .where(GPSData.user_id == user_id)
            .order_by(GPSData.timestamp.desc())
            .limit(1)
        ).first()
        if not last:
            return None

        center_lat, center_lng = last
        meters_per_deg_lat = 111320.0
        meters_per_deg_lng = 111320.0 * max(0.01, math.cos(math.radians(center_lat)))
        half_width_m = self.RADIUS_M
        half_height_m = self.RADIUS_M * self.HEIGHT / self.WIDTH

        rows = db.session.execute(
            select(GPSData.latitude, GPSData.longitude, GPSData.timestamp, GPSData.speed)
            .where(
                GPSData.user_id == user_id,
                GPSData.latitude.between(center_lat - half_height_m / meters_per_deg_lat, center_lat + half_height_m / meters_per_deg_lat),
                GPSData.longitude.between(center_lng - half_width_m / meters_per_deg_lng, center_lng + half_width_m / meters_per_deg_lng),
            )
            .order_by(GPSData.timestamp)
        ).all()

        width, height = self.WIDTH * self.SUPERSAMPLE, self.HEIGHT * self.SUPERSAMPLE
        scale = width / (2 * half_width_m)

        def to_pixel(lat, lng):
            return (
                width / 2 + (lng - center_lng) * meters_per_deg_lng * scale,
                height / 2 - (lat - center_lat) * meters_per_deg_lat * scale,
            )

        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for a, b in zip(rows, rows[1:]):
            distance = self.haversine(a.latitude, a.longitude, b.latitude, b.longitude)
            if distance > self.MAX_SEGMENT_M:
                continue

            if a.speed is not None and b.speed is not None and a.speed >= 0 and b.speed >= 0:
                speed = (a.speed + b.speed) / 2
            else:
                seconds = (b.timestamp - a.timestamp).total_seconds()
                speed = distance / seconds if seconds > 0 else 0

            draw.line([to_pixel(a.latitude, a.longitude), to_pixel(b.latitude, b.longitude)],
                      fill=self.speed_color(speed), width=self.LINE_WIDTH)

        image = image.filter(ImageFilter.GaussianBlur(radius=self.BLUR_RADIUS))
        return image.resize((self.WIDTH, self.HEIGHT), Image.LANCZOS)

    def speed_color(self, speed_mps: float) -> tuple[int, int, int]:
        kmh = max(0.0, speed_mps * 3.6)
        for (low, low_color), (high, high_color) in zip(self.SPEED_STOPS, self.SPEED_STOPS[1:]):
            if kmh <= high:
                f = (kmh - low) / (high - low)
                return tuple(round(c1 + (c2 - c1) * f) for c1, c2 in zip(low_color, high_color))
        return self.SPEED_STOPS[-1][1]

    @staticmethod
    def haversine(lat1, lon1, lat2, lon2):
        """Distance in meters between two points."""
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)
        a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
        return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))



# Tiles contain private location data, so entries are keyed by the owning
# user/trace and never shared between them.
map_tile_data_store = OrderedDict()
MAP_TILE_CACHE_MAX_ENTRIES = 2048

class MapTileView(MethodView):
    """Serve /tiles/<z>/<x>/<y>.png as a 256x256 PNG."""

    decorators = [login_required]

    # -----------------------------------------------------------------------
    #  Tunables
    # -----------------------------------------------------------------------
    TILE_SIZE = 256           # px
    MAX_POINT_DISTANCE = 100  # NEW ▸ metres – beyond this no line, only dots

    Point = namedtuple("Point", "lat lon speed ts")

    # -----------------------------------------------------------------------
    #  Slippy‑map helpers (unchanged)
    # -----------------------------------------------------------------------
    EARTH_R = 6378137.0

    @staticmethod
    def tile_bounds(z, x, y):
        n = 2 ** z
        lon_w = x / n * 360.0 - 180.0
        lon_e = (x + 1) / n * 360.0 - 180.0

        def merc2lat(my): return math.degrees(math.atan(math.sinh(my)))

        lat_n = merc2lat(math.pi * (1 - 2 * y / n))
        lat_s = merc2lat(math.pi * (1 - 2 * (y + 1) / n))

        # extend bounds to 1 times tile width in each direction
        lon_w -= (lon_e - lon_w) / 2
        lon_e += (lon_e - lon_w) / 2
        lat_s -= (lat_n - lat_s) / 2
        lat_n += (lat_n - lat_s) / 2

        # clamp to valid lat/lon
        lat_s = max(lat_s, -85.05112878)
        lat_n = min(lat_n, 85.05112878)
        lon_w = max(lon_w, -180)
        lon_e = min(lon_e, 180)

        return lon_w, lat_s, lon_e, lat_n

    @staticmethod
    def latlon_to_pixel(lat, lon, z):
        n = 2 ** z * MapTileView.TILE_SIZE
        x = (lon + 180) / 360 * n
        siny = math.sin(math.radians(lat))
        y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * n
        return x, y

    # -----------------------------------------------------------------------
    #  Colour logic (unchanged)
    # -----------------------------------------------------------------------
    class ColorStop:
        def __init__(self, s, rgb): self.s, self.c = s, rgb

    COLOR_STOPS = [
        ColorStop(0, (0, 0, 255)),
        ColorStop(30, (0, 255, 0)),
        ColorStop(60, (255, 255, 0)),
        ColorStop(90, (255, 0, 0)),
    ]

    @classmethod
    def colour_for_speed(cls, mps):
        kmh = mps * 3.6
        st = cls.COLOR_STOPS
        if kmh <= st[0].s:
            return st[0].c
        for a, b in zip(st[:-1], st[1:]):
            if kmh <= b.s:
                r = (kmh - a.s) / (b.s - a.s)
                return tuple(round(a.c[i] + (b.c[i] - a.c[i]) * r) for i in range(3))
        return st[-1].c

    # -----------------------------------------------------------------------
    def get(self, z: int, x: int, y: int):
        if "trace_id" in g.trace_query:
            owner_column, owner_id = "trace_id", g.trace_query["trace_id"]
        else:
            owner_column, owner_id = "user_id", g.trace_query["user_id"]

        cache_key = (owner_column, str(owner_id), z, x, y)

        # Check if the tile is already cached
        if cache_key in map_tile_data_store:
            map_tile_data_store.move_to_end(cache_key)
            return self.send_png(map_tile_data_store[cache_key])

        w, s, e, n = self.tile_bounds(z, x, y)

        sql = text(
            f"""SELECT latitude, longitude, speed, "timestamp"
               FROM gps_data
               WHERE {owner_column}=:oid
                 AND latitude BETWEEN :s AND :n
                 AND longitude BETWEEN :w AND :e
               ORDER BY "timestamp" """
        )
        rows = db.session.execute(sql, dict(oid=owner_id, s=s, n=n, w=w, e=e)).fetchall()
        if not rows:
            img = Image.new("RGBA", (self.TILE_SIZE, self.TILE_SIZE), (0, 0, 0, 0))
            return self.send_png(img)

        pts = [self.Point(r.latitude, r.longitude, r.speed, r.timestamp) for r in rows]
        img = self.render_tile(pts, z, x, y)

        # Cache the tile
        map_tile_data_store[cache_key] = img
        map_tile_data_store.move_to_end(cache_key)
        while len(map_tile_data_store) > MAP_TILE_CACHE_MAX_ENTRIES:
            map_tile_data_store.popitem(last=False)

        return self.send_png(img)

    @staticmethod
    def send_png(img: Image.Image):
        buf = BytesIO(); img.save(buf, "PNG"); buf.seek(0)
        response = send_file(buf, mimetype="image/png")
        response.headers["Cache-Control"] = "private, no-store"
        return response

    # -----------------------------------------------------------------------
    #  Rendering – now honours MAX_POINT_DISTANCE
    # -----------------------------------------------------------------------
    def render_tile(self, pts, z, tx, ty):
        img = Image.new("RGBA", (self.TILE_SIZE, self.TILE_SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        ox, oy = tx * self.TILE_SIZE, ty * self.TILE_SIZE
        r_dot = 1  # dot radius (px)

        for a, b in zip(pts[:-1], pts[1:]):
            xa, ya = self.latlon_to_pixel(a.lat, a.lon, z)
            xb, yb = self.latlon_to_pixel(b.lat, b.lon, z)

            # translate to tile‑local
            xa -= ox; ya -= oy; xb -= ox; yb -= oy

            # colour for this pair / point
            speed_val = (
                (a.speed + b.speed) / 2.0
                if a.speed is not None and b.speed is not None
                else self.haversine(a.lat, a.lon, b.lat, b.lon)
                   / max((b.ts - a.ts).total_seconds(), 1)
            )
            colour = self.colour_for_speed(speed_val)

            dist = self.haversine(a.lat, a.lon, b.lat, b.lon)

            if dist > self.MAX_POINT_DISTANCE:           # NEW -------------
                # draw dots instead of a line
                for xx, yy in [(xa, ya), (xb, yb)]:
                    draw.ellipse(
                        (xx - r_dot, yy - r_dot, xx + r_dot, yy + r_dot),
                        fill=colour,
                        outline=None,
                    )
                continue

            # regular line for close pairs
            draw.line([(xa, ya), (xb, yb)], fill=colour, width=2)

        img = img.filter(ImageFilter.GaussianBlur(0.6))
        return img

    # -----------------------------------------------------------------------
    @staticmethod
    def haversine(lat1, lon1, lat2, lon2):
        R = 6371000.0
        dlat, dlon = map(math.radians, (lat2 - lat1, lon2 - lon1))
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) *
             math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))



# Register the class-based views with the Blueprint
web_bp.add_url_rule("/", view_func=HomeView.as_view("home"))
web_bp.add_url_rule("/login", view_func=LoginView.as_view("login"), methods=["GET", "POST"])
web_bp.add_url_rule("/logout", view_func=LogoutView.as_view("logout"))
web_bp.add_url_rule("/set_trace_id", view_func=SetTraceView.as_view("set_trace_id"), methods=["POST"])
web_bp.add_url_rule("/full_bleed_background.png", view_func=FullBleedBackground.as_view("full_bleed_background"))
web_bp.add_url_rule("/tiles/<int:z>/<int:x>/<int:y>.png", view_func=MapTileView.as_view("map_tile"))
web_bp.add_url_rule("/map", view_func=MapView.as_view("map"), methods=["GET", "POST", "DELETE"])
web_bp.add_url_rule("/map/heatmap_data.csv", view_func=HeatMapDataView.as_view("heatmap_data"))
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
