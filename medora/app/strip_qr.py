"""Parse the QR code printed on Indian medicine strips.

Since Aug 2023, the top 300 brands must carry a QR code with: unique product
ID, proper/generic name, brand name, manufacturer, batch number, manufacturing
date, expiry date and licence number. Layouts vary by manufacturer, so this is
a tolerant key/value parser. Its output goes through the same confirm screen
as a photo: the user still confirms every field.
"""
from __future__ import annotations

import re

KEYS = {
    "brand": r"brand(?:\s*name)?",
    "generic": r"(?:generic|proper)(?:\s*name)?|composition|salt",
    "manufacturer": r"(?:mfr|mfd\.?\s*by|manufactur(?:er|ed\s*by))(?:\s*name)?",
    "batch": r"batch(?:\s*(?:no\.?|number))?|b\.?\s*no\.?|lot(?:\s*no\.?)?",
    "expiry": r"exp(?:iry)?\.?(?:\s*date)?|use\s*before",
    "mfg_date": r"mfg\.?\s*date|date\s*of\s*manufactur\w*|mfd\.?(?:\s*date)?",
}
STRENGTH = re.compile(r"(\d+(?:\.\d+)?\s*(?:mg|mcg|µg|g|iu)\b)", re.I)
FORM_WORDS = re.compile(r"\b(tablets?|capsules?|syrup|suspension|injection|i\.?p\.?|b\.?p\.?|u\.?s\.?p\.?)\b", re.I)


def _fields(text: str) -> dict:
    out = {}
    # Split on newlines, semicolons, pipes or ' , Key:' boundaries.
    parts = re.split(r"[\n;|]+|,\s*(?=[A-Za-z .]{2,30}:)", text)
    for part in parts:
        if ":" not in part and "=" not in part:
            continue
        k, v = re.split(r"[:=]", part, maxsplit=1)
        k, v = k.strip().lower(), v.strip()
        for field, pat in KEYS.items():
            if field not in out and re.fullmatch(pat, k, re.I):
                out[field] = v
                break
    return out


def _composition(generic: str) -> list[dict]:
    comps = []
    for chunk in re.split(r"\s*(?:\+|,|&|\band\b)\s*", generic):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = STRENGTH.search(chunk)
        strength = m.group(1) if m else ""
        name = STRENGTH.sub("", chunk)
        name = FORM_WORDS.sub("", name)
        name = re.sub(r"\s+", " ", name).strip(" .-")
        if name:
            comps.append({"ingredient_as_printed": name, "strength_as_printed": strength, "legibility": "clear"})
    return comps


def parse_strip_qr(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    f = _fields(text)
    if not f.get("brand") and not f.get("generic"):
        return None
    comps = _composition(f.get("generic", ""))
    return {
        "is_medicine_pack": True,
        "brand_as_printed": f.get("brand", ""),
        "brand_legibility": "clear",
        "manufacturer_as_printed": f.get("manufacturer", ""),
        "dosage_form_as_printed": "",
        "composition_visible": bool(comps),
        "composition": comps,
        "expiry_as_printed": f.get("expiry", ""),
        "batch_as_printed": f.get("batch", ""),
        "_raw_qr": text[:500],
    }
