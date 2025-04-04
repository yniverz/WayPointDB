import os
from waitress import serve
from core import web_app, job_manager, Config
import signal
import requests

# cl = QueryPhotonJob([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
# job_manager.add_job(cl)


def handle_sigterm(*args):
    """
    Handle the SIGTERM signal (graceful shutdown).
    """
    print("Received SIGTERM, gracefully stopping tasks...")

    # Stop the background job manager
    job_manager.stop(blocking=True)

    # 1. Stop ongoing background jobs or threads safely.
    # 2. Close database connections if desired.
    # 3. Flush any pending logs or caches.
    # 4. Perform any other necessary cleanup steps.

    # After cleanup, we can exit with code 0
    os._exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)  # optional: handle ctrl+c in dev

    # send request to a start server. This is anonymous and only used to keep track of currently installed versions.
    version = Config.VERSION
    try:
        requests.get(f"http://95.179.227.112:9123/start/{version}")
    except requests.exceptions.RequestException as e:
        print(f"Error sending version to server: {e}")



    serve(web_app, host="0.0.0.0", port=8500)