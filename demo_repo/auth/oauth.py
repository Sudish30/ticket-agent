"""'Continue with Google' — stubbed. The real flow redirects to accounts.google.com and back.

Note: the return from Google is a top-level cross-site navigation back to our callback URL.
"""
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"


def google_authorize_url(callback: str) -> str:
    return f"{GOOGLE_AUTH}?client_id=demo&redirect_uri={callback}&response_type=code&scope=email"


def handle_google_callback(args) -> str | None:
    return args.get("email") or None  # stub: accept ?email=... as if Google returned it
