import threading
import time
import traceback

from flask import Flask

from ..models import User
from ..config import Config
from .jobs import ConcurrencyLimitType, GenerateFullStatisticsJob, Job, PhotonFillJob




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
        last_day = time.localtime().tm_mday
        self.running = True
        while True:
            if self.stop_requested:
                break

            try:
                if len(self.threads) < self.config.BACKGROUND_MAX_THREADS:
                    for user in User.query.all():
                        concurrent_types = [job.concurrency_limit_type for job in self.running_jobs if job.concurrency_limit_type != None and job.user in [None, user]]
                        if self.queued_jobs and ConcurrencyLimitType.GLOBAl not in concurrent_types:
                            to_remove = []
                            for job in self.queued_jobs:
                                if job.concurrency_limit_type in concurrent_types and job.user not in [None, user]:
                                    continue

                                job.running = True
                                job.start_time = time.time()
                                job.thread = threading.Thread(target=self.run_safely, args=(job,))
                                job.thread.start()
                                self.threads.append(job.thread)
                                self.running_jobs.append(job)
                                to_remove.append(job)

                            for job in to_remove:
                                self.queued_jobs.remove(job)

                for job in self.running_jobs:
                    if job.done:
                        try:
                            self.threads.remove(job.thread)
                        except ValueError:
                            pass

                self.running_jobs = [job for job in self.running_jobs if not job.done]


                if time.localtime().tm_mday != last_day:
                    self.check_for_daily_jobs()
                    last_day = time.localtime().tm_mday

            except Exception:
                print(traceback.format_exc())

            time.sleep(0.1)

        self.running = False

    def check_for_daily_jobs(self):
        with self.web_app.app_context():
            for user in User.query.all():
                if len(self.config.PHOTON_SERVER_HOST) > 0:
                    if PhotonFillJob.__name__ not in [job.__class__.__name__ for job in self.queued_jobs + self.running_jobs]:
                        self.add_job(PhotonFillJob(user))

                if GenerateFullStatisticsJob.__name__ not in [job.__class__.__name__ for job in self.queued_jobs + self.running_jobs]:
                    self.add_job(GenerateFullStatisticsJob(user))

            

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