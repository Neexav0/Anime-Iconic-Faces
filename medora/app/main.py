from __future__ import annotations

import contextvars
import io
import zlib
import os
import re
import secrets
import socket
import threading
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
from pathlib import Path

import anyio
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select

from . import vision
from .auth import (LOGIN_THROTTLE, RESET_THROTTLE, SAMPLE_THROTTLE, SESSION_COOKIE, SESSION_DAYS, SIGNUP_THROTTLE,
                   hash_password, new_recovery_code, new_token, normalise_login, normalise_recovery, password_problem,
                   token_hash, verify_password)
from .db import DATABASE_URL, SessionLocal
from .evaluation import run_all
from .i18n import translator
from .matching import clean, parse_expiry
from .models import Account, AuthSession, Draft, Entry, Patient, Review
from .pdf import build_pdf
from .photos import PhotoStore
from .rules_engine import evaluate
from .seed import init_db, load_kb
from .strip_qr import parse_strip_qr

APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Medora")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

KB = None
# Photos are held in memory only until the user confirms or discards the draft.
# Photos of unconfirmed drafts: memory only, expire after 30 minutes, 200 MB cap (see photos.py).
DRAFT_IMAGES = PhotoStore()
REVIEW_DAYS = 14  # a pharmacist link stops working after this many days
MAX_UPLOAD = 10 * 1024 * 1024

# entries: (brand, note, printed expiry, amount per day: tablets/capsules, or ml for a syrup)
DEMOS = {
    "paracetamol": {"title": "Hidden paracetamol: 4.6 g a day from a fever tablet and a cold tablet", "age": 72, "name": "Kamla Devi",
                    "entries": [("Dolo 650", "Dr. Mehta (fever)", None, 4), ("Sinarest", "Bought at chemist for cold", None, 4)]},
    "bp": {"title": "Two doctors, same BP ingredient", "age": 68, "name": "Ramesh Kumar",
           "entries": [("Telma-H", "Cardiologist", None, 1), ("Telma-AM", "Family physician", None, 1),
                       ("Glycomet 500", "Diabetologist", None, 2), ("Janumet 50/500", "Diabetologist (new)", "EXP 08/2025", 2)]},
    "bleed": {"title": "Blood thinner + painkiller", "age": 74, "name": "Suresh Iyer",
              "entries": [("Warf 5", "Cardiologist", None, 1), ("Combiflam", "Bought for knee pain", None, 2), ("Pan 40", "", None, 1)]},
    "allergy": {"title": "Allergy + side-effect check", "age": 70, "name": "Lakshmi Rao",
                "allergies": ["sulfa", "penicillin"], "symptoms": ["ankle_swelling", "drowsiness"],
                "entries": [("Telma-H", "Cardiologist", None, 1), ("Amlokind 5", "Cardiologist", None, 1),
                            ("Augmentin 625 Duo", "Dentist", None, 2), ("Cetzine", "Chemist, for cold", None, 1)]},
    "child": {"title": "Child, 18 kg: fever syrup plus an adult cold tablet", "age": 5, "weight_kg": 18, "name": "Aarav Sharma",
              "entries": [("Calpol 250 Suspension", "Paediatrician, for fever", None, 30),
                          ("Sinarest", "From the family's cold tablets", None, 1)]},
}


_READY = threading.Lock()


def ensure_ready() -> None:
    """Create tables, seed the catalogue and load the KB once per process.
    Called at startup and, for serverless hosts that skip lifespan events, on the first request."""
    global KB
    if KB is not None:
        return
    with _READY:
        if KB is None:
            init_db()
            purge_old_demos()
            KB = load_kb()


@app.on_event("startup")
def startup():
    ensure_ready()


# Behind a hosting proxy (Vercel sets VERCEL=1) the socket peer is the proxy, not the visitor.
TRUST_PROXY = bool(os.getenv("VERCEL") or os.getenv("TRUST_PROXY"))


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
        if fwd.strip():
            return fwd.strip()
    return request.client.host if request.client else ""


def is_https(request: Request) -> bool:
    if TRUST_PROXY and request.headers.get("x-forwarded-proto"):
        return request.headers["x-forwarded-proto"].split(",")[0].strip() == "https"
    return request.url.scheme == "https"


def ctx(request: Request, **kw):
    lang = request.cookies.get("lang", "en")
    return {"request": request, "t": translator(lang), "lang": lang, "demos": DEMOS, "me": CURRENT_USER.get(),
            "large": request.cookies.get("large") == "1", "db_kind": DATABASE_URL.split(":")[0].split("+")[0], **kw}


CURRENT_USER: contextvars.ContextVar[dict | None] = contextvars.ContextVar("current_user", default=None)
# Pages and actions that need a signed-in account (prefix match).
PROTECTED = ("/add", "/manual", "/confirm", "/extract", "/strip-qr", "/entries", "/drafts", "/draft-image", "/health",
             "/share", "/pharmacist", "/passport.pdf", "/patient", "/people", "/demo")


def load_user(token: str | None) -> dict | None:
    """Session cookie -> the signed-in account and the person being viewed (None if not signed in)."""
    if not token or len(token) > 100:
        return None
    with SessionLocal() as s:
        sess = s.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
        if not sess or sess.expires_at < datetime.now():
            return None
        acc = s.get(Account, sess.account_id)
        if not acc:
            return None
        p = s.get(Patient, sess.patient_id) if sess.patient_id else None
        if not p or p.account_id != acc.id:
            p = s.scalar(select(Patient).where(Patient.account_id == acc.id).order_by(Patient.id).limit(1))
            sess.patient_id = p.id if p else None
            s.commit()
        return {"account_id": acc.id, "name": acc.name, "login": acc.login, "is_demo": acc.is_demo,
                "session_id": sess.id, "pid": p.id if p else None, "patient_name": p.name if p else "",
                "patient_age": p.age if p else None}


class AuthMiddleware:
    """Pure ASGI: resolves the session cookie, guards private pages and rejects cross-site form posts."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/static/"):
            return await self.app(scope, receive, send)
        if KB is None:
            await anyio.to_thread.run_sync(ensure_ready)
        req = Request(scope)
        path, method = scope["path"], scope["method"]
        if method == "POST":
            origin = req.headers.get("origin")
            if origin and urlparse(origin).netloc != req.headers.get("host"):
                return await Response("Cross-site form posts are not allowed.", status_code=403)(scope, receive, send)
        user = await anyio.to_thread.run_sync(load_user, req.cookies.get(SESSION_COOKIE))
        needs = path == "/" or any(path == x or path.startswith(x + "/") for x in PROTECTED)
        if needs and user is None:
            nxt = f"?next={quote(path)}" if method == "GET" and path != "/" else ""
            return await RedirectResponse("/welcome" if path == "/" else f"/login{nxt}", status_code=303)(scope, receive, send)
        if needs and user["pid"] is None and not path.startswith("/people"):
            return await RedirectResponse("/people", status_code=303)(scope, receive, send)
        token = CURRENT_USER.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            CURRENT_USER.reset(token)


app.add_middleware(AuthMiddleware)


def get_patient(s, user: dict | None = None) -> Patient:
    """The person the signed-in account is viewing. Never falls back to someone else's record."""
    u = user or CURRENT_USER.get()
    p = s.get(Patient, u["pid"]) if u and u.get("pid") else None
    if not p or p.account_id != u["account_id"]:
        raise HTTPException(status_code=401)
    return p


def owned_draft(s, draft_id: int) -> Draft | None:
    d = s.get(Draft, draft_id)
    u = CURRENT_USER.get()
    return d if d and u and d.patient_id == u["pid"] else None


def owned_entry(s, entry_id: int) -> Entry | None:
    e = s.get(Entry, entry_id)
    u = CURRENT_USER.get()
    return e if e and u and e.patient_id == u["pid"] else None


def parse_amount(raw: str) -> float | None:
    """'2' / '1.5' / '½' -> per-day amount; blank or out of range -> None."""
    raw = (raw or "").strip().replace(",", ".").replace("½", ".5")
    try:
        v = float(raw)
    except ValueError:
        return None
    return round(v, 2) if 0 < v <= 500 else None


def parse_weight(raw: str) -> float | None:
    try:
        v = float((raw or "").strip().replace(",", "."))
    except ValueError:
        return None
    return round(v, 1) if 1 <= v <= 300 else None


def parse_age(raw: str) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() and 0 <= int(raw) < 125 else None


def current_state(s, lang: str, patient: Patient | None = None):
    patient = patient or get_patient(s)
    entries = list(s.scalars(select(Entry).where(Entry.patient_id == patient.id).order_by(Entry.id)))
    result = evaluate([e.as_engine_dict() for e in entries], patient.as_engine_dict(), KB, lang=lang)
    return patient, entries, result


def uses_view(entries, lang: str) -> dict:
    """entry id -> [(ingredient display, 'commonly used for' text or None)]."""
    out = {}
    for e in entries:
        rows, seen = [], set()
        for ing in e.ingredients or []:
            rec = KB.normalizer.recognise(ing["raw"])
            if rec.canonical in seen:
                continue
            seen.add(rec.canonical)
            rows.append((KB.display(rec.canonical) if rec.canonical else ing["raw"], KB.uses(rec.canonical, lang),
                         KB.label_source(rec.canonical)["url"] if rec.canonical else None))
        out[e.id] = rows
    return out


def latest_review(s, patient_id: int) -> Review | None:
    """The most recent review a pharmacist has actually saved (its notes show on the patient's home)."""
    return s.scalar(select(Review).where(Review.patient_id == patient_id, Review.reviewed_at.is_not(None))
                    .order_by(Review.reviewed_at.desc()).limit(1))


def review_expiry(r: Review) -> datetime:
    return r.expires_at or (r.created_at + timedelta(days=REVIEW_DAYS))


def review_active(r: Review | None) -> bool:
    return bool(r) and review_expiry(r) > datetime.now()


def active_share(s, patient_id: int) -> Review | None:
    r = s.scalar(select(Review).where(Review.patient_id == patient_id).order_by(Review.id.desc()).limit(1))
    return r if review_active(r) else None


def build_view(entries, result):
    """Per-entry ingredient chips + the data for the ingredient map."""
    dup_keys = {a.get("ingredient_key") for a in result["alerts"] if a["rule_id"] == "R-DUP-001"}
    owners: dict[str, list[str]] = {}
    resolved = {}
    for e in entries:
        resolved[e.id] = []
        for ing in e.ingredients or []:
            rec = KB.normalizer.recognise(ing["raw"])
            key = rec.canonical or "?" + clean(ing["raw"])
            label = KB.display(rec.canonical) if rec.canonical else ing["raw"]
            resolved[e.id].append((key, label, ing.get("strength") or "", bool(rec.canonical)))
            owners.setdefault(key, []).append(e.name)
    tags, links, ings = {}, [], {}
    for e in entries:
        tags[e.id] = []
        for key, label, strength, known in resolved[e.id]:
            others = [n for n in owners[key] if n != e.name]
            tags[e.id].append({"label": label, "strength": strength, "kind": "dup" if key in dup_keys else ("" if known else "unk"),
                               "also": others, "hue": zlib.crc32(key.encode()) % 6})
            links.append([e.id, key])
            ings[key] = {"key": key, "label": label, "shared": key in dup_keys}
    today = datetime.now()
    expiry = {}
    for e in entries:
        exp = parse_expiry(e.expiry)
        if exp:
            expiry[e.id] = {"label": f"{exp[1]:02d}/{exp[0]}", "expired": (exp[0], exp[1]) < (today.year, today.month)}
    graph = {"meds": [{"id": e.id, "name": e.name} for e in entries], "ings": list(ings.values()), "links": links}
    return tags, graph, expiry


def lan_base_url(request: Request) -> str:
    """Base URL a phone on the same Wi-Fi can open (QR codes must not point at 'localhost')."""
    if os.getenv("PUBLIC_BASE_URL"):
        return os.getenv("PUBLIC_BASE_URL").rstrip("/")
    host = request.url.hostname or "localhost"
    port = request.url.port
    if host in ("localhost", "127.0.0.1", "::1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.connect(("10.255.255.255", 1))  # no packet is sent; picks the outbound interface
                host = sk.getsockname()[0]
        except OSError:
            pass
    scheme = "https" if is_https(request) else request.url.scheme
    return f"{scheme}://{host}" + (f":{port}" if port and not TRUST_PROXY else "")


def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return re.sub(r"<\?xml[^>]*\?>", "", svg).replace("<svg ", '<svg role="img" aria-label="QR code" ', 1)


# ---------------------------------------------------------------- welcome / sign-in
HERO_PAIRS = [("Dolo 650", "Sinarest"), ("Telma-H", "Telma-AM"), ("Brufen 400", "Combiflam")]
_EVAL_CACHE: dict = {}


def hero_pairs() -> list[dict]:
    """Real catalogue compositions for the landing-page strip comparison."""
    out = []
    for a, b in HERO_PAIRS:
        pair = [KB.brand_matcher.exact(a), KB.brand_matcher.exact(b)]
        if not all(pair):
            continue
        shared = {i["canonical"] for i in pair[0]["ingredients"]} & {i["canonical"] for i in pair[1]["ingredients"]}
        out.append({
            "shared": [KB.display(c) for c in shared],
            "brands": [{"name": x["name"], "maker": x["manufacturer"], "form": x["form"],
                        "ings": [{"name": KB.display(i["canonical"]), "strength": i["strength"],
                                  "shared": i["canonical"] in shared} for i in x["ingredients"]]} for x in pair],
        })
    return out


def eval_summary():
    if "ev" not in _EVAL_CACHE:
        _EVAL_CACHE["ev"] = run_all(KB)
    return _EVAL_CACHE["ev"]


@app.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    current = CURRENT_USER.get()
    return templates.TemplateResponse(request, "welcome.html", ctx(
        request, current=current, pairs=hero_pairs(), ev=eval_summary(), kb=KB))


def safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/\\") else "/"


def start_session(resp: Response, request: Request, account_id: int, patient_id: int | None) -> None:
    """New random token on every sign-in (no session fixation); only its hash is stored."""
    old = request.cookies.get(SESSION_COOKIE)
    token, th = new_token()
    with SessionLocal() as s:
        if old:
            stale = s.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(old)))
            if stale:
                s.delete(stale)
        s.add(AuthSession(token_hash=th, account_id=account_id, patient_id=patient_id,
                          expires_at=datetime.now() + timedelta(days=SESSION_DAYS)))
        s.commit()
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=is_https(request), path="/")


AUTH_ERRORS = {
    "name": ("Type your name.", "अपना नाम लिखें।"),
    "login": ("Enter an email address or a 10-digit Indian mobile number.", "ईमेल या 10 अंकों का भारतीय मोबाइल नंबर लिखें।"),
    "taken": ("An account already uses this email or mobile number. Log in instead.", "इस ईमेल या मोबाइल नंबर से पहले से खाता है। लॉग इन करें।"),
    "pw_short": ("Use at least 8 characters for your password.", "पासवर्ड में कम से कम 8 अक्षर रखें।"),
    "pw_long": ("Use a password shorter than 200 characters.", "पासवर्ड 200 अक्षरों से छोटा रखें।"),
    "pw_same": ("Choose a password that is different from your email or mobile number.", "ऐसा पासवर्ड चुनें जो आपके ईमेल या मोबाइल नंबर से अलग हो।"),
    "pw_common": ("That password is too easy to guess. Choose another.", "यह पासवर्ड आसानी से अंदाज़ा लगाया जा सकता है। कोई और चुनें।"),
    "bad": ("That email, mobile number or password is not right. Check them and try again.", "ईमेल, मोबाइल नंबर या पासवर्ड सही नहीं है। जाँचकर फिर कोशिश करें।"),
    "locked": ("Too many attempts. Wait 15 minutes, then try again.", "बहुत ज़्यादा कोशिशें हुईं। 15 मिनट रुककर फिर कोशिश करें।"),
    "bad_code": ("That email, mobile number or recovery code is not right. Check them and try again.",
                 "ईमेल, मोबाइल नंबर या रिकवरी कोड सही नहीं है। जाँचकर फिर कोशिश करें।"),
}


def auth_error(code: str | None, lang: str) -> str | None:
    if not code or code not in AUTH_ERRORS:
        return None
    en, hi = AUTH_ERRORS[code]
    return hi if lang == "hi" else en


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    if CURRENT_USER.get():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "signup.html", ctx(request, form={}, error=None))


@app.post("/signup")
def signup(request: Request, name: str = Form(""), age: str = Form(""), login: str = Form(""), password: str = Form(""),
           lang: str = Form("en")):
    ui_lang = lang if lang in ("en", "hi") else request.cookies.get("lang", "en")
    ip = client_ip(request)
    if SIGNUP_THROTTLE.blocked(f"ip:{ip}"):
        return too_many(request)
    name = " ".join(name.split())[:120]
    norm = normalise_login(login)
    err = "name" if not name else ("login" if not norm else password_problem(password, norm))
    if not err:
        with SessionLocal() as s:
            if s.scalar(select(Account).where(Account.login == norm)):
                err = "taken"
            else:
                code = new_recovery_code()
                acc = Account(login=norm, name=name, password_hash=hash_password(password),
                              recovery_hash=hash_password(normalise_recovery(code)))
                s.add(acc)
                s.flush()
                p = Patient(account_id=acc.id, name=name, age=parse_age(age))
                s.add(p)
                s.commit()
                aid, pid = acc.id, p.id
    if err:
        return templates.TemplateResponse(request, "signup.html", ctx(
            request, form={"name": name, "age": age, "login": login}, error=auth_error(err, ui_lang)), status_code=400)
    SIGNUP_THROTTLE.hit(f"ip:{ip}")
    resp = templates.TemplateResponse(request, "recovery.html", ctx(request, code=code, reason="signup"))
    start_session(resp, request, aid, pid)
    resp.set_cookie("lang", ui_lang, max_age=31536000, samesite="lax")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def too_many(request: Request) -> HTMLResponse:
    hi = request.cookies.get("lang") == "hi"
    return templates.TemplateResponse(request, "message.html", ctx(
        request, title="बहुत ज़्यादा कोशिशें" if hi else "Too many attempts from this network",
        body="एक घंटे बाद फिर कोशिश करें।" if hi else "Wait an hour, then try again."), status_code=429)


@app.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return templates.TemplateResponse(request, "forgot.html", ctx(request, login_value="", error=None))


@app.post("/forgot")
def forgot(request: Request, login: str = Form(""), code: str = Form(""), password: str = Form("")):
    """Reset a password with the recovery code. Signs out every other device and issues a new code."""
    lang = request.cookies.get("lang", "en")
    norm = normalise_login(login) or (login or "").strip().lower()[:160]
    ip = client_ip(request)
    keys = (f"login:{norm}", f"ip:{ip}")
    fail = lambda err, status: templates.TemplateResponse(
        request, "forgot.html", ctx(request, login_value=login, error=auth_error(err, lang)), status_code=status)
    if RESET_THROTTLE.blocked(*keys):
        return fail("locked", 429)
    with SessionLocal() as s:
        acc = s.scalar(select(Account).where(Account.login == norm, Account.is_demo.is_(False)))
        if not verify_password(normalise_recovery(code), acc.recovery_hash if acc else None):
            RESET_THROTTLE.fail(*keys)
            return fail("bad_code", 401)
        if err := password_problem(password, norm):
            return fail(err, 400)
        new_code = new_recovery_code()
        acc.password_hash = hash_password(password)
        acc.recovery_hash = hash_password(normalise_recovery(new_code))
        for sess in s.scalars(select(AuthSession).where(AuthSession.account_id == acc.id)):
            s.delete(sess)
        p = s.scalar(select(Patient).where(Patient.account_id == acc.id).order_by(Patient.id).limit(1))
        s.commit()
        aid, pid = acc.id, (p.id if p else None)
    RESET_THROTTLE.reset(*keys)
    LOGIN_THROTTLE.reset(f"login:{norm}")
    resp = templates.TemplateResponse(request, "recovery.html", ctx(request, code=new_code, reason="reset"))
    start_session(resp, request, aid, pid)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/people/recovery-code")
def regenerate_recovery(request: Request):
    """Show a new recovery code (the old one stops working)."""
    u = CURRENT_USER.get()
    if u["is_demo"]:
        return RedirectResponse("/people", status_code=303)
    code = new_recovery_code()
    with SessionLocal() as s:
        s.get(Account, u["account_id"]).recovery_hash = hash_password(normalise_recovery(code))
        s.commit()
    resp = templates.TemplateResponse(request, "recovery.html", ctx(request, code=code, reason="new"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if CURRENT_USER.get():
        return RedirectResponse(safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", ctx(request, next=safe_next(next), login_value="", error=None))


@app.post("/login")
def login(request: Request, login: str = Form(""), password: str = Form(""), next: str = Form("/")):
    lang = request.cookies.get("lang", "en")
    norm = normalise_login(login) or (login or "").strip().lower()[:160]
    ip = client_ip(request)
    keys = (f"login:{norm}", f"ip:{ip}")
    if LOGIN_THROTTLE.blocked(*keys):
        return templates.TemplateResponse(request, "login.html", ctx(
            request, next=safe_next(next), login_value=login, error=auth_error("locked", lang)), status_code=429)
    with SessionLocal() as s:
        acc = s.scalar(select(Account).where(Account.login == norm))
        ok = verify_password(password, acc.password_hash if acc else None)
        pid = None
        if ok:
            p = s.scalar(select(Patient).where(Patient.account_id == acc.id).order_by(Patient.id).limit(1))
            pid = p.id if p else None
    if not ok:
        LOGIN_THROTTLE.fail(*keys)
        return templates.TemplateResponse(request, "login.html", ctx(
            request, next=safe_next(next), login_value=login, error=auth_error("bad", lang)), status_code=401)
    LOGIN_THROTTLE.reset(f"login:{norm}")
    resp = RedirectResponse(safe_next(next), status_code=303)
    start_session(resp, request, acc.id, pid)
    return resp


@app.post("/logout")
def logout(request: Request):
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        with SessionLocal() as s:
            sess = s.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(tok)))
            if sess:
                s.delete(sess)
                s.commit()
    resp = RedirectResponse("/welcome", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/try/{key}")
def try_demo(request: Request, key: str):
    """One click from the landing page into a sample patient. Sample accounts have no password and are purged after 2 days."""
    demo = DEMOS.get(key)
    if not demo:
        return RedirectResponse("/welcome", status_code=303)
    ip = client_ip(request)
    if SAMPLE_THROTTLE.blocked(f"ip:{ip}"):
        return too_many(request)
    SAMPLE_THROTTLE.hit(f"ip:{ip}")
    with SessionLocal() as s:
        acc = Account(login=f"sample:{secrets.token_hex(8)}", name="Sample", is_demo=True)
        s.add(acc)
        s.flush()
        p = Patient(account_id=acc.id, name=demo["name"])
        s.add(p)
        s.flush()
        apply_demo(s, p, demo)
        s.commit()
        aid, pid = acc.id, p.id
    resp = RedirectResponse("/", status_code=303)
    start_session(resp, request, aid, pid)
    return resp


def delete_patient_data(s, p: Patient) -> None:
    for model in (Entry, Review, Draft):
        for row in s.scalars(select(model).where(model.patient_id == p.id)):
            s.delete(row)
    for sess in s.scalars(select(AuthSession).where(AuthSession.patient_id == p.id)):
        sess.patient_id = None
    s.delete(p)


def purge_old_demos() -> None:
    cutoff = datetime.now() - timedelta(days=2)
    with SessionLocal() as s:
        for acc in s.scalars(select(Account).where(Account.is_demo.is_(True), Account.created_at < cutoff)):
            for p in s.scalars(select(Patient).where(Patient.account_id == acc.id)):
                delete_patient_data(s, p)
            for sess in s.scalars(select(AuthSession).where(AuthSession.account_id == acc.id)):
                s.delete(sess)
            s.delete(acc)
        for sess in s.scalars(select(AuthSession).where(AuthSession.expires_at < datetime.now())):
            s.delete(sess)
        for d in s.scalars(select(Draft).where(Draft.created_at < datetime.now() - timedelta(days=1))):
            s.delete(d)
        s.commit()


# ---------------------------------------------------------------- people (one account, several people)
@app.get("/people", response_class=HTMLResponse)
def people_page(request: Request, error: str | None = None):
    u = CURRENT_USER.get()
    with SessionLocal() as s:
        people = list(s.scalars(select(Patient).where(Patient.account_id == u["account_id"]).order_by(Patient.id)))
        counts = {p.id: len(list(s.scalars(select(Entry.id).where(Entry.patient_id == p.id)))) for p in people}
    return templates.TemplateResponse(request, "people.html", ctx(request, people=people, counts=counts, error=error,
                                                                  child_below=KB.dose_limits.get("child_below_age", 12)))


def _owned_person(s, person_id: int) -> Patient | None:
    p = s.get(Patient, person_id)
    return p if p and p.account_id == CURRENT_USER.get()["account_id"] else None


@app.post("/people")
def add_person(name: str = Form(""), age: str = Form(""), weight: str = Form("")):
    name = " ".join(name.split())[:120]
    if not name:
        return RedirectResponse("/people?error=name", status_code=303)
    u = CURRENT_USER.get()
    with SessionLocal() as s:
        p = Patient(account_id=u["account_id"], name=name, age=parse_age(age), weight_kg=parse_weight(weight))
        s.add(p)
        s.flush()
        s.get(AuthSession, u["session_id"]).patient_id = p.id
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/people/{person_id}/switch")
def switch_person(person_id: int):
    u = CURRENT_USER.get()
    with SessionLocal() as s:
        if _owned_person(s, person_id):
            s.get(AuthSession, u["session_id"]).patient_id = person_id
            s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/people/{person_id}/update")
def update_person(person_id: int, name: str = Form(""), age: str = Form(""), weight: str = Form(""), back: str = Form("/people")):
    with SessionLocal() as s:
        p = _owned_person(s, person_id)
        if p:
            p.name = " ".join(name.split())[:120] or p.name
            p.age = parse_age(age)
            p.weight_kg = parse_weight(weight)
            s.commit()
    return RedirectResponse(safe_next(back), status_code=303)


@app.post("/people/{person_id}/delete")
def delete_person(person_id: int):
    u = CURRENT_USER.get()
    with SessionLocal() as s:
        mine = list(s.scalars(select(Patient).where(Patient.account_id == u["account_id"])))
        p = _owned_person(s, person_id)
        if p and len(mine) > 1:
            delete_patient_data(s, p)
            s.commit()
            return RedirectResponse("/people", status_code=303)
    return RedirectResponse("/people?error=last", status_code=303)


# ---------------------------------------------------------------- patient pages
@app.get("/", response_class=HTMLResponse)
def home(request: Request, added: int | None = None):
    lang = request.cookies.get("lang", "en")
    with SessionLocal() as s:
        patient, entries, result = current_state(s, lang)
        review = latest_review(s, patient.id)
    tags, graph, expiry = build_view(entries, result)
    added_entry = next((e for e in entries if e.id == added), None)
    return templates.TemplateResponse(request, "home.html", ctx(
        request, patient=patient, entries=entries, result=result, tags=tags, graph=graph, expiry=expiry,
        review=review if review and review.reviewed_at else None, added_entry=added_entry,
        uses=uses_view(entries, lang), allergy_labels=allergy_labels(patient, lang), today=today_label(lang),
        symptom_labels=[KB.symptom_label(c, lang) for c in patient.symptoms or [] if c in KB.symptom_by_code]))


HI_DAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
HI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def today_label(lang: str) -> str:
    d = datetime.now()
    if lang == "hi":
        return f"{HI_DAYS[d.weekday()]}, {d.day} {HI_MONTHS[d.month - 1]}"
    return d.strftime("%A, %d %B").upper()


def allergy_labels(patient, lang: str) -> list[str]:
    return [(KB.group_by_code[a].get(lang) or KB.group_by_code[a]["en"]) if a in KB.group_by_code else a
            for a in patient.allergies or []]


@app.get("/health", response_class=HTMLResponse)
def health_page(request: Request):
    lang = request.cookies.get("lang", "en")
    with SessionLocal() as s:
        patient = get_patient(s)
    others = [a for a in patient.allergies or [] if a not in KB.group_by_code]
    return templates.TemplateResponse(request, "health.html", ctx(
        request, patient=patient, groups=KB.allergy_groups, symptoms=KB.symptoms,
        chosen_allergies=set(patient.allergies or []), chosen_symptoms=set(patient.symptoms or []),
        other_allergies=", ".join(others)))


@app.post("/health")
async def save_health(request: Request):
    form = await request.form()
    groups = [g for g in form.getlist("allergy") if g in KB.group_by_code]
    others = [o.strip()[:60] for o in str(form.get("other_allergies", "")).split(",") if o.strip()][:10]
    symptoms = [c for c in form.getlist("symptom") if c in KB.symptom_by_code]
    none_known = form.get("no_allergies") == "on"
    if none_known:
        groups, others = [], []
    await run_in_threadpool(store_health, CURRENT_USER.get(), groups + [o for o in others if o not in groups], none_known, symptoms)
    return RedirectResponse("/?health=1", status_code=303)


def store_health(user: dict, given: list[str], none_known: bool, symptoms: list[str]) -> None:
    with SessionLocal() as s:
        p = get_patient(s, user)
        # [] = "no known allergies" (told us); None = not told yet.
        p.allergies = given if given else ([] if none_known else None)
        p.symptoms = symptoms
        s.commit()


@app.get("/pharmacist", response_class=HTMLResponse)
def pharmacist_view(request: Request):
    lang = request.cookies.get("lang", "en")
    with SessionLocal() as s:
        patient, entries, result = current_state(s, lang)
        review = latest_review(s, patient.id)
    tags, graph, expiry = build_view(entries, result)
    flagged = {eid for a in result["alerts"] for eid in a["entry_ids"]}
    return templates.TemplateResponse(request, "pharmacist.html", ctx(
        request, patient=patient, entries=entries, result=result, flagged=flagged, tags=tags, graph=graph,
        expiry=expiry, review=review if review and review.reviewed_at else None, kb=KB,
        allergy_labels=allergy_labels(patient, lang),
        symptom_labels=[KB.symptom_label(c, lang) for c in patient.symptoms or [] if c in KB.symptom_by_code]))


@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "add.html", ctx(request, error=error))


@app.post("/extract")
async def extract(request: Request, photo: UploadFile = File(...)):
    data = await photo.read(MAX_UPLOAD + 1)  # never read more than the limit into memory
    if not data:
        return RedirectResponse("/add?error=empty", status_code=303)
    if len(data) > MAX_UPLOAD:
        return RedirectResponse("/add?error=too_large", status_code=303)
    mime = photo.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        return RedirectResponse("/add?error=not_image", status_code=303)
    # The Gemini call and the DB write are blocking: run them on a worker thread so other users are not frozen.
    draft_id = await run_in_threadpool(read_label_into_draft, CURRENT_USER.get()["pid"], data, mime)
    return RedirectResponse(f"/confirm/{draft_id}", status_code=303)


def read_label_into_draft(pid: int, data: bytes, mime: str) -> int:
    try:
        transcription, src = vision.transcribe(data, mime)
        error = None
    except vision.VisionError as exc:
        transcription, src, error = None, None, str(exc)
    with SessionLocal() as s:
        d = Draft(patient_id=pid, origin="photo", extracted=({**transcription, "_source": src} if transcription else None),
                  error=error)
        s.add(d)
        s.commit()
        DRAFT_IMAGES.put(d.id, data, mime)
        return d.id


@app.post("/strip-qr")
def strip_qr(text: str = Form("")):
    """Text decoded (in the browser) from the QR code printed on the strip."""
    parsed = parse_strip_qr(text[:2000])
    with SessionLocal() as s:
        d = Draft(patient_id=CURRENT_USER.get()["pid"], origin="qr", extracted={**parsed, "_source": "qr"} if parsed else None,
                  error=None if parsed else "The QR code did not contain medicine details. Please enter them manually.")
        s.add(d)
        s.commit()
        DRAFT_IMAGES.pop(d.id, None)
        return RedirectResponse(f"/confirm/{d.id}", status_code=303)


@app.get("/manual")
def manual(q: str = ""):
    with SessionLocal() as s:
        d = Draft(patient_id=CURRENT_USER.get()["pid"], origin="manual", extracted={"brand_as_printed": q.strip()[:80]} if q.strip() else None)
        s.add(d)
        s.commit()
        DRAFT_IMAGES.pop(d.id, None)
        return RedirectResponse(f"/confirm/{d.id}", status_code=303)


@app.get("/draft-image/{draft_id}")
def draft_image(draft_id: int):
    with SessionLocal() as s:
        mine = owned_draft(s, draft_id) is not None
    hit = DRAFT_IMAGES.get(draft_id) if mine else None
    if not hit:
        return Response(status_code=404)
    data, mime = hit
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/confirm/{draft_id}", response_class=HTMLResponse)
def confirm_page(request: Request, draft_id: int, error: str | None = None):
    with SessionLocal() as s:
        d = owned_draft(s, draft_id)
    if not d:
        return RedirectResponse("/add", status_code=303)
    x = d.extracted or {}
    brand_text = x.get("brand_as_printed", "") if x else ""
    exact = KB.brand_matcher.exact(brand_text) if brand_text else None
    rows, rows_source = [], None
    if x.get("composition"):
        rows = [{"raw": c["ingredient_as_printed"], "strength": c["strength_as_printed"], "legibility": c["legibility"]}
                for c in x["composition"]]
        rows_source = "qr" if d.origin == "qr" else "pack"
    elif exact:
        rows = [{"raw": KB.display(i["canonical"]), "strength": i["strength"], "legibility": "catalogue"} for i in exact["ingredients"]]
        rows_source = "catalogue"
    return templates.TemplateResponse(request, "confirm.html", ctx(
        request, draft=d, x=x, brand_text=brand_text, exact=exact, rows=rows,
        rows_source=rows_source, has_image=draft_id in DRAFT_IMAGES, error=error, kb=KB))


@app.post("/entries")
def create_entry(request: Request, draft_id: int = Form(...), name: str = Form(""), brand_id: str = Form(""),
                 form: str = Form(""), note: str = Form(""), confirmed: str = Form(""),
                 expiry: str = Form(""), batch: str = Form(""), per_day: str = Form(""),
                 ing_raw: list[str] = Form(default=[]), ing_strength: list[str] = Form(default=[])):
    if confirmed != "on":
        return RedirectResponse(f"/confirm/{draft_id}?error=must_check", status_code=303)
    name = name.strip()[:160]
    if not name:
        return RedirectResponse(f"/confirm/{draft_id}?error=name", status_code=303)
    ingredients = [{"raw": r.strip()[:120], "strength": (st or "").strip()[:40]}
                   for r, st in zip(ing_raw, ing_strength + [""] * len(ing_raw)) if r.strip()]
    bid = int(brand_id) if brand_id.strip().isdigit() and int(brand_id) in KB.brand_by_id else None
    if bid is None:
        hit = KB.brand_matcher.exact(name)
        bid = hit["id"] if hit else None
    with SessionLocal() as s:
        d = owned_draft(s, draft_id)
        printed = None
        if d and d.extracted and d.extracted.get("composition"):
            printed = [{"raw": c["ingredient_as_printed"], "strength": c["strength_as_printed"]} for c in d.extracted["composition"]]
        patient = get_patient(s)
        e = Entry(patient_id=patient.id, name=name, brand_id=bid, form=form.strip()[:40], note=note.strip()[:255],
                  origin=d.origin if d else "manual", ingredients=ingredients, printed_ingredients=printed,
                  expiry=expiry.strip()[:40] or None, batch=batch.strip()[:60] or None, per_day=parse_amount(per_day))
        s.add(e)
        if d:
            s.delete(d)
        s.commit()
        DRAFT_IMAGES.pop(draft_id, None)
        return RedirectResponse(f"/?added={e.id}", status_code=303)


@app.post("/drafts/{draft_id}/discard")
def discard(draft_id: int):
    with SessionLocal() as s:
        d = owned_draft(s, draft_id)
        if d:
            DRAFT_IMAGES.pop(draft_id, None)
            s.delete(d)
            s.commit()
    return RedirectResponse("/add", status_code=303)


@app.post("/entries/{entry_id}/delete")
def delete_entry(entry_id: int, back: str = Form("/")):
    with SessionLocal() as s:
        e = owned_entry(s, entry_id)
        if e:
            s.delete(e)
            s.commit()
    return RedirectResponse(back if back in ("/", "/pharmacist") else "/", status_code=303)


@app.post("/entries/{entry_id}/amount")
def set_amount(entry_id: int, per_day: str = Form(""), back: str = Form("/")):
    with SessionLocal() as s:
        e = owned_entry(s, entry_id)
        if e:
            e.per_day = parse_amount(per_day)
            s.commit()
    return RedirectResponse(back if back in ("/", "/pharmacist") else "/", status_code=303)


@app.post("/patient")
def update_patient(name: str = Form(""), age: str = Form(""), weight: str = Form(""), back: str = Form("/")):
    with SessionLocal() as s:
        p = get_patient(s)
        p.name = " ".join(name.split())[:120] or p.name
        p.age = parse_age(age)
        p.weight_kg = parse_weight(weight)
        s.commit()
    return RedirectResponse(back if back in ("/", "/pharmacist") else "/", status_code=303)


@app.post("/demo/{key}")
def load_demo(key: str):
    if not CURRENT_USER.get()["is_demo"]:
        return Response("Sample scenarios can only replace a sample patient's list.", status_code=403)
    with SessionLocal() as s:
        apply_demo(s, get_patient(s), DEMOS.get(key))
        s.commit()
    return RedirectResponse("/", status_code=303)


def apply_demo(s, p: Patient, demo: dict | None):
    """Replace a patient's list with a demo scenario (or clear it when demo is None)."""
    for e in s.scalars(select(Entry).where(Entry.patient_id == p.id)):
        s.delete(e)
    for r in s.scalars(select(Review).where(Review.patient_id == p.id)):
        s.delete(r)
    p.allergies, p.symptoms = (demo or {}).get("allergies", []), (demo or {}).get("symptoms", [])
    if demo:
        p.name, p.age, p.weight_kg = demo["name"], demo["age"], demo.get("weight_kg")
        for brand_name, note, exp, per_day in demo["entries"]:
            b = KB.brand_matcher.exact(brand_name)
            s.add(Entry(patient_id=p.id, name=b["name"], brand_id=b["id"], form=b["form"], note=note, origin="manual",
                        expiry=exp, per_day=per_day,
                        ingredients=[{"raw": KB.display(i["canonical"]), "strength": i["strength"]} for i in b["ingredients"]]))


# ---------------------------------------------------------------- pharmacist handoff
@app.get("/share", response_class=HTMLResponse)
def share_page(request: Request):
    lang = request.cookies.get("lang", "en")
    with SessionLocal() as s:
        patient, entries, result = current_state(s, lang)
        review = active_share(s, patient.id)
        if not review:
            review = Review(token=secrets.token_urlsafe(16), patient_id=patient.id,
                            expires_at=datetime.now() + timedelta(days=REVIEW_DAYS))
            if (prev := latest_review(s, patient.id)) is not None:
                review.items, review.reviewer, review.general_note = prev.items, prev.reviewer, prev.general_note
            s.add(review)
            s.commit()
        token, expires = review.token, review_expiry(review)
    url = f"{lan_base_url(request)}/p/{token}"
    summary = "\n".join(f"- {a['simple']}" for a in result["alerts"]) or "- No warnings from Medora's checks."
    share_text = f"Medora medicine summary for {patient.name or 'me'} ({len(entries)} medicines):\n{summary}\nPharmacist review: {url}"
    return templates.TemplateResponse(request, "share.html", ctx(
        request, patient=patient, entries=entries, result=result, url=url, qr=qr_svg(url), share_text=share_text,
        expires=expires, revoked=request.query_params.get("revoked") == "1"))


@app.post("/share/revoke")
def revoke_share():
    """Stop every open pharmacist link for this person. The next visit to /share makes a fresh one."""
    with SessionLocal() as s:
        p = get_patient(s)
        now = datetime.now()
        # MySQL DATETIME rounds to whole seconds, so "now" could be stored slightly in the future: backdate it.
        gone = now - timedelta(seconds=2)
        for r in s.scalars(select(Review).where(Review.patient_id == p.id)):
            if review_expiry(r) > gone:
                r.expires_at = gone
        s.commit()
    return RedirectResponse("/share?revoked=1", status_code=303)


def link_gone(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "message.html", ctx(
        request, title="This review link no longer works",
        body="Pharmacist links stop working after 14 days, or as soon as the patient makes a new one. "
             "Ask the patient to open Medora and show you their QR code again."), status_code=410)


@app.get("/p/{token}", response_class=HTMLResponse)
def review_page(request: Request, token: str, saved: int = 0):
    with SessionLocal() as s:
        review = s.scalar(select(Review).where(Review.token == token))
        if not review_active(review):
            return link_gone(request)
        patient, entries, result = current_state(s, "en", s.get(Patient, review.patient_id))
    tags, graph, expiry = build_view(entries, result)
    return templates.TemplateResponse(request, "review.html", ctx(
        request, review=review, patient=patient, entries=entries, result=result, tags=tags, expiry=expiry, saved=saved, kb=KB,
        allergy_labels=allergy_labels(patient, "en"),
        symptom_labels=[KB.symptom_label(c) for c in patient.symptoms or [] if c in KB.symptom_by_code]))


@app.post("/p/{token}")
async def save_review(request: Request, token: str):
    form = await request.form()
    ok = await run_in_threadpool(store_review, token, form)
    return RedirectResponse(f"/p/{token}?saved=1", status_code=303) if ok else link_gone(request)


def store_review(token: str, form) -> bool:
    with SessionLocal() as s:
        review = s.scalar(select(Review).where(Review.token == token))
        if not review_active(review):
            return False
        _, _, result = current_state(s, "en", s.get(Patient, review.patient_id))
        items = {}
        for a in result["alerts"]:
            k = a["key"]
            items[k] = {"done": form.get(f"done::{k}") == "on", "note": str(form.get(f"note::{k}", ""))[:300]}
        review.items = items
        review.reviewer = str(form.get("reviewer", ""))[:120]
        review.general_note = str(form.get("general_note", ""))[:500]
        review.reviewed_at = datetime.now()
        s.commit()
    return True


@app.get("/passport.pdf")
def passport_pdf():
    with SessionLocal() as s:
        patient, entries, result = current_state(s, "en")
    pdf = build_pdf(patient, entries, result, KB)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="medora-pharmacist-summary.pdf"'})


# ---------------------------------------------------------------- expert pages
@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    return templates.TemplateResponse(request, "rules.html", ctx(request, kb=KB))


@app.get("/catalogue", response_class=HTMLResponse)
def catalogue_page(request: Request):
    return templates.TemplateResponse(request, "catalogue.html", ctx(request, kb=KB))


@app.get("/eval", response_class=HTMLResponse)
def eval_page(request: Request):
    ev = run_all(KB)
    _EVAL_CACHE["ev"] = ev
    return templates.TemplateResponse(request, "eval.html", ctx(request, ev=ev))


@app.get("/settings")
def settings(request: Request, lang: str | None = None, large: str | None = None, back: str = "/"):
    resp = RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/", status_code=303)
    if lang in ("en", "hi"):
        resp.set_cookie("lang", lang, max_age=31536000, samesite="lax")
    if large in ("0", "1"):
        resp.set_cookie("large", large, max_age=31536000, samesite="lax")
    return resp


# ---------------------------------------------------------------- JSON helpers for the confirm screen
@app.get("/api/brands")
def api_brands(request: Request, q: str = ""):
    lang = request.cookies.get("lang", "en")
    return JSONResponse([
        {"id": b["id"], "name": b["name"], "manufacturer": b["manufacturer"], "form": b["form"], "score": b["score"],
         "uses": KB.uses(b["ingredients"][0]["canonical"], lang) if b["ingredients"] else None,
         "ingredients": [{"display": KB.display(i["canonical"]), "strength": i["strength"]} for i in b["ingredients"]]}
        for b in KB.brand_matcher.candidates(q[:80])])


@app.get("/api/recognise")
def api_recognise(name: str = ""):
    rec = KB.normalizer.recognise(name[:120])
    return {"canonical": rec.canonical, "display": KB.display(rec.canonical) if rec.canonical else None,
            "method": rec.method, "suggestion": KB.display(rec.suggestion) if rec.suggestion else None}
