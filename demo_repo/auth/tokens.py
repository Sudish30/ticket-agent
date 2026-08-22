"""Password-reset tokens: signed, single-use, time-limited."""
import hashlib, hmac, time

SECRET = b"reset-secret"
TOKEN_TTL_MINUTES = 30
_used: set[str] = set()


def _sign(payload: str) -> str:
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]


def issue_reset_token(email: str) -> str:
    payload = f"{email}:{int(time.time())}"
    return f"{payload}:{_sign(payload)}"


def validate_reset_token(token: str) -> str | None:
    try:
        email, issued, sig = token.rsplit(":", 2)
    except ValueError:
        return None
    if _sign(f"{email}:{issued}") != sig or token in _used:
        return None
    age = time.time() - int(issued)
    if age > TOKEN_TTL_MINUTES:
        return None
    _used.add(token)
    return email
