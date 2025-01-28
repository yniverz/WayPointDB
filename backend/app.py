from waitress import serve
from core import create_web_app, create_job_app
import signal
import sys

app = create_web_app()
job_manager = create_job_app()

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
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)  # optional: handle ctrl+c in dev

    serve(app, host="0.0.0.0", port=8500)