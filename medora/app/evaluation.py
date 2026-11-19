"""Runs the labelled synthetic test set through the rule engine and scores it.

Measures implementation correctness against labelled cases (including tricky
spellings, salts, synonyms, low-dose exclusions and unknowns). It is NOT a
clinical validation.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .kb import KB
from .rules_engine import evaluate

CASES_PATH = Path(__file__).resolve().parent.parent / "tests" / "cases.json"


def case_to_entries(case: dict) -> list[dict]:
    return [{"id": i + 1, "name": name, "brand_id": None,
             "ingredients": [{"raw": r, "strength": s} for r, s in ings]}
            for i, (name, ings) in enumerate(case["entries"])]


def run_case(case: dict, kb: KB) -> dict:
    res = evaluate(case_to_entries(case), {"age": case.get("age")}, kb)
    fired = sorted({a["rule_id"] for a in res["alerts"]})
    dups = sorted({a["ingredient_key"] for a in res["alerts"] if a["rule_id"] == "R-DUP-001"})
    na = sorted(n["entry"] for n in res["not_assessed"])
    exp = case["expect"]
    ok = fired == sorted(exp["rules"]) and dups == sorted(exp["duplicates"]) and na == sorted(exp["not_assessed"])
    return {"id": case["id"], "desc": case["desc"], "fired": fired, "expected": sorted(exp["rules"]),
            "dups": dups, "expected_dups": sorted(exp["duplicates"]), "not_assessed": na,
            "expected_na": sorted(exp["not_assessed"]), "pass": ok}


def run_all(kb: KB) -> dict:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    results = [run_case(c, kb) for c in cases]
    tp, fp, fn = Counter(), Counter(), Counter()
    for r in results:
        f, e = set(r["fired"]), set(r["expected"])
        for rid in f & e: tp[rid] += 1
        for rid in f - e: fp[rid] += 1
        for rid in e - f: fn[rid] += 1
    per_rule = {}
    for rid in sorted(set(tp) | set(fp) | set(fn)):
        p = tp[rid] / (tp[rid] + fp[rid]) if tp[rid] + fp[rid] else None
        rc = tp[rid] / (tp[rid] + fn[rid]) if tp[rid] + fn[rid] else None
        per_rule[rid] = {"tp": tp[rid], "fp": fp[rid], "fn": fn[rid], "precision": p, "recall": rc}
    na_expected = sum(len(r["expected_na"]) for r in results)
    na_correct = sum(len(set(r["not_assessed"]) & set(r["expected_na"])) for r in results)
    return {"cases": results, "passed": sum(r["pass"] for r in results), "total": len(results),
            "per_rule": per_rule,
            "not_assessed": {"expected": na_expected, "correct": na_correct}}
