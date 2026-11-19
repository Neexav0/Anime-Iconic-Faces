"""Deterministic name normalisation and catalogue matching.

Recognition of an ingredient is deterministic: exact name, listed synonym, or
salt-stripped name. Fuzzy matching only produces *suggestions* the user must
accept; it never silently changes what the user confirmed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process

PHARMACOPOEIA = re.compile(r"\b(i\.?\s?p\.?|b\.?\s?p\.?|u\.?\s?s\.?\s?p\.?|ph\.?\s?eur\.?)(?=\s|$|,)", re.I)
STRENGTH = re.compile(r"\d+(?:\.\d+)?\s*(mg|mcg|µg|g|iu|ml|%)\b", re.I)
FILLER = re.compile(r"\b(eq\.?|equivalent|equiv\.?|to|anhydrous)\b", re.I)
SALTS = {
    "sodium", "potassium", "hydrochloride", "hcl", "maleate", "besylate", "besilate",
    "calcium", "magnesium", "succinate", "tartrate", "mesylate", "citrate", "sulphate",
    "sulfate", "monohydrate", "dihydrate", "trihydrate", "hemihydrate", "phosphate",
    "bromide", "acetate", "fumarate", "dihydrochloride", "trihydrate",
}


def clean(raw: str) -> str:
    """Lower-case and strip pharmacopoeia tags, strengths, brackets and filler."""
    s = (raw or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = PHARMACOPOEIA.sub(" ", s)
    s = STRENGTH.sub(" ", s)
    s = FILLER.sub(" ", s)
    s = re.sub(r"[^a-z0-9\- ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_mg(strength: str | None) -> float | None:
    """'650 mg' -> 650.0, '50 mcg' -> 0.05, '1 g' -> 1000; IU/ml/unknown -> None."""
    if not strength:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g)\b", strength, re.I)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    return {"mg": val, "g": val * 1000, "mcg": val / 1000, "µg": val / 1000}[unit]


def parse_dose_strength(strength: str | None) -> tuple[float, str] | None:
    """Amount of ingredient in one unit you take.
    '650 mg' -> (650.0, 'unit'); '250 mg/5 ml' -> (50.0, 'ml'); unparseable -> None."""
    mg = parse_mg(strength)
    if mg is None:
        return None
    m = re.search(r"/\s*(\d+(?:\.\d+)?)?\s*ml(?![a-z])", strength or "", re.I)
    if m:
        vol = float(m.group(1) or 1)
        return (mg / vol, "ml") if vol > 0 else None
    return mg, "unit"


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_expiry(text: str | None) -> tuple[int, int] | None:
    """'EXP 03/2026', 'Exp. MAR 2026', '03/26', '2026-03', 'MAR.26' -> (2026, 3). Unparseable -> None."""
    if not text:
        return None
    s = text.lower()
    m = re.search(r"\b(20\d{2})[-/.](\d{1,2})\b", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"\b(\d{1,2})\s*[-/.]\s*(20\d{2}|\d{2})\b", s)
        if m:
            mo, y = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*[-/.,']?\s*(20\d{2}|\d{2})\b", s)
            if not m:
                return None
            mo, y = MONTHS[m.group(1)], int(m.group(2))
    if y < 100:
        y += 2000
    return (y, mo) if 1 <= mo <= 12 and 2000 <= y <= 2100 else None


def brand_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


@dataclass
class Recognition:
    canonical: str | None  # canonical ingredient name, or None if unrecognised
    method: str            # exact | synonym | salt-stripped | unrecognised
    suggestion: str | None = None  # fuzzy suggestion (never auto-applied)
    score: float = 0.0


class IngredientNormalizer:
    def __init__(self, ingredients: list[dict]):
        self.lookup: dict[str, tuple[str, str]] = {}
        for ing in ingredients:
            self.lookup[ing["name"]] = (ing["name"], "exact")
            for syn in ing.get("synonyms", []):
                self.lookup[clean(syn)] = (ing["name"], "synonym")
        self.keys = list(self.lookup)

    def recognise(self, raw: str) -> Recognition:
        c = clean(raw)
        if not c:
            return Recognition(None, "unrecognised")
        if c in self.lookup:
            return Recognition(*self.lookup[c])
        stripped = " ".join(t for t in c.split() if t not in SALTS)
        if stripped in self.lookup:
            return Recognition(self.lookup[stripped][0], "salt-stripped")
        best = process.extractOne(stripped or c, self.keys, scorer=fuzz.ratio)
        if best and best[1] >= 80:
            return Recognition(None, "unrecognised", self.lookup[best[0]][0], best[1])
        return Recognition(None, "unrecognised")


class BrandMatcher:
    def __init__(self, brands: list[dict]):
        self.brands = brands
        self.by_key = {brand_key(b["name"]): b for b in brands}
        self.names = [b["name"] for b in brands]

    def exact(self, name: str) -> dict | None:
        return self.by_key.get(brand_key(name))

    def candidates(self, name: str, limit: int = 5) -> list[dict]:
        if not name or not name.strip():
            return []
        hit = self.exact(name)
        out = [dict(hit, score=100.0)] if hit else []
        for nm, score, idx in process.extract(name, self.names, scorer=fuzz.WRatio, limit=limit + 1):
            b = self.brands[idx]
            if score >= 60 and (not hit or b["id"] != hit["id"]):
                out.append(dict(b, score=round(score, 1)))
        return out[:limit]
