import os
import threading
from flask import Flask, g
from jinja2 import ChoiceLoader, FileSystemLoader

from .config import Config
from .extensions import init_extensions, api_v1
from .routes.web import web_bp
from .routes.api import api_ns
from .utils import check_db, create_default_user
from .background import job_manager


def inject_user():
    """
    This context processor runs before every template render.
    It returns a dict of variables to add to the Jinja context.
    """
    return {
        "current_user": getattr(g, "current_user", None)
    }

def month_name(value):
    months = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    # The function expects month values 1..12
    try:
        return months[value - 1]
    except (IndexError, TypeError):
        # Fallback if value is out of range or invalid
        return "Unknown Month"

def create_web_app(config_class = Config):
    app = Flask(__name__, template_folder="templates")
    app.config.from_object(config_class)
    app.context_processor(inject_user)
    app.jinja_env.filters["month_name"] = month_name

    # Optionally set up a custom Jinja loader:
    app.jinja_loader = ChoiceLoader([FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates"))])

    # Initialize Extensions (DB, Migrate, RESTx, etc.)
    init_extensions(app)

    # Register Blueprints
    app.register_blueprint(web_bp, url_prefix="")
    api_v1.add_namespace(api_ns)


    # Create DB tables and default user if needed
    with app.app_context():
        create_default_user()
        check_db()

    return app

def create_job_app(config_class = Config, app=None):
    job_manager.set_config(config_class)
    job_manager.set_web_app(app=app)
    thread = threading.Thread(target=job_manager.run)
    thread.start()
    return job_manager


web_app = create_web_app()
job_manager = create_job_app(app=web_app)