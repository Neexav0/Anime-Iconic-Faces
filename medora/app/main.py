from __future__ import annotations

import io
import os
import re
import secrets
import socket
from datetime import datetime
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from . import vision
from .db import DATABASE_URL, SessionLocal
from .evaluation import run_all
from .i18n import translator
from .matching import clean, parse_expiry
from .models import Draft, Entry, Patient, Review
from .pdf import build_pdf
from .rules_engine import evaluate
from .seed import init_db, load_kb
from .strip_qr import parse_strip_qr

APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Medora")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

KB = None
# Photos are held in memory only until the user confirms or discards the draft.
DRAFT_IMAGES: dict[int, tuple[bytes, str]] = {}
MAX_UPLOAD = 10 * 1024 * 1024

DEMOS = {
    "paracetamol": {"title": "Hidden paracetamol: fever tablet + cold tablet", "age": 72, "name": "Kamla Devi",
                    "entries": [("Dolo 650", "Dr. Mehta (fever)", None), ("Sinarest", "Bought at chemist for cold", None)]},
    "bp": {"title": "Two doctors, same BP ingredient", "age": 68, "name": "Ramesh Kumar",
           "entries": [("Telma-H", "Cardiologist", None), ("Telma-AM", "Family physician", None),
                       ("Glycomet 500", "Diabetologist", None), ("Janumet 50/500", "Diabetologist (new)", "EXP 08/2025")]},
    "bleed": {"title": "Blood thinner + painkiller", "age": 74, "name": "Suresh Iyer",
              "entries": [("Warf 5", "Cardiologist", None), ("Combiflam", "Bought for knee pain", None), ("Pan 40", "", None)]},
    "allergy": {"title": "Allergy + side-effect check", "age": 70, "name": "Lakshmi Rao",
                "allergies": ["sulfa", "penicillin"], "symptoms": ["ankle_swelling", "drowsiness"],
                "entries": [("Telma-H", "Cardiologist", None), ("Amlokind 5", "Cardiologist", None),
                            ("Augmentin 625 Duo", "Dentist", None), ("Cetzine", "Chemist, for cold", None)]},
}


@app.on_event("startup")
def startup():
    global KB
    init_db()
    KB = load_kb()


def ctx(request: Request, **kw):
    lang = request.cookies.get("lang", "en")
    return {"request": request, "t": translator(lang), "lang": lang, "demos": DEMOS,
            "large": request.cookies.get("large") == "1", "db_kind": DATABASE_URL.split(":")[0].split("+")[0], **kw}


def get_patient(s) -> Patient:
    return s.scalar(select(Patient).order_by(Patient.id).limit(1))


def current_state(s, lang: str):
    patient = get_patient(s)
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
    return s.scalar(select(Review).where(Review.patient_id == patient_id).order_by(Review.id.desc()).limit(1))


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
                               "also": others})
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
    return f"{request.url.scheme}://{host}" + (f":{port}" if port else "")


def qr_svg(data: str) -> str:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return re.sub(r"<\?xml[^>]*\?>", "", svg).replace("<svg ", '<svg role="img" aria-label="QR code" ', 1)


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
        uses=uses_view(entries, lang), allergy_labels=allergy_labels(patient, lang),
        symptom_labels=[KB.symptom_label(c, lang) for c in patient.symptoms or [] if c in KB.symptom_by_code]))


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
    if form.get("no_allergies") == "on":
        groups, others = [], []
    with SessionLocal() as s:
        p = get_patient(s)
        given = groups + [o for o in others if o not in groups]
        # [] = "no known allergies" (told us); None = not told yet.
        p.allergies = given if given else ([] if form.get("no_allergies") == "on" else None)
        p.symptoms = symptoms
        s.commit()
    return RedirectResponse("/?health=1", status_code=303)


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
    data = await photo.read()
    if not data:
        return RedirectResponse("/add?error=empty", status_code=303)
    if len(data) > MAX_UPLOAD:
        return RedirectResponse("/add?error=too_large", status_code=303)
    mime = photo.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        return RedirectResponse("/add?error=not_image", status_code=303)
    try:
        transcription, src = vision.transcribe(data, mime)
        error = None
    except vision.VisionError as exc:
        transcription, src, error = None, None, str(exc)
    with SessionLocal() as s:
        d = Draft(origin="photo", extracted=({**transcription, "_source": src} if transcription else None), error=error)
        s.add(d)
        s.commit()
        DRAFT_IMAGES[d.id] = (data, mime)
        return RedirectResponse(f"/confirm/{d.id}", status_code=303)


@app.post("/strip-qr")
def strip_qr(text: str = Form("")):
    """Text decoded (in the browser) from the QR code printed on the strip."""
    parsed = parse_strip_qr(text[:2000])
    with SessionLocal() as s:
        d = Draft(origin="qr", extracted={**parsed, "_source": "qr"} if parsed else None,
                  error=None if parsed else "The QR code did not contain medicine details. Please enter them manually.")
        s.add(d)
        s.commit()
        DRAFT_IMAGES.pop(d.id, None)
        return RedirectResponse(f"/confirm/{d.id}", status_code=303)


@app.get("/manual")
def manual():
    with SessionLocal() as s:
        d = Draft(origin="manual", extracted=None)
        s.add(d)
        s.commit()
        DRAFT_IMAGES.pop(d.id, None)
        return RedirectResponse(f"/confirm/{d.id}", status_code=303)


@app.get("/draft-image/{draft_id}")
def draft_image(draft_id: int):
    if draft_id not in DRAFT_IMAGES:
        return Response(status_code=404)
    data, mime = DRAFT_IMAGES[draft_id]
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/confirm/{draft_id}", response_class=HTMLResponse)
def confirm_page(request: Request, draft_id: int, error: str | None = None):
    with SessionLocal() as s:
        d = s.get(Draft, draft_id)
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
                 expiry: str = Form(""), batch: str = Form(""),
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
        d = s.get(Draft, draft_id)
        printed = None
        if d and d.extracted and d.extracted.get("composition"):
            printed = [{"raw": c["ingredient_as_printed"], "strength": c["strength_as_printed"]} for c in d.extracted["composition"]]
        patient = get_patient(s)
        e = Entry(patient_id=patient.id, name=name, brand_id=bid, form=form.strip()[:40], note=note.strip()[:255],
                  origin=d.origin if d else "manual", ingredients=ingredients, printed_ingredients=printed,
                  expiry=expiry.strip()[:40] or None, batch=batch.strip()[:60] or None)
        s.add(e)
        if d:
            s.delete(d)
        s.commit()
        DRAFT_IMAGES.pop(draft_id, None)
        return RedirectResponse(f"/?added={e.id}", status_code=303)


@app.post("/drafts/{draft_id}/discard")
def discard(draft_id: int):
    DRAFT_IMAGES.pop(draft_id, None)
    with SessionLocal() as s:
        d = s.get(Draft, draft_id)
        if d:
            s.delete(d)
            s.commit()
    return RedirectResponse("/add", status_code=303)


@app.post("/entries/{entry_id}/delete")
def delete_entry(entry_id: int, back: str = Form("/")):
    with SessionLocal() as s:
        e = s.get(Entry, entry_id)
        if e:
            s.delete(e)
            s.commit()
    return RedirectResponse(back if back in ("/", "/pharmacist") else "/", status_code=303)


@app.post("/patient")
def update_patient(name: str = Form(""), age: str = Form(""), back: str = Form("/")):
    with SessionLocal() as s:
        p = get_patient(s)
        p.name = name.strip()[:120]
        p.age = int(age) if age.strip().isdigit() and 0 < int(age) < 125 else None
        s.commit()
    return RedirectResponse(back if back in ("/", "/pharmacist") else "/", status_code=303)


@app.post("/demo/{key}")
def load_demo(key: str):
    demo = DEMOS.get(key)
    with SessionLocal() as s:
        p = get_patient(s)
        for e in s.scalars(select(Entry).where(Entry.patient_id == p.id)):
            s.delete(e)
        for r in s.scalars(select(Review).where(Review.patient_id == p.id)):
            s.delete(r)
        p.allergies, p.symptoms = (demo or {}).get("allergies", []), (demo or {}).get("symptoms", [])
        if demo:
            p.name, p.age = demo["name"], demo["age"]
            for brand_name, note, exp in demo["entries"]:
                b = KB.brand_matcher.exact(brand_name)
                s.add(Entry(patient_id=p.id, name=b["name"], brand_id=b["id"], form=b["form"], note=note, origin="manual",
                            expiry=exp,
                            ingredients=[{"raw": KB.display(i["canonical"]), "strength": i["strength"]} for i in b["ingredients"]]))
        s.commit()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- pharmacist handoff
@app.get("/share", response_class=HTMLResponse)
def share_page(request: Request):
    lang = request.cookies.get("lang", "en")
    with SessionLocal() as s:
        patient, entries, result = current_state(s, lang)
        review = latest_review(s, patient.id)
        if not review:
            review = Review(token=secrets.token_urlsafe(12), patient_id=patient.id)
            s.add(review)
            s.commit()
        token = review.token
    url = f"{lan_base_url(request)}/p/{token}"
    summary = "\n".join(f"- {a['simple']}" for a in result["alerts"]) or "- No warnings from Medora's checks."
    share_text = f"Medora medicine summary for {patient.name or 'me'} ({len(entries)} medicines):\n{summary}\nPharmacist review: {url}"
    return templates.TemplateResponse(request, "share.html", ctx(
        request, patient=patient, entries=entries, result=result, url=url, qr=qr_svg(url), share_text=share_text))


@app.get("/p/{token}", response_class=HTMLResponse)
def review_page(request: Request, token: str, saved: int = 0):
    with SessionLocal() as s:
        review = s.scalar(select(Review).where(Review.token == token))
        if not review:
            return HTMLResponse("<h1>This review link is not valid.</h1>", status_code=404)
        patient, entries, result = current_state(s, "en")
    tags, graph, expiry = build_view(entries, result)
    return templates.TemplateResponse(request, "review.html", ctx(
        request, review=review, patient=patient, entries=entries, result=result, tags=tags, expiry=expiry, saved=saved, kb=KB,
        allergy_labels=allergy_labels(patient, "en"),
        symptom_labels=[KB.symptom_label(c) for c in patient.symptoms or [] if c in KB.symptom_by_code]))


@app.post("/p/{token}")
async def save_review(request: Request, token: str):
    form = await request.form()
    with SessionLocal() as s:
        review = s.scalar(select(Review).where(Review.token == token))
        if not review:
            return Response(status_code=404)
        _, _, result = current_state(s, "en")
        items = {}
        for a in result["alerts"]:
            k = a["key"]
            items[k] = {"done": form.get(f"done::{k}") == "on", "note": str(form.get(f"note::{k}", ""))[:300]}
        review.items = items
        review.reviewer = str(form.get("reviewer", ""))[:120]
        review.general_note = str(form.get("general_note", ""))[:500]
        review.reviewed_at = datetime.now()
        s.commit()
    return RedirectResponse(f"/p/{token}?saved=1", status_code=303)


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
    return templates.TemplateResponse(request, "eval.html", ctx(request, ev=run_all(KB)))


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
def api_brands(q: str = ""):
    return JSONResponse([
        {"id": b["id"], "name": b["name"], "manufacturer": b["manufacturer"], "form": b["form"], "score": b["score"],
         "ingredients": [{"display": KB.display(i["canonical"]), "strength": i["strength"]} for i in b["ingredients"]]}
        for b in KB.brand_matcher.candidates(q[:80])])


@app.get("/api/recognise")
def api_recognise(name: str = ""):
    rec = KB.normalizer.recognise(name[:120])
    return {"canonical": rec.canonical, "display": KB.display(rec.canonical) if rec.canonical else None,
            "method": rec.method, "suggestion": KB.display(rec.suggestion) if rec.suggestion else None}
