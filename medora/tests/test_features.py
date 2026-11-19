from datetime import date

import pytest

from app.kb import kb_from_json
from app.matching import parse_expiry
from app.rules_engine import evaluate
from app.strip_qr import parse_strip_qr

KB = kb_from_json()


@pytest.mark.parametrize("s,exp", [
    ("EXP 03/2026", (2026, 3)), ("Exp. MAR 2026", (2026, 3)), ("03/26", (2026, 3)), ("2026-03", (2026, 3)),
    ("EXP.DEC.27", (2027, 12)), ("Use before 11/2025", (2025, 11)), ("", None), ("13/2026", None), ("soon", None),
])
def test_parse_expiry(s, exp):
    assert parse_expiry(s) == exp


def _entry(i, name, raw, expiry=None):
    return {"id": i, "name": name, "brand_id": None, "expiry": expiry, "ingredients": [{"raw": raw, "strength": ""}]}


def test_expired_pack_flagged_through_end_of_month():
    today = date(2026, 11, 19)
    res = evaluate([_entry(1, "Old strip", "Pantoprazole", "EXP 10/2026"), _entry(2, "This month", "Omeprazole", "11/2026"),
                    _entry(3, "No date", "Atorvastatin")], {"age": 70}, KB, today=today)
    assert [(a["rule_id"], a["entries"]) for a in res["alerts"]] == [("R-EXP-001", ["Old strip"])]


def test_alerts_have_plain_text_and_stable_keys():
    entries = [_entry(1, "Dolo 650", "Paracetamol"), _entry(2, "Sinarest", "Paracetamol")]
    en = evaluate(entries, {"age": 70}, KB, lang="en")["alerts"][0]
    hi = evaluate(entries, {"age": 70}, KB, lang="hi")["alerts"][0]
    assert en["key"] == hi["key"]
    assert "pharmacist" in en["simple"].lower() and "फ़ार्मासिस्ट" in hi["simple"]


def test_strip_qr_key_value_text():
    q = parse_strip_qr("Brand Name: DOLO 650\nGeneric Name: Paracetamol Tablets IP 650 mg\nManufacturer: Micro Labs Ltd\n"
                       "Batch No: DOBS4521\nMfg Date: 01/2025\nExp Date: 12/2027")
    assert q["brand_as_printed"] == "DOLO 650"
    assert q["composition"] == [{"ingredient_as_printed": "Paracetamol", "strength_as_printed": "650 mg", "legibility": "clear"}]
    assert q["batch_as_printed"] == "DOBS4521" and q["expiry_as_printed"] == "12/2027"


def test_strip_qr_combination_and_junk():
    q = parse_strip_qr("Brand: Combiflam; Composition: Ibuprofen 400 mg + Paracetamol 325 mg; Exp: 06/2025")
    assert [c["ingredient_as_printed"] for c in q["composition"]] == ["Ibuprofen", "Paracetamol"]
    assert parse_strip_qr("https://example.com/verify?id=123") is None
    assert parse_strip_qr("") is None
