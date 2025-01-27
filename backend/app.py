import signal
import sys
import time
from flask import Flask
import psycopg2
import os

app = Flask(__name__)

# Database connection
DB_HOST = os.getenv("POSTGRES_HOST", "db")
DB_NAME = os.getenv("POSTGRES_DB", "mydatabase")
DB_USER = os.getenv("POSTGRES_USER", "user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "password")

def init_db():
    """Creates the GPS data table if it does not exist."""
    conn = psycopg2.connect(
        dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST
    )
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gps_data (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP NOT NULL,
            latitude DOUBLE PRECISION NOT NULL,
            longitude DOUBLE PRECISION NOT NULL,
            horizontal_accuracy DOUBLE PRECISION,
            altitude DOUBLE PRECISION,
            vertical_accuracy DOUBLE PRECISION,
            heading DOUBLE PRECISION,
            heading_accuracy DOUBLE PRECISION,
            speed DOUBLE PRECISION,
            speed_accuracy DOUBLE PRECISION
        );
    """)

    conn.commit()
    cur.close()
    conn.close()

# Ensure the database and table exist on startup
init_db()

@app.route("/")
def home():
    return "GPS Data Collection API is Running!"

@app.route("/db")
def test_db():
    """Check if the database is accessible."""
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST
        )
        cur = conn.cursor()
        cur.execute("SELECT 'Database connection successful' AS status;")
        result = cur.fetchone()
        return f"Database Status: {result[0]}"
    except Exception as e:
        return f"Error: {str(e)}"
    
@app.route("/table")
def test_table():
    """Check if the table is accessible. (including column names)"""
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST
        )
        cur = conn.cursor()
        cur.execute("SELECT * FROM gps_data LIMIT 1;")
        result = cur.fetchone()
        return f"Table Columns: {cur.description}"
    except Exception as e:
        return f"Error: {str(e)}"
    

# Graceful Shutdown Handler
def handle_shutdown(signum, frame):
    """Handles container stop signals and ensures data is processed before exiting."""
    print("\n[INFO] Received shutdown signal. Cleaning up...")
    
    # Simulate saving any in-memory data (modify if necessary)
    time.sleep(2)  # Simulate finalizing any ongoing processes

    print("[INFO] Cleanup complete. Shutting down.")
    sys.exit(0)  # Exit cleanly

# Capture SIGTERM (Docker stop) and SIGINT (Ctrl+C)
signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
    

if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=8500)