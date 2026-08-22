# Notely — demo app for the Quorum hackathon

A deliberately small Flask app (notes + accounts) used as the target codebase for the
ticket-resolution agents. It runs, has tests, and contains a few planted bugs on the
`buggy` branch that the mock tickets in `../mock_tickets/` describe.

    pip install flask
    python app.py            # http://localhost:5000
    python -m pytest tests   # 2 tests fail on the buggy branch — that's the point

Structure
- app.py                 Flask app factory, routes for notes
- auth/session.py        session cookie issuance (name, SameSite, expiry)
- auth/routes.py         /login /logout /signup /reset
- auth/oauth.py          "Continue with Google" redirect flow (stubbed)
- auth/tokens.py         password-reset token generation and validation
- forms/validators.py    email / password validation used by signup
- templates/             minimal HTML
- tests/                 pytest suite
