"""Knowledge base: ingredients, brand catalogue, rules and sources.

At runtime the catalogue is loaded from the database (MySQL); tests build the
same structure straight from the JSON seed files.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .matching import BrandMatcher, IngredientNormalizer

DATA_DIR = Path(__file__).resolve().parent / "data"


def load_json(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@dataclass
class KB:
    ingredients: dict[str, dict]          # canonical -> {display, classes, conditional_classes}
    brands: list[dict]                    # [{id, name, manufacturer, form, ingredients:[{canonical, strength}]}]
    rules: list[dict]
    sources: dict[str, dict]
    drug_info: dict[str, dict] = field(default_factory=lambda: {k: v for k, v in load_json("drug_info.json").items() if not k.startswith("_")})
    symptoms: list[dict] = field(default_factory=lambda: load_json("symptoms.json"))
    allergy_groups: list[dict] = field(default_factory=lambda: load_json("allergy_groups.json"))
    normalizer: IngredientNormalizer = field(init=False)
    brand_matcher: BrandMatcher = field(init=False)

    def __post_init__(self):
        self.normalizer = IngredientNormalizer(list(self.ingredients.values()))
        self.brand_matcher = BrandMatcher(self.brands)
        self.brand_by_id = {b["id"]: b for b in self.brands}
        self.symptom_by_code = {s["code"]: s for s in self.symptoms}
        self.group_by_code = {g["code"]: g for g in self.allergy_groups}

    def display(self, canonical: str) -> str:
        ing = self.ingredients.get(canonical)
        return ing["display"] if ing else canonical

    def uses(self, canonical: str | None, lang: str = "en") -> str | None:
        info = self.drug_info.get(canonical or "")
        return (info["uses"].get(lang) or info["uses"]["en"]) if info else None

    def symptom_label(self, code: str, lang: str = "en") -> str:
        s = self.symptom_by_code.get(code)
        return (s.get(lang) or s["en"]) if s else code

    def label_source(self, canonical: str) -> dict:
        """Per-ingredient product-label source (DailyMed search link)."""
        return {"id": f"DM-{canonical.upper().replace(' ', '-')}",
                "title": f"{self.display(canonical)} product label: indications and adverse reactions",
                "publisher": "DailyMed, U.S. National Library of Medicine",
                "url": "https://dailymed.nlm.nih.gov/dailymed/search.cfm?labeltype=all&query=" + canonical.replace(" ", "+")}


def build_kb(ingredients: list[dict], brands: list[dict], rules: list[dict] | None = None,
             sources: list[dict] | None = None) -> KB:
    return KB(
        ingredients={i["name"]: i for i in ingredients},
        brands=brands,
        rules=rules if rules is not None else load_json("rules.json"),
        sources={s["id"]: s for s in (sources if sources is not None else load_json("sources.json"))},
    )


def kb_from_json() -> KB:
    """Build the KB from seed files only (used by tests and eval)."""
    brands = [
        {"id": i + 1, "name": b["name"], "manufacturer": b["manufacturer"], "form": b["form"],
         "ingredients": [{"canonical": c, "strength": s} for c, s in b["ingredients"]]}
        for i, b in enumerate(load_json("catalogue.json"))
    ]
    return build_kb(load_json("ingredients.json"), brands)
