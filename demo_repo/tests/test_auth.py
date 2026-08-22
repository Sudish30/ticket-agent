import time


def test_login_sets_session(client):
    r = client.post("/login", json={"email": "admin@notely.dev", "password": "admin123"})
    assert r.status_code == 200
    assert client.get("/").status_code == 200


def test_signup_accepts_plus_addressing(client):
    r = client.post("/signup", json={"email": "sudish+test@gmail.com", "password": "abc12345"})
    assert r.status_code == 201, r.get_json()


def test_reset_token_valid_for_minutes(client, monkeypatch):
    tok = client.post("/reset/request", json={"email": "admin@notely.dev"}).get_json()["token"]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 120)  # two minutes later
    r = client.post("/reset/confirm", json={"token": tok, "password": "newpass99"})
    assert r.status_code == 200, r.get_json()

