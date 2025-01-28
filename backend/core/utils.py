import uuid
import flask
from flask import session, redirect, url_for, g
from functools import wraps
from .models import User, db

def login_required(f):
    """Decorator to ensure the user is logged in (session-based) for HTML routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        g.current_user = get_current_user()
        return f(*args, **kwargs)
    return decorated_function

def api_key_required(f):
    """Decorator for REST endpoints that requires an API key."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = flask.request.args.get("api_key")  # or from headers
        user = User.query.filter_by(api_key=api_key).first()
        if not user:
            return {"error": "Invalid or missing API key"}, 401
        g.current_user = user
        return f(*args, **kwargs)
    return decorated_function

def get_current_user():
    """Return the currently logged-in user object (or None) using the session."""
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None

def create_default_user():
    """Create a default admin user if none exists."""
    from .models import User
    db.create_all()

    # if no user with is_admin=True exists, create one
    existing_admin = User.query.filter_by(is_admin=True).first()
    if not existing_admin:
        admin_email = "admin@example.com"
        admin_pass = "password"

        user = User(
            email=admin_email,
            is_admin=True
        )
        user.set_password(admin_pass)
        db.session.add(user)
        db.session.commit()

    # existing = User.query.filter_by(email=admin_email).first()
    # if not existing:
    #     admin_email = "admin@example.com"
    #     admin_pass = "password"
    #     user = User(
    #         email=admin_email,
    #         is_admin=True
    #     )
    #     user.set_password(admin_pass)
    #     db.session.add(user)
    #     db.session.commit()

