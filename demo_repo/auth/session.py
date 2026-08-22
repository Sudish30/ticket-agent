"""Session cookie configuration.

PROJ-130 (last sprint) hardened the session cookie: HttpOnly + SameSite=Strict.
"""
from datetime import timedelta

SESSION_COOKIE_NAME = "notely_session"
SESSION_LIFETIME = timedelta(days=7)


def install_session_config(app) -> None:
    app.config.update(
        SESSION_COOKIE_NAME=SESSION_COOKIE_NAME,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=False,  # dev only
        SESSION_COOKIE_SAMESITE="Strict",  # hardened in PROJ-130
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    )


def start_session(session, email: str, role: str = "user") -> None:
    session.permanent = True
    session["user"] = email
    session["role"] = role


def end_session(session) -> None:
    session.clear()
