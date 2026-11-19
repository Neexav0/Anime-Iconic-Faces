"""Curated, explainable rule engine. No LLM anywhere in this file.

Input: confirmed entries (plain dicts) + patient + KB.
Output: alerts (each with rule_id, sources, triggering entries, evidence) and
an explicit list of what was NOT assessed.

Entry dict shape:
  {"id": 1, "name": "Dolo 650", "brand_id": 3 | None,
   "ingredients":         [{"raw": "Paracetamol IP", "strength": "650 mg"}, ...],   # user-confirmed
   "printed_ingredients": [{"raw": ..., "strength": ...}] | None}                   # vision transcription, if any
"""
from __future__ import annotations

from collections import defaultdict

from .kb import KB
from .matching import clean, parse_mg


def _resolve(entry: dict, kb: KB) -> list[dict]:
    """Attach canonical names + effective classes to each confirmed ingredient."""
    out = []
    for ing in entry.get("ingredients") or []:
        rec = kb.normalizer.recognise(ing.get("raw", ""))
        mg = parse_mg(ing.get("strength"))
        classes: set[str] = set()
        if rec.canonical:
            meta = kb.ingredients[rec.canonical]
            classes = set(meta.get("classes", []))
            for cc in meta.get("conditional_classes", []):
                # Unknown strength -> include the class (conservative: more review, not less).
                if mg is None or mg >= cc["min_mg"]:
                    classes.add(cc["class"])
        out.append({
            "raw": ing.get("raw", ""), "strength": ing.get("strength") or "",
            "mg": mg, "canonical": rec.canonical, "method": rec.method,
            "key": rec.canonical or f"?{clean(ing.get('raw', ''))}",
            "classes": classes,
        })
    return out


def _label(kb: KB, key: str) -> str:
    return kb.display(key) if not key.startswith("?") else key[1:].title()


def _fmt(template: str, **kw) -> str:
    return template.format(**kw)


def evaluate(entries: list[dict], patient: dict, kb: KB, lang: str = "en") -> dict:
    resolved = {e["id"]: _resolve(e, kb) for e in entries}
    names = {e["id"]: e["name"] for e in entries}
    alerts: list[dict] = []

    def make_alert(rule, entry_ids, evidence, **fmt):
        alerts.append({
            "rule_id": rule["rule_id"],
            "title": rule["title"].get(lang) or rule["title"]["en"],
            "message": _fmt(rule["message"].get(lang) or rule["message"]["en"], **fmt),
            "entry_ids": sorted(set(entry_ids)),
            "entries": [names[i] for i in sorted(set(entry_ids))],
            "evidence": evidence,
            "sources": [kb.sources[s] for s in rule["sources"] if s in kb.sources],
        })

    # Index: ingredient key -> list of (entry_id, ingredient)
    by_key: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for eid, ings in resolved.items():
        for ing in ings:
            by_key[ing["key"]].append((eid, ing))

    for rule in kb.rules:
        t, p = rule["type"], rule.get("params", {})

        if t == "duplicate_ingredient":
            for key, hits in sorted(by_key.items()):
                eids = {eid for eid, _ in hits}
                if len(eids) < 2:
                    continue
                evidence = [f"{names[eid]}: {ing['raw']} {ing['strength']}".strip() for eid, ing in hits]
                if key.startswith("?"):
                    evidence.append("Matched by exact printed name only (ingredient not in Medora's knowledge base).")
                make_alert(rule, eids, evidence, ingredient=_label(kb, key), count=len(eids),
                           entries=", ".join(names[e] for e in sorted(eids)))
                alerts[-1]["ingredient_key"] = key

        elif t == "class_multiple":
            hits = [(eid, ing) for eid, ings in resolved.items() for ing in ings if p["class"] in ing["classes"]]
            distinct = sorted({ing["canonical"] for _, ing in hits})
            if len(distinct) >= p.get("min_distinct", 2):
                evidence = [f"{names[eid]}: {kb.display(ing['canonical'])} {ing['strength']}".strip() for eid, ing in hits]
                make_alert(rule, [eid for eid, _ in hits], evidence,
                           items=", ".join(f"{kb.display(c)}" for c in distinct))

        elif t == "class_pair":
            b_classes = set(p["class_b"]) if isinstance(p["class_b"], list) else {p["class_b"]}
            a_hits = [(eid, ing) for eid, ings in resolved.items() for ing in ings if p["class_a"] in ing["classes"]]
            b_hits = [(eid, ing) for eid, ings in resolved.items() for ing in ings
                      if ing["classes"] & b_classes and p["class_a"] not in ing["classes"]]
            for a_eid, a in a_hits:
                partners = [(eid, ing) for eid, ing in b_hits]
                if not partners:
                    continue
                evidence = [f"{names[a_eid]}: {kb.display(a['canonical'])}"] + \
                           [f"{names[eid]}: {kb.display(ing['canonical'])} {ing['strength']}".strip() for eid, ing in partners]
                make_alert(rule, [a_eid] + [eid for eid, _ in partners], evidence,
                           a=f"{kb.display(a['canonical'])} ({names[a_eid]})",
                           b=", ".join(f"{kb.display(ing['canonical'])} ({names[eid]})" for eid, ing in partners))

        elif t == "elderly_listed":
            age = patient.get("age")
            if age is None or age < p["min_age"]:
                continue
            listed = set(p["ingredients"])
            for eid, ings in resolved.items():
                for ing in ings:
                    if ing["canonical"] in listed:
                        make_alert(rule, [eid], [f"{names[eid]}: {kb.display(ing['canonical'])} {ing['strength']}".strip(),
                                                 f"Patient age: {age}"],
                                   items=f"{kb.display(ing['canonical'])} ({names[eid]})")

        elif t == "composition_mismatch":
            for e in entries:
                brand = kb.brand_by_id.get(e.get("brand_id")) if e.get("brand_id") else None
                if not brand:
                    continue
                cat = {i["canonical"] for i in brand["ingredients"]}
                checks = [("confirmed", resolved[e["id"]])]
                if e.get("printed_ingredients"):
                    checks.append(("read from pack", _resolve({"ingredients": e["printed_ingredients"]}, kb)))
                for label, ings in checks:
                    got = {i["key"] for i in ings}
                    if ings and got != cat:
                        make_alert(rule, [e["id"]],
                                   [f"{label}: {', '.join(sorted(_label(kb, k) for k in got))}",
                                    f"catalogue ({brand['name']}): {', '.join(sorted(kb.display(c) for c in cat))}"],
                                   entry=e["name"],
                                   printed=", ".join(sorted(_label(kb, k) for k in got)),
                                   catalogue=", ".join(sorted(kb.display(c) for c in cat)))
                        break

    # What we could NOT assess -- always shown, never hidden.
    not_assessed = []
    for e in entries:
        ings = resolved[e["id"]]
        unknown = [i["raw"] for i in ings if not i["canonical"]]
        if not ings:
            not_assessed.append({"entry_id": e["id"], "entry": e["name"],
                                 "reason": "no_composition", "items": []})
        elif len(unknown) == len(ings):
            not_assessed.append({"entry_id": e["id"], "entry": e["name"],
                                 "reason": "all_unknown", "items": unknown})
        elif unknown:
            not_assessed.append({"entry_id": e["id"], "entry": e["name"],
                                 "reason": "some_unknown", "items": unknown})

    ingredient_view = []
    for key, hits in sorted(by_key.items(), key=lambda kv: _label(kb, kv[0])):
        ingredient_view.append({
            "ingredient": _label(kb, key), "recognised": not key.startswith("?"),
            "entries": [f"{names[eid]} ({ing['strength']})" if ing["strength"] else names[eid] for eid, ing in hits],
            "count": len({eid for eid, _ in hits}),
        })

    return {"alerts": alerts, "not_assessed": not_assessed, "ingredient_view": ingredient_view,
            "rules_checked": [r["rule_id"] for r in kb.rules]}
