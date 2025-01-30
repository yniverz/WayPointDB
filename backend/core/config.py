import os
import uuid

class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or "test"  # For local dev
    DB_HOST = os.environ.get("POSTGRES_HOST")
    DB_NAME = os.environ.get("POSTGRES_DB")
    DB_USER = os.environ.get("POSTGRES_USER")
    DB_PASS = os.environ.get("POSTGRES_PASSWORD", "password")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/app/imports")
    BACKGROUND_MAX_THREADS = os.getenv("BACKGROUND_MAX_THREADS", 1)
    SQLALCHEMY_DATABASE_URI = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    PHOTON_SERVER_HOST = os.getenv("PHOTON_SERVER_HOST", "")
    PHOTON_SERVER_HTTPS = os.getenv("PHOTON_SERVER_HTTPS", True)
    PHOTON_SERVER_API_KEY = os.getenv("PHOTON_SERVER_API_KEY", "")
