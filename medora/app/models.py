from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Ingredient(Base):
    __tablename__ = "ingredients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display: Mapped[str] = mapped_column(String(120))
    classes: Mapped[list] = mapped_column(JSON, default=list)
    conditional_classes: Mapped[list] = mapped_column(JSON, default=list)
    synonyms: Mapped[list] = mapped_column(JSON, default=list)


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    manufacturer: Mapped[str] = mapped_column(String(120), default="")
    form: Mapped[str] = mapped_column(String(40), default="")
    verified_from: Mapped[str] = mapped_column(String(255), default="printed pack composition")
    ingredients: Mapped[list["BrandIngredient"]] = relationship(back_populates="brand", cascade="all, delete-orphan")


class BrandIngredient(Base):
    __tablename__ = "brand_ingredients"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id"))
    strength: Mapped[str] = mapped_column(String(40), default="")
    strength_mg: Mapped[float | None] = mapped_column(Float, nullable=True)
    brand: Mapped[Brand] = relationship(back_populates="ingredients")
    ingredient: Mapped[Ingredient] = relationship()


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allergies: Mapped[list | None] = mapped_column(JSON, nullable=True)  # group codes or free text
    symptoms: Mapped[list | None] = mapped_column(JSON, nullable=True)   # codes from symptoms.json
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def as_engine_dict(self) -> dict:
        return {"age": self.age, "allergies": self.allergies or [], "symptoms": self.symptoms or []}


class Draft(Base):
    """An unconfirmed extraction. Never used by the rule engine."""
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    origin: Mapped[str] = mapped_column(String(20))  # photo | manual
    extracted: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Entry(Base):
    """A user-confirmed medicine. The only thing the rule engine ever sees."""
    __tablename__ = "entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"), nullable=True)
    form: Mapped[str] = mapped_column(String(40), default="")
    origin: Mapped[str] = mapped_column(String(20), default="manual")
    note: Mapped[str] = mapped_column(String(255), default="")
    ingredients: Mapped[list] = mapped_column(JSON, default=list)
    printed_ingredients: Mapped[list | None] = mapped_column(JSON, nullable=True)
    expiry: Mapped[str | None] = mapped_column(String(40), nullable=True)
    batch: Mapped[str | None] = mapped_column(String(60), nullable=True)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def as_engine_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "brand_id": self.brand_id, "expiry": self.expiry,
                "ingredients": self.ingredients or [], "printed_ingredients": self.printed_ingredients}


class Review(Base):
    """A pharmacist review session opened by scanning the patient's QR code."""
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String(120), default="")
    items: Mapped[dict] = mapped_column(JSON, default=dict)  # alert key -> {"done": bool, "note": str}
    general_note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
