from collections import deque
from datetime import datetime
import threading
import time
import traceback

from flask import Flask

from ..extensions import db
from ..models import ScheduledJob, User
from ..config import Config
from .jobs import JOB_TYPES, Job, JobCancelled, JobStatus


SCHEDULE_CHECK_INTERVAL_SECONDS = 30

# Schedules every user starts with, matching the previous built-in nightly run.
DEFAULT_SCHEDULES = [
    ("full_stats", 0),
    ("photon_fill", 0),
]


def apply_schedule_start(schedule: ScheduledJob, now: datetime):
    """
    Skip today's run if the scheduled hour has already passed, so enabling a
    schedule or moving it to an earlier hour does not start the job immediately.
    """
    if schedule.enabled and schedule.hour < now.hour and schedule.last_run_date != now.date():
        schedule.last_run_date = now.date()




class JobManager:
    # Finished jobs kept to show their outcome on the jobs page.
    HISTORY_SIZE = 50

    def __init__(self):
        self.config = Config
        self.web_app: Flask = None
        self.queued_jobs: list[Job] = []
        self.running_jobs: list[Job] = []
        self.finished_jobs: deque[Job] = deque(maxlen=self.HISTORY_SIZE)
        self.lock = threading.RLock()
        self.wake = threading.Event()

        self.running = False
        self.stop_requested = False

    def set_config(self, config: Config):
        self.config = config

    def set_web_app(self, app):
        self.web_app = app

    def get_jobs(self, user_id=None) -> list[Job]:
        """Running, queued and recently finished jobs, newest first; only the user's if given."""
        with self.lock:
            jobs = self.running_jobs + self.queued_jobs + list(reversed(self.finished_jobs))
        return [job for job in jobs if user_id is None or job.user_id == user_id]

    def get_job(self, job_id) -> Job | None:
        return next((job for job in self.get_jobs() if job.id == job_id), None)

    def run_safely(self, job: Job):
        print(f"Running job {job.__class__.__name__}...")
        try:
            with self.web_app.app_context():
                job.run()
            job.status = JobStatus.CANCELLED if job.stop_requested else JobStatus.DONE
            job.progress = 1.0 if job.status == JobStatus.DONE else job.progress
        except JobCancelled:
            job.status = JobStatus.CANCELLED
        except Exception as e:
            print(traceback.format_exc())
            job.status = JobStatus.FAILED
            job.error = str(e) or e.__class__.__name__

        job.end_time = time.time()
        print(f"Job {job.__class__.__name__} {job.status} after {job.end_time - job.start_time:.3f} seconds.")
        self.wake.set()

    def can_start(self, job: Job, running: list[Job]) -> bool:
        if len(running) >= self.config.BACKGROUND_MAX_THREADS:
            return False
        # One job at a time per user and lane, so jobs never change the same data at once.
        if any(other.user_id == job.user_id and other.LANE == job.LANE for other in running):
            return False
        if job.GLOBAL_LIMIT is not None and sum(type(other) is type(job) for other in running) >= job.GLOBAL_LIMIT:
            return False
        return True

    def start_jobs(self):
        with self.lock:
            for job in [job for job in self.running_jobs if job.done]:
                self.running_jobs.remove(job)
                self.finished_jobs.append(job)

            # Queued jobs start in order, but a job that has to wait does not hold up others.
            for job in list(self.queued_jobs):
                if not self.can_start(job, self.running_jobs):
                    continue
                self.queued_jobs.remove(job)
                job.status = JobStatus.RUNNING
                job.start_time = time.time()
                job.thread = threading.Thread(target=self.run_safely, args=(job,), daemon=True)
                self.running_jobs.append(job)
                job.thread.start()

    def run(self):
        last_schedule_check = 0
        self.running = True
        while not self.stop_requested:
            self.wake.clear()
            try:
                self.start_jobs()

                if time.time() - last_schedule_check >= SCHEDULE_CHECK_INTERVAL_SECONDS:
                    last_schedule_check = time.time()
                    self.check_scheduled_jobs()
            except Exception:
                print(traceback.format_exc())

            self.wake.wait(timeout=1)

        self.running = False

    def has_job(self, user: User, job_type: type) -> bool:
        """Whether a job of this type is already queued or running for the user."""
        with self.lock:
            return any(
                type(job) is job_type and job.user_id == user.id
                for job in self.queued_jobs + self.running_jobs
            )

    def create_default_schedules(self, now: datetime):
        """Give users that have no schedules yet the default ones."""
        users_with_schedules = db.session.query(ScheduledJob.user_id)
        for user in User.query.filter(~User.id.in_(users_with_schedules)).all():
            for job_name, hour in DEFAULT_SCHEDULES:
                schedule = ScheduledJob(user_id=user.id, job_name=job_name, enabled=True, hour=hour, params={})
                apply_schedule_start(schedule, now)
                db.session.add(schedule)

    def check_scheduled_jobs(self):
        """
        Queue every enabled schedule whose hour has been reached today and that
        has not run yet today. A run missed while the server was down is caught
        up later the same day.
        """
        now = datetime.now()
        today = now.date()

        with self.web_app.app_context():
            self.create_default_schedules(now)

            due = ScheduledJob.query.filter(
                ScheduledJob.enabled.is_(True),
                ScheduledJob.hour <= now.hour,
                (ScheduledJob.last_run_date.is_(None)) | (ScheduledJob.last_run_date != today),
            ).all()

            to_queue = []
            for schedule in due:
                # Marked as run even if skipped below, so a failing schedule is not retried all day.
                schedule.last_run_date = today
                if schedule.job_name in JOB_TYPES:
                    to_queue.append((schedule.user_id, schedule.job_name, dict(schedule.params or {})))

            db.session.commit()

            # Load users after the commit, which would otherwise expire them before the jobs use them.
            for user_id, job_name, params in to_queue:
                user = db.session.get(User, user_id)
                job_type = JOB_TYPES[job_name]
                if user is None or self.has_job(user, job_type):
                    continue

                try:
                    self.add_job(job_type(user=user, **params))
                except Exception:
                    print(f"Could not create scheduled job {job_name} for user {user_id}:")
                    print(traceback.format_exc())

    def stop(self, blocking=False):
        self.stop_requested = True
        with self.lock:
            for job in self.queued_jobs:
                job.status = JobStatus.CANCELLED
            self.queued_jobs.clear()
            running = list(self.running_jobs)
        for job in running:
            job.stop()
        self.wake.set()

        if blocking:
            for job in running:
                job.thread.join()
            while self.running:
                time.sleep(0.1)

    def add_job(self, job: Job):
        job.set_config(self.config)
        job.set_web_app(self.web_app)
        with self.lock:
            self.queued_jobs.append(job)
        self.wake.set()

    def cancel_job(self, job_id) -> bool:
        """Cancel a queued job, or ask a running one to stop at its next checkpoint."""
        with self.lock:
            for job in self.queued_jobs:
                if job.id == job_id:
                    self.queued_jobs.remove(job)
                    job.status = JobStatus.CANCELLED
                    job.end_time = time.time()
                    self.finished_jobs.append(job)
                    return True

            for job in self.running_jobs:
                if job.id == job_id:
                    job.stop()
                    return True

        return False

job_manager = JobManager()
