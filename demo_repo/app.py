"""Notely — tiny notes app. Entry point and note routes."""
from flask import Flask, jsonify, request, session, redirect, url_for

from auth.routes import auth_bp
from auth.session import install_session_config

NOTES: dict[str, list[dict]] = {}  # user_email -> notes


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "dev-secret-change-me"
    install_session_config(app)
    app.register_blueprint(auth_bp)

    @app.get("/")
    def index():
        if "user" not in session:
            return redirect(url_for("auth.login"))
        return jsonify({"user": session["user"], "notes": NOTES.get(session["user"], [])})

    @app.post("/notes")
    def add_note():
        if "user" not in session:
            return jsonify({"error": "not logged in"}), 401
        body = request.get_json(silent=True) or {}
        note = {"id": len(NOTES.get(session["user"], [])) + 1, "text": body.get("text", "")}
        NOTES.setdefault(session["user"], []).append(note)
        return jsonify(note), 201

    @app.get("/admin")
    def admin():
        # Admin portal shares the same session cookie as the main app.
        if session.get("role") != "admin":
            return jsonify({"error": "forbidden"}), 403
        return jsonify({"users": list(NOTES.keys())})

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
