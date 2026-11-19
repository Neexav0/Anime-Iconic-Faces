"""Accounts, sessions, ownership and the daily-amount flow, through the real app."""
import itertools
import re

import pytest
from fastapi.testclient import TestClient

from app.auth import ALL_THROTTLES, hash_password, normalise_login, password_problem, verify_password
from app.main import app

_n = itertools.count(1)


@pytest.fixture(scope="module")
def server():
    with TestClient(app) as c:  # runs startup: create tables, seed catalogue
        yield c


@pytest.fixture(autouse=True)
def _fresh_throttle():
    for t in ALL_THROTTLES:
        t._fails.clear()


def client():
    return TestClient(app, follow_redirects=False)


def signup(c, name="Kamla Devi", password="neem-tree-42", **extra):
    login = f"user{next(_n)}@example.com"
    r = c.post("/signup", data={"name": name, "login": login, "password": password, "lang": "en", **extra})
    assert r.status_code == 200 and "Save your recovery code" in r.text, r.text
    c.last_code = re.search(r'id="code">([A-Z0-9-]+)<', r.text).group(1)
    return login


# ---------------------------------------------------------------- unit
def test_password_hash_roundtrip():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert verify_password("correct horse", h)
    assert not verify_password("wrong horse", h)
    assert not verify_password("anything", None)
    assert hash_password("same") != hash_password("same")  # random salt


@pytest.mark.parametrize("raw,norm", [
    ("Kamla@Example.COM ", "kamla@example.com"), ("+91 98765 43210", "9876543210"), ("09876543210", "9876543210"),
    ("919876543210", "9876543210"), ("12345", None), ("5876543210", None), ("not-an-email@", None), ("", None),
])
def test_normalise_login(raw, norm):
    assert normalise_login(raw) == norm


def test_password_rules():
    assert password_problem("short") == "pw_short"
    assert password_problem("password") == "pw_common"
    assert password_problem("9876543210", "9876543210") == "pw_same"
    assert password_problem("neem-tree-42") is None


# ---------------------------------------------------------------- web
def test_private_pages_need_sign_in(server):
    c = client()
    assert c.get("/").headers["location"] == "/welcome"
    assert c.get("/add").headers["location"] == "/login?next=/add"
    assert c.get("/passport.pdf").headers["location"].startswith("/login")
    # The old forged-cookie trick no longer works.
    c.cookies.set("pid", "1")
    assert c.get("/").headers["location"] == "/welcome"


def test_signup_login_logout(server):
    c = client()
    login = signup(c, name="Ravi Menon")
    assert "Ravi Menon" in c.get("/").text
    c.post("/logout")
    assert c.get("/").status_code == 303

    c2 = client()
    assert c2.post("/login", data={"login": login, "password": "wrong-password"}).status_code == 401
    r = c2.post("/login", data={"login": login.upper(), "password": "neem-tree-42", "next": "/health"})
    assert r.status_code == 303 and r.headers["location"] == "/health"
    assert c2.get("/health").status_code == 200


def test_signup_errors_keep_values(server):
    c = client()
    r = c.post("/signup", data={"name": "Asha", "login": "asha@example.com", "password": "short"})
    assert r.status_code == 400 and "at least 8 characters" in r.text and 'value="asha@example.com"' in r.text
    signup_login = signup(c, name="First")
    r = client().post("/signup", data={"name": "Again", "login": signup_login, "password": "another-pass-1"})
    assert r.status_code == 400 and "already uses" in r.text


def test_login_is_throttled(server):
    c = client()
    login = signup(c)
    c2 = client()
    for _ in range(5):
        assert c2.post("/login", data={"login": login, "password": "nope-nope-nope"}).status_code == 401
    r = c2.post("/login", data={"login": login, "password": "neem-tree-42"})
    assert r.status_code == 429  # even the right password waits out the lockout


def test_open_redirect_blocked(server):
    c = client()
    login = signup(c)
    c2 = client()
    r = c2.post("/login", data={"login": login, "password": "neem-tree-42", "next": "//evil.example"})
    assert r.headers["location"] == "/"


def test_cross_site_post_rejected(server):
    c = client()
    signup(c)
    r = c.post("/people", data={"name": "X"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def _add_dolo(c, per_day="3"):
    r = c.get("/manual?q=Dolo 650")
    draft = int(r.headers["location"].rsplit("/", 1)[1])
    r = c.post("/entries", data={"draft_id": draft, "name": "Dolo 650", "confirmed": "on", "per_day": per_day,
                                 "ing_raw": ["Paracetamol"], "ing_strength": ["650 mg"]})
    return int(re.search(r"added=(\d+)", r.headers["location"]).group(1)), draft


def test_people_cannot_touch_each_others_data(server):
    a, b = client(), client()
    signup(a, name="Owner")
    signup(b, name="Stranger")
    entry_id, _ = _add_dolo(a)
    draft = int(a.get("/manual?q=Crocin").headers["location"].rsplit("/", 1)[1])

    b.post(f"/entries/{entry_id}/delete")
    b.post(f"/entries/{entry_id}/amount", data={"per_day": "50"})
    assert b.get(f"/confirm/{draft}").headers["location"] == "/add"
    assert b.get(f"/draft-image/{draft}").status_code == 404

    home = a.get("/").text
    assert "Dolo 650" in home and 'value="3"' in home  # untouched


def test_adult_daily_maximum(server):
    c = client()
    signup(c, age="70")
    _add_dolo(c, per_day="7")  # 7 x 650 = 4550 mg
    assert "4550 mg of Paracetamol a day" in c.get("/").text


def test_child_needs_weight_then_gets_checked(server):
    c = client()
    signup(c, name="Parent")
    c.post("/people", data={"name": "Aarav", "age": "5"})
    r = c.get("/manual?q=Calpol 250 Suspension")
    draft = int(r.headers["location"].rsplit("/", 1)[1])
    c.post("/entries", data={"draft_id": draft, "name": "Calpol 250 Suspension", "confirmed": "on", "per_day": "30",
                             "ing_raw": ["Paracetamol"], "ing_strength": ["250 mg/5 ml"]})
    home = c.get("/").text
    assert "Aarav" in home and "Weight needed" in home and "Above the daily maximum" not in home

    pid = int(re.search(r'/people/(\d+)/update"[^>]*>\s*<label class="field"><span>Name</span><input[^>]*value="Aarav"',
                        c.get("/people").text).group(1))
    c.post(f"/people/{pid}/update", data={"name": "Aarav", "age": "5", "weight": "18"})
    home = c.get("/").text
    assert "1500 mg of Paracetamol a day" in home and "a child weighing 18 kg" in home


def test_sample_patient_is_one_click(server):
    c = client()
    r = c.post("/try/child")
    assert r.status_code == 303 and "sid" in r.cookies
    home = c.get("/").text
    assert "Aarav Sharma" in home and "sample patient" in home
    assert "no dose for children under 6" in home


# ---------------------------------------------------------------- blocking-bug fixes
def test_photo_is_read_off_the_event_loop_and_upload_is_capped(server, monkeypatch):
    import threading
    from app import main, vision
    seen = {}

    def fake_transcribe(data, mime):
        seen["thread"] = threading.current_thread() is threading.main_thread()
        raise vision.VisionError("not configured")
    monkeypatch.setattr(vision, "transcribe", fake_transcribe)
    c = client()
    signup(c)
    r = c.post("/extract", files={"photo": ("strip.jpg", b"JPEG-test-bytes", "image/jpeg")})
    draft = int(r.headers["location"].rsplit("/", 1)[1])
    assert seen["thread"] is False  # ran on a worker thread, not the event loop
    assert c.get(f"/draft-image/{draft}").content == b"JPEG-test-bytes"
    big = b"x" * (main.MAX_UPLOAD + 10)
    assert c.post("/extract", files={"photo": ("big.jpg", big, "image/jpeg")}).headers["location"] == "/add?error=too_large"


def test_photo_store_expires_and_caps():
    from app.photos import PhotoStore
    now = [0.0]
    store = PhotoStore(ttl=60, max_bytes=10, clock=lambda: now[0])
    store.put(1, b"aaaa", "image/jpeg")
    store.put(2, b"bbbb", "image/jpeg")
    store.put(3, b"cccc", "image/jpeg")  # over 10 bytes: oldest (1) is dropped
    assert 1 not in store and 2 in store and store.size == (2, 8)
    now[0] = 61
    assert 2 not in store and 3 not in store and store.size == (0, 0)


def _share_token(c):
    return re.search(r"/p/([A-Za-z0-9_-]+)", c.get("/share").text).group(1)


def test_pharmacist_link_expires_and_can_be_revoked(server):
    from datetime import datetime, timedelta
    from app.db import SessionLocal
    from app.models import Review
    from sqlalchemy import select
    c = client()
    signup(c)
    _add_dolo(c)
    token = _share_token(c)
    pharmacist = client()
    assert pharmacist.get(f"/p/{token}").status_code == 200
    assert _share_token(c) == token  # same link while it is active

    c.post("/share/revoke")
    assert pharmacist.get(f"/p/{token}").status_code == 410
    with SessionLocal() as s:  # backdated, so MySQL's whole-second rounding cannot leave it alive
        assert s.scalar(select(Review).where(Review.token == token)).expires_at <= datetime.now() - timedelta(seconds=1)
    assert pharmacist.post(f"/p/{token}", data={"reviewer": "X"}).status_code == 410
    fresh = _share_token(c)
    assert fresh != token and pharmacist.get(f"/p/{fresh}").status_code == 200

    with SessionLocal() as s:
        s.scalar(select(Review).where(Review.token == fresh)).expires_at = datetime.now() - timedelta(minutes=1)
        s.commit()
    assert pharmacist.get(f"/p/{fresh}").status_code == 410


def test_reviewed_notes_survive_a_new_link(server):
    c = client()
    signup(c)
    _add_dolo(c)
    _add_dolo(c)
    token = _share_token(c)
    page = client().get(f"/p/{token}").text
    key = re.search(r'name="note::([^"]+)"', page).group(1)
    client().post(f"/p/{token}", data={"reviewer": "A. Sharma", f"done::{key}": "on", f"note::{key}": "Take only one."})
    c.post("/share/revoke")
    assert "Take only one." in c.get("/").text


def test_password_reset_with_recovery_code(server):
    c = client()
    login = signup(c)
    code = c.last_code
    other_device = client()
    other_device.post("/login", data={"login": login, "password": "neem-tree-42"})
    assert other_device.get("/health").status_code == 200

    stranger = client()
    assert stranger.post("/forgot", data={"login": login, "code": "AAAA-BBBB-CCCC-DDDD", "password": "new-pass-123"}).status_code == 401
    r = stranger.post("/forgot", data={"login": login, "code": code.lower().replace("-", " "), "password": "new-pass-123"})
    assert r.status_code == 200 and "Your old recovery code no longer works" in r.text
    assert other_device.get("/health").status_code == 303  # every other device signed out
    assert client().post("/login", data={"login": login, "password": "neem-tree-42"}).status_code == 401
    assert client().post("/login", data={"login": login, "password": "new-pass-123"}).status_code == 303
    # A recovery code works once.
    assert client().post("/forgot", data={"login": login, "code": code, "password": "third-pass-123"}).status_code == 401


def test_sign_ups_and_samples_are_rate_limited(server):
    c = client()
    for _ in range(20):
        assert c.post("/try/paracetamol").status_code == 303
    assert c.post("/try/paracetamol").status_code == 429
    for _ in range(10):
        signup(client())
    r = client().post("/signup", data={"name": "Eleven", "login": "eleven@example.com", "password": "neem-tree-42"})
    assert r.status_code == 429
