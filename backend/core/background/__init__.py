import threading
import time
import traceback

from flask import Flask
from ..config import Config
from .jobs import Job




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
            ("QueryPhotonJob", False, 0.5),
            ("QueryPhotonJob", True, 1.0),
            ...
        ]
        """ 
        return [(job.__class__.__name__, job.done, job.progress) for job in self.running_jobs]

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

            except Exception:
                print(traceback.format_exc())

            time.sleep(0.1)


    def stop(self, blocking=False):
        self.queued_jobs.clear()
        for job in self.running_jobs:
            job.stop(blocking)

    def add_job(self, job: Job):
        job.set_config(self.config)
        self.queued_jobs.append(job)

job_manager = JobManager()