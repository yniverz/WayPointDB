import threading
import time
import traceback

from flask import Flask

from backend.core.models import User
from ..config import Config
from .jobs import GenerateFullStatisticsJob, Job, PhotonFillJob




class JobManager:
    def __init__(self):
        self.config = Config
        self.web_app: Flask = None
        self.queued_jobs: list[Job] = []
        self.running_jobs: list[Job] = []
        self.threads = []

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
        return [(job.__class__.__name__, job.done, max(0.01, job.progress*100), job.start_time) for job in self.running_jobs]

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
        while True:
            try:
                if len(self.threads) < int(self.config.BACKGROUND_MAX_THREADS):
                    if self.queued_jobs:
                        job = self.queued_jobs.pop(0)
                        job.running = True
                        job.start_time = time.time()
                        thread = threading.Thread(target=self.run_safely, args=(job,))
                        thread.start()
                        self.threads.append(thread)
                        self.running_jobs.append(job)

                for job in self.running_jobs:
                    if job.done:
                        self.running_jobs.remove(job)
                        self.threads.remove(thread)

                if time.localtime().tm_mday != last_day:
                    self.check_for_daily_jobs()
                    last_day = time.localtime().tm_mday

            except Exception:
                print(traceback.format_exc())

            time.sleep(0.1)

    def check_for_daily_jobs(self):
        with self.web_app.app_context():
            for user in User.query.all():
                if len(self.config.PHOTON_SERVER_HOST) > 0:
                    job = PhotonFillJob(user)
                    self.add_job(job)

                job = GenerateFullStatisticsJob(user)
                self.add_job(job)

            

    def stop(self, blocking=False):
        self.queued_jobs.clear()
        for job in self.running_jobs:
            job.stop(blocking)

    def add_job(self, job: Job):
        job.set_config(self.config)
        job.set_web_app(self.web_app)
        self.queued_jobs.append(job)

job_manager = JobManager()