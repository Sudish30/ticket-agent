"""Input validation for signup and reset forms."""
import re

EMAIL_RE = re.compile(r"^[A-Za-z0-9._]+@[A-Za-z0-9.]+\.[A-Za-z]{2,}$")
MIN_PASSWORD = 8


def validate_email(email: str) -> str | None:
    if not email:
        return "email is required"
    if not EMAIL_RE.match(email):
        return "email looks invalid"
    return None


def validate_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if password.isdigit() or password.isalpha():
        return "password must mix letters and numbers"
    return None
