import json
from pathlib import Path

import pytest

from app.evaluation import run_case
from app.kb import kb_from_json
from app.matching import parse_mg
from app.rules_engine import evaluate

KB = kb_from_json()
CASES = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_labelled_case(case):
    r = run_case(case, KB)
    assert r["pass"], r


def test_every_alert_is_traceable():
    for case in CASES:
        from app.evaluation import case_to_entries
        res = evaluate(case_to_entries(case), {"age": case["age"]}, KB)
        for a in res["alerts"]:
            assert a["rule_id"] and a["sources"] and a["entries"] and a["evidence"]
            assert "pharmacist" in a["message"].lower()
            for banned in ("stop taking", "safe to", "increase the dose", "reduce the dose"):
                assert banned not in a["message"].lower()


def test_composition_mismatch_flags_pack_vs_catalogue():
    dolo = next(b for b in KB.brands if b["name"] == "Dolo 650")
    entries = [{"id": 1, "name": "Dolo 650", "brand_id": dolo["id"],
                "ingredients": [{"raw": "Paracetamol", "strength": "650 mg"}],
                "printed_ingredients": [{"raw": "Ibuprofen", "strength": "400 mg"}]}]
    res = evaluate(entries, {"age": 40}, KB)
    assert [a["rule_id"] for a in res["alerts"]] == ["R-XCHK-001"]


def test_matching_pack_and_catalogue_gives_no_mismatch():
    tel = next(b for b in KB.brands if b["name"] == "Telma-H")
    entries = [{"id": 1, "name": "Telma-H", "brand_id": tel["id"],
                "ingredients": [{"raw": "Telmisartan IP", "strength": "40 mg"}, {"raw": "Hydrochlorothiazide IP", "strength": "12.5 mg"}],
                "printed_ingredients": [{"raw": "TELMISARTAN I.P.", "strength": "40mg"}, {"raw": "Hydrochlorothiazide I.P.", "strength": "12.5mg"}]}]
    assert evaluate(entries, {"age": 70}, KB)["alerts"] == []


def test_fuzzy_is_suggestion_only():
    rec = KB.normalizer.recognise("Paracetamoll")
    assert rec.canonical is None and rec.suggestion == "paracetamol"


@pytest.mark.parametrize("s,mg", [("650 mg", 650), ("50 mcg", 0.05), ("1 g", 1000), ("250 IU", None), ("", None)])
def test_parse_mg(s, mg):
    assert parse_mg(s) == mg


def test_catalogue_ingredients_all_known():
    for b in KB.brands:
        for i in b["ingredients"]:
            assert i["canonical"] in KB.ingredients, (b["name"], i)
