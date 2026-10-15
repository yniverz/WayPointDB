from datetime import datetime
import threading
import time
import traceback

from flask import Flask

from ..extensions import db
from ..models import ScheduledJob, User
from ..config import Config
from .jobs import JOB_TYPES, ConcurrencyLimitType, Job


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
    def __init__(self):
        self.config = Config
        self.web_app: Flask = None
        self.queued_jobs: list[Job] = []
        self.running_jobs: list[Job] = []
        self.threads = []

        self.running = False
        self.stop_requested = False

    def set_config(self, config: Config):
        self.config = config

    def set_web_app(self, app):
        self.web_app = app

    def get_jobs(self):
        """
        Return a list of tuples with the job name, done status, and progress.
        
        Example:
        [
            ("QueryPhotonJob", False, 50.4),
            ("QueryPhotonJob", True, 100.0),
            ...
        ]
        """ 
        return [(job.user, job.__class__.__name__, job.running, job.progress, job.start_time) for job in self.queued_jobs + self.running_jobs]

    def run_safely(self, job: Job):
        print(f"Running job {job.__class__.__name__}...")
        try:
            with self.web_app.app_context():
                job.run()
        except Exception:
            print(traceback.format_exc())

        job.done = True

        print(f"Job {job.__class__.__name__} finished in {time.time() - job.start_time:.3f} seconds.")

    def run(self):
        last_schedule_check = 0
        self.running = True
        while True:
            if self.stop_requested:
                break

            try:
                if len(self.threads) < self.config.BACKGROUND_MAX_THREADS:
                    running_global_types = [job.concurrency_limit_type for job in self.running_jobs if job.user is None]
                    running_user_types = [str(job.user.email)+str(job.concurrency_limit_type) for job in self.running_jobs if job.user is not None]


                    for job in self.queued_jobs:
                        if job.concurrency_limit_type in running_global_types or (job.user is not None and str(job.user.email)+str(job.concurrency_limit_type) in running_user_types):
                            continue

                        job.running = True
                        job.start_time = time.time()
                        job.thread = threading.Thread(target=self.run_safely, args=(job,))
                        job.thread.start()
                        self.threads.append(job.thread)
                        self.running_jobs.append(job)

                    self.queued_jobs = [job for job in self.queued_jobs if job not in self.running_jobs]





                    # for user in [job.user for job in self.queued_jobs + self.running_jobs]:
                    #     concurrent_types = [job.concurrency_limit_type for job in self.running_jobs if job.concurrency_limit_type != None and job.user in [None, user]]
                    #     if self.queued_jobs and ConcurrencyLimitType.GLOBAl not in concurrent_types:
                    #         to_remove = []
                    #         for job in self.queued_jobs:
                    #             if job.concurrency_limit_type in concurrent_types and job.user not in [None, user]:
                    #                 continue

                    #             job.running = True
                    #             job.start_time = time.time()
                    #             job.thread = threading.Thread(target=self.run_safely, args=(job,))
                    #             job.thread.start()
                    #             self.threads.append(job.thread)
                    #             self.running_jobs.append(job)
                    #             to_remove.append(job)

                    #         for job in to_remove:
                    #             self.queued_jobs.remove(job)

                for job in self.running_jobs:
                    if job.done:
                        try:
                            self.threads.remove(job.thread)
                        except ValueError:
                            pass

                self.running_jobs = [job for job in self.running_jobs if not job.done]


                if time.time() - last_schedule_check >= SCHEDULE_CHECK_INTERVAL_SECONDS:
                    last_schedule_check = time.time()
                    self.check_scheduled_jobs()

            except Exception:
                print(traceback.format_exc())

            time.sleep(0.1)

        self.running = False

    def has_job(self, user: User, job_type: type) -> bool:
        """Whether a job of this type is already queued or running for the user."""
        return any(
            type(job) is job_type and job.user is not None and job.user.id == user.id
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
        self.queued_jobs.clear()
        for job in self.running_jobs:
            job.stop(blocking)

        if blocking:
            while self.running:
                time.sleep(0.1)

    def add_job(self, job: Job):
        job.set_config(self.config)
        job.set_web_app(self.web_app)
        self.queued_jobs.append(job)

    def cancel_job(self, job_id, blocking=False):
        print(f"Cancelling job {job_id}...")
        for job in self.running_jobs:
            if job.id == job_id:
                job.stop(blocking)
                return True
            
        for job in self.queued_jobs:
            if job.id == job_id:
                self.queued_jobs.remove(job)
                return True

        return False

job_manager = JobManager()