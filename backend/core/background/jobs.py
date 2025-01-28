from . import Config
import time


class Job:
    def __init__(self):
        self.config = Config
        self.running = False
        self.done = False
        self.stop_requested = False

    def set_config(self, config: Config):
        self.config = config

    def run(self):
        """
        This method should be overridden by the subclass.
        Run the main job loop here and return when the job is done.
        """
        pass

    def stop(self, blocking=False):
        self.stop_requested = True

        if blocking:
            while not self.done:
                time.sleep(0.1)
                pass




class GenerateStatisticsJob(Job):
    def run(self):
        # This is a dummy example job that generates statistics.
        # In a real application, this method would do something useful.
        print("Generating statistics...")
        time.sleep(5)
        print("Statistics generated.")
        self.done = True