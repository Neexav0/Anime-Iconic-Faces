"""Create tables, seed the catalogue into the DB (idempotent) and build the runtime KB from the DB."""
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import selectinload

from .db import Base, SessionLocal, engine
from .kb import KB, build_kb, load_json
from .matching import parse_mg
from .models import Brand, BrandIngredient, Ingredient


def _add_missing_columns() -> None:
    """Tiny forward-only migration for columns added after the first deploy."""
    wanted = {"entries": {"expiry": "VARCHAR(40)", "batch": "VARCHAR(60)", "per_day": "FLOAT"},
              "patients": {"allergies": "JSON", "symptoms": "JSON", "account_id": "INTEGER", "weight_kg": "FLOAT"},
              "drafts": {"patient_id": "INTEGER"},
              "accounts": {"recovery_hash": "VARCHAR(255)"},
              "reviews": {"expires_at": "DATETIME"}}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in cols.items():
                if col not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl} NULL"))


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()
    with SessionLocal() as s:
        ing_by_name = {i.name: i for i in s.scalars(select(Ingredient))}
        for data in load_json("ingredients.json"):
            ing = ing_by_name.get(data["name"])
            if not ing:
                ing = Ingredient(name=data["name"])
                s.add(ing)
                ing_by_name[data["name"]] = ing
            ing.display = data["display"]
            ing.classes = data.get("classes", [])
            ing.conditional_classes = data.get("conditional_classes", [])
            ing.synonyms = data.get("synonyms", [])
        s.flush()
        existing = {b.name for b in s.scalars(select(Brand))}
        for b in load_json("catalogue.json"):
            if b["name"] in existing:
                continue
            brand = Brand(name=b["name"], manufacturer=b["manufacturer"], form=b["form"])
            for canon, strength in b["ingredients"]:
                brand.ingredients.append(BrandIngredient(ingredient=ing_by_name[canon], strength=strength,
                                                         strength_mg=parse_mg(strength)))
            s.add(brand)
        s.commit()


def load_kb() -> KB:
    with SessionLocal() as s:
        ingredients = [{"name": i.name, "display": i.display, "classes": i.classes or [],
                        "conditional_classes": i.conditional_classes or [], "synonyms": i.synonyms or []}
                       for i in s.scalars(select(Ingredient))]
        brands = [{"id": b.id, "name": b.name, "manufacturer": b.manufacturer, "form": b.form,
                   "ingredients": [{"canonical": bi.ingredient.name, "strength": bi.strength} for bi in b.ingredients]}
                  for b in s.scalars(select(Brand).options(selectinload(Brand.ingredients).selectinload(BrandIngredient.ingredient)).order_by(Brand.name))]
    return build_kb(ingredients, brands)
