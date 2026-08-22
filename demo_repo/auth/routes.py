"""Auth routes: login, logout, signup, password reset, Google OAuth."""
from flask import Blueprint, jsonify, redirect, request, session, url_for

from auth.oauth import google_authorize_url, handle_google_callback
from auth.session import end_session, start_session
from auth.tokens import issue_reset_token, validate_reset_token
from forms.validators import validate_email, validate_password

auth_bp = Blueprint("auth", __name__)

USERS: dict[str, dict] = {"admin@notely.dev": {"password": "admin123", "role": "admin"}}


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return jsonify({"form": ["email", "password"], "oauth": url_for("auth.google_start")})
    data = request.get_json(silent=True) or request.form
    user = USERS.get(data.get("email", ""))
    if not user or user["password"] != data.get("password"):
        return jsonify({"error": "invalid credentials"}), 401
    start_session(session, data["email"], user["role"])
    return jsonify({"ok": True})


@auth_bp.post("/logout")
def logout():
    end_session(session)
    return jsonify({"ok": True})


@auth_bp.post("/signup")
def signup():
    data = request.get_json(silent=True) or request.form
    email, password = data.get("email", ""), data.get("password", "")
    err = validate_email(email) or validate_password(password)
    if err:
        return jsonify({"error": err}), 400
    if email in USERS:
        return jsonify({"error": "already registered"}), 409
    USERS[email] = {"password": password, "role": "user"}
    start_session(session, email)
    return jsonify({"ok": True}), 201


@auth_bp.post("/reset/request")
def reset_request():
    email = (request.get_json(silent=True) or request.form).get("email", "")
    if email not in USERS:
        return jsonify({"ok": True})  # don't leak account existence
    token = issue_reset_token(email)
    return jsonify({"ok": True, "token": token})  # emailed in prod; returned here for tests


@auth_bp.post("/reset/confirm")
def reset_confirm():
    data = request.get_json(silent=True) or request.form
    email = validate_reset_token(data.get("token", ""))
    if not email:
        return jsonify({"error": "token invalid or expired"}), 400
    err = validate_password(data.get("password", ""))
    if err:
        return jsonify({"error": err}), 400
    USERS[email]["password"] = data["password"]
    return jsonify({"ok": True})


@auth_bp.get("/oauth/google")
def google_start():
    return redirect(google_authorize_url(callback=url_for("auth.google_callback", _external=True)))


@auth_bp.get("/oauth/google/callback")
def google_callback():
    email = handle_google_callback(request.args)
    if not email:
        return jsonify({"error": "oauth failed"}), 401
    USERS.setdefault(email, {"password": None, "role": "user"})
    start_session(session, email)
    return redirect(url_for("index"))
