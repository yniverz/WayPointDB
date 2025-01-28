from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_restx import Api
from flask import Flask

db = SQLAlchemy()
migrate = Migrate()
api_v1 = Api(
    version="1.0",
    title="GPS Tracker API V1",
    description="API V1 documentation for GPS tracking",
    prefix="/api/v1",
    doc="/api/v1/docs"
)

def init_extensions(app: Flask):
    """Initialize all Flask extensions with the app."""
    db.init_app(app)
    migrate.init_app(app, db)
    api_v1.init_app(app)
