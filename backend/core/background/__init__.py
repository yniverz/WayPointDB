import threading
import time
import traceback
from ..config import Config
from .jobs import Job





class JobManager:
    def __init__(self):
        self.config = Config
        self.queued_jobs: list[Job] = []
        self.running_jobs: list[Job] = []
        self.threads = []

    def set_config(self, config: Config):
        self.config = config

    def run(self):
        while True:
            try:
                if len(self.threads) < self.config.BACKGROUND_MAX_THREADS:
                    if self.queued_jobs:
                        job = self.queued_jobs.pop(0)
                        job.running = True
                        thread = threading.Thread(target=job.run)
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