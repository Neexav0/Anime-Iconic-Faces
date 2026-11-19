from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from . import vision
from .db import DATABASE_URL, SessionLocal
from .evaluation import run_all
from .i18n import translator
from .matching import clean
from .models import Draft, Entry, Patient
from .pdf import build_pdf
from .rules_engine import evaluate
from .seed import init_db, load_kb

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
                    "entries": [("Dolo 650", "Dr. Mehta (fever)"), ("Sinarest", "Bought at chemist for cold")]},
    "bp": {"title": "Two doctors, same BP ingredient", "age": 68, "name": "Ramesh Kumar",
           "entries": [("Telma-H", "Cardiologist"), ("Telma-AM", "Family physician"), ("Glycomet 500", "Diabetologist"), ("Janumet 50/500", "Diabetologist (new)")]},
    "bleed": {"title": "Blood thinner + painkiller", "age": 74, "name": "Suresh Iyer",
              "entries": [("Warf 5", "Cardiologist"), ("Combiflam", "Bought for knee pain"), ("Pan 40", "")]},
}


@app.on_event("startup")
def startup():
    global KB
    init_db()
    KB = load_kb()


def ctx(request: Request, **kw):
    lang = request.cookies.get("lang", "en")
    return {"request": request, "t": translator(lang), "lang": lang,
            "large": request.cookies.get("large") == "1", "db_kind": DATABASE_URL.split(":")[0].split("+")[0], **kw}


def get_patient(s) -> Patient:
    return s.scalar(select(Patient).order_by(Patient.id).limit(1))


def current_state(s, lang: str):
    patient = get_patient(s)
    entries = list(s.scalars(select(Entry).where(Entry.patient_id == patient.id).order_by(Entry.id)))
    result = evaluate([e.as_engine_dict() for e in entries], {"age": patient.age}, KB, lang=lang)
    return patient, entries, result


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index(request: Request, added: int | None = None):
    with SessionLocal() as s:
        patient, entries, result = current_state(s, request.cookies.get("lang", "en"))
    flagged = {eid for a in result["alerts"] for eid in a["entry_ids"]}
    na_ids = {n["entry_id"] for n in result["not_assessed"]}
    dup_keys = {a.get("ingredient_key") for a in result["alerts"] if a["rule_id"] == "R-DUP-001"}
    tags = {}
    for e in entries:
        tags[e.id] = []
        for ing in e.ingredients or []:
            rec = KB.normalizer.recognise(ing["raw"])
            key = rec.canonical or "?" + clean(ing["raw"])
            label = (KB.display(rec.canonical) if rec.canonical else ing["raw"]) + (f" {ing['strength']}" if ing.get("strength") else "")
            tags[e.id].append({"label": label, "kind": "dup" if key in dup_keys else ("" if rec.canonical else "unk")})
    return templates.TemplateResponse(request, "index.html", ctx(
        request, patient=patient, entries=entries, result=result, flagged=flagged, na_ids=na_ids,
        added=added, demos=DEMOS, kb=KB, tags=tags))


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
    candidates = KB.brand_matcher.candidates(brand_text) if brand_text else []
    exact = KB.brand_matcher.exact(brand_text) if brand_text else None
    rows, rows_source = [], None
    if x.get("composition"):
        rows = [{"raw": c["ingredient_as_printed"], "strength": c["strength_as_printed"], "legibility": c["legibility"]}
                for c in x["composition"]]
        rows_source = "pack"
    elif exact:
        rows = [{"raw": KB.display(i["canonical"]), "strength": i["strength"], "legibility": "catalogue"} for i in exact["ingredients"]]
        rows_source = "catalogue"
    return templates.TemplateResponse(request, "confirm.html", ctx(
        request, draft=d, x=x, brand_text=brand_text, candidates=candidates, exact=exact, rows=rows,
        rows_source=rows_source, has_image=draft_id in DRAFT_IMAGES, error=error, kb=KB))


@app.post("/entries")
def create_entry(request: Request, draft_id: int = Form(...), name: str = Form(""), brand_id: str = Form(""),
                 form: str = Form(""), note: str = Form(""), confirmed: str = Form(""),
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
                  origin=d.origin if d else "manual", ingredients=ingredients, printed_ingredients=printed)
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
def delete_entry(entry_id: int):
    with SessionLocal() as s:
        e = s.get(Entry, entry_id)
        if e:
            s.delete(e)
            s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/patient")
def update_patient(name: str = Form(""), age: str = Form("")):
    with SessionLocal() as s:
        p = get_patient(s)
        p.name = name.strip()[:120]
        p.age = int(age) if age.strip().isdigit() and 0 < int(age) < 125 else None
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/demo/{key}")
def load_demo(key: str):
    demo = DEMOS.get(key)
    with SessionLocal() as s:
        p = get_patient(s)
        for e in s.scalars(select(Entry).where(Entry.patient_id == p.id)):
            s.delete(e)
        if demo:
            p.name, p.age = demo["name"], demo["age"]
            for brand_name, note in demo["entries"]:
                b = KB.brand_matcher.exact(brand_name)
                s.add(Entry(patient_id=p.id, name=b["name"], brand_id=b["id"], form=b["form"], note=note, origin="manual",
                            ingredients=[{"raw": KB.display(i["canonical"]), "strength": i["strength"]} for i in b["ingredients"]]))
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/passport.pdf")
def passport_pdf():
    with SessionLocal() as s:
        patient, entries, result = current_state(s, "en")
    pdf = build_pdf(patient, entries, result, KB)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="medora-pharmacist-summary.pdf"'})


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
