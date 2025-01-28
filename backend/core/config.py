import os
import uuid

class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or "test"  # For local dev
    DB_HOST = os.getenv("POSTGRES_HOST", "db")
    DB_NAME = os.getenv("POSTGRES_DB", "mydatabase")
    DB_USER = os.getenv("POSTGRES_USER", "user")
    DB_PASS = os.getenv("POSTGRES_PASSWORD", "password")
    BACKGROUND_MAX_THREADS = os.getenv("BACKGROUND_MAX_THREADS", 5)
    BACKGROUND_MAX_THREADS_PER_JOB = os.getenv("BACKGROUND_MAX_THREADS_PER_JOB", 5)
    SQLALCHEMY_DATABASE_URI = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PHOTON_SERVER_HOST = os.getenv("PHOTON_SERVER_HOST", "")
    PHOTON_SERVER_HTTPS = os.getenv("PHOTON_SERVER_HTTPS", True)
    PHOTON_SERVER_API_KEY = os.getenv("PHOTON_SERVER_API_KEY", "")
