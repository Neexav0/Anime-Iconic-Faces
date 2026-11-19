"""Curated, explainable rule engine. No LLM anywhere in this file.

Input: confirmed entries (plain dicts) + patient + KB.
Output: alerts (each with rule_id, sources, triggering entries, evidence) and
an explicit list of what was NOT assessed.

Entry dict shape:
  {"id": 1, "name": "Dolo 650", "brand_id": 3 | None,
   "ingredients":         [{"raw": "Paracetamol IP", "strength": "650 mg"}, ...],   # user-confirmed
   "printed_ingredients": [{"raw": ..., "strength": ...}] | None,                  # vision transcription, if any
   "per_day": 3 | None}       # units (tablets/capsules) or ml taken per day, as entered by the user

Patient dict: {"age": 72 | None, "weight_kg": 18.5 | None, "allergies": [...], "symptoms": [...]}
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date

from .kb import KB
from .matching import clean, parse_dose_strength, parse_expiry, parse_mg


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


def _join(names: list[str], lang: str) -> str:
    """'A, B and C' / 'A, B और C'."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + (" और " if lang == "hi" else " and ") + names[-1]


def evaluate(entries: list[dict], patient: dict, kb: KB, lang: str = "en", today: date | None = None) -> dict:
    today = today or date.today()
    resolved = {e["id"]: _resolve(e, kb) for e in entries}
    names = {e["id"]: e["name"] for e in entries}
    alerts: list[dict] = []

    def make_alert(rule, entry_ids, evidence, extra_sources=(), only_sources=None, **fmt):
        simple = rule.get("simple") or rule["message"]
        # Evidence lines are language-neutral, so the key is stable across languages and reloads.
        disc = hashlib.sha1("\n".join(evidence).encode()).hexdigest()[:8]
        alerts.append({
            "rule_id": rule["rule_id"],
            "key": f"{rule['rule_id']}|{','.join(map(str, sorted(set(entry_ids))))}|{disc}",
            "title": rule["title"].get(lang) or rule["title"]["en"],
            "message": _fmt(rule["message"].get(lang) or rule["message"]["en"], **fmt),
            "simple": _fmt(simple.get(lang) or simple["en"], **fmt),
            "question": _fmt((rule.get("question") or simple).get(lang) or (rule.get("question") or simple)["en"], **fmt),
            "entry_ids": sorted(set(entry_ids)),
            "entries": [names[i] for i in sorted(set(entry_ids))],
            "evidence": evidence,
            "sources": [kb.sources[s] for s in (rule["sources"] if only_sources is None else only_sources)
                        if s in kb.sources] + list(extra_sources),
        })

    # Index: ingredient key -> list of (entry_id, ingredient)
    by_key: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for eid, ings in resolved.items():
        for ing in ings:
            by_key[ing["key"]].append((eid, ing))

    # Daily totals per ingredient that has a label limit: sum over medicines of (amount per unit x units per day).
    limits = (kb.dose_limits or {}).get("limits", {})
    child_below = (kb.dose_limits or {}).get("child_below_age", 12)
    age, weight = patient.get("age"), patient.get("weight_kg")
    is_child = age is not None and age < child_below
    dose: dict[str, dict] = {}
    for e in entries:
        per_day = e.get("per_day")
        for ing in resolved[e["id"]]:
            c = ing["canonical"]
            if c not in limits:
                continue
            rec = dose.setdefault(c, {"mg": 0.0, "parts": [], "missing": []})
            ds = parse_dose_strength(ing["strength"])
            if not per_day or ds is None:
                rec["missing"].append(e["id"])
                continue
            mg_day = ds[0] * per_day
            rec["mg"] += mg_day
            unit = "ml" if ds[1] == "ml" else "units"
            rec["parts"].append((e["id"], f"{names[e['id']]}: {_num(per_day)} {unit} a day x {_num(ds[0])} mg = {_num(mg_day)} mg"))
    dose_unchecked: list[dict] = []

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
                           entries=_join([names[e] for e in sorted(eids)], lang))
                alerts[-1]["ingredient_key"] = key

        elif t == "class_multiple":
            hits = [(eid, ing) for eid, ings in resolved.items() for ing in ings if p["class"] in ing["classes"]]
            distinct = sorted({ing["canonical"] for _, ing in hits})
            if len(distinct) >= p.get("min_distinct", 2):
                evidence = [f"{names[eid]}: {kb.display(ing['canonical'])} {ing['strength']}".strip() for eid, ing in hits]
                make_alert(rule, [eid for eid, _ in hits], evidence,
                           items=_join([kb.display(c) for c in distinct], lang))

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

        elif t == "allergy_match":
            for raw in patient.get("allergies") or []:
                members, group = resolve_allergy(raw, kb)
                if not members:
                    continue
                label = (group.get(lang) or group["en"]) if group and clean(raw) == group["code"] else raw
                for eid, ings in resolved.items():
                    hit = [i for i in ings if i["canonical"] in members]
                    if not hit:
                        continue
                    direct = group is None or any(clean(raw) in {i["canonical"], clean(kb.display(i["canonical"]))} for i in hit)
                    gtxt = ""
                    if group and not direct:
                        gname = group.get(lang) or group["en"]
                        if clean(raw) == group["code"]:  # the allergy *is* the group: don't repeat its name
                            gtxt = " (जो इसी समूह की दवा है)" if lang == "hi" else " (which belongs to this group)"
                        else:
                            gtxt = f" (उसी समूह की दवा: {gname})" if lang == "hi" else f" (same group: {gname})"
                    extra = [kb.sources[group["source"]]] if group and group["source"] in kb.sources else []
                    make_alert(rule, [eid], [f"Reported allergy: {raw}"] +
                               [f"{names[eid]}: {kb.display(i['canonical'])} {i['strength']}".strip() for i in hit],
                               extra_sources=extra, allergy=label, entry=names[eid],
                               ingredient=_join([kb.display(i["canonical"]) for i in hit], lang), group=gtxt)

        elif t == "symptom_side_effect":
            for code in patient.get("symptoms") or []:
                if code not in kb.symptom_by_code:
                    continue
                hits = [(eid, i) for eid, ings in resolved.items() for i in ings
                        if i["canonical"] and code in kb.drug_info.get(i["canonical"], {}).get("side_effects", [])]
                if not hits:
                    continue
                seen, uniq = set(), []
                for eid, i in hits:
                    if (eid, i["canonical"]) not in seen:
                        seen.add((eid, i["canonical"]))
                        uniq.append((eid, i))
                make_alert(rule, [eid for eid, _ in uniq],
                           [f"Reported symptom: {kb.symptom_label(code)}"] +
                           [f"{names[eid]}: {kb.display(i['canonical'])} lists it as a side effect" for eid, i in uniq],
                           extra_sources=[kb.label_source(c) for c in sorted({i["canonical"] for _, i in uniq})],
                           symptom=kb.symptom_label(code, lang),
                           items=_join([f"{kb.display(i['canonical'])} ({names[eid]})" for eid, i in uniq], lang))

        elif t == "daily_dose_limit":
            if (p.get("group") == "child") != is_child:
                continue
            for c, rec in sorted(dose.items()):
                lim = limits[c]
                if is_child and age < lim.get("child_min_age", 0):
                    continue  # the label gives no dose at this age at all: R-DOSE-AGE covers it
                if is_child and "child_mg_per_kg_day" in lim:
                    if not weight:
                        involved = {i for i, _ in rec["parts"]} | set(rec["missing"])
                        dose_unchecked.append({"ingredient": kb.display(c), "reason": "weight",
                                               "entries": [names[i] for i in sorted(involved)]})
                        continue
                    limit = min(lim["child_mg_per_kg_day"] * weight, lim.get("child_cap_mg_day", float("inf")))
                    basis = f"{_num(weight)} किलो के बच्चे" if lang == "hi" else f"a child weighing {_num(weight)} kg"
                    rule_line = f"Child limit: {lim['child_mg_per_kg_day']} mg/kg/day x {_num(weight)} kg = {_num(limit)} mg/day"
                elif is_child:
                    limit = lim["child_mg_day"]
                    lo, hi_age = lim["child_min_age"], child_below - 1
                    basis = f"{lo} से {hi_age} साल के बच्चे" if lang == "hi" else f"a child aged {lo} to {hi_age}"
                    rule_line = f"Child limit (age {lo}-{hi_age}): {_num(limit)} mg/day"
                else:
                    limit = lim["adult_mg_day"]
                    basis = ""
                    if lim.get("adult_basis") == "otc":
                        basis = (" (बिना पर्चे वाली दवा का लेबल; डॉक्टर ने अलग मात्रा दी हो सकती है)" if lang == "hi"
                                 else " (over-the-counter label; your doctor may have prescribed a different amount)")
                    rule_line = f"Adult limit: {_num(limit)} mg/day"
                if rec["mg"] > limit:
                    ids = [i for i, _ in rec["parts"]]
                    make_alert(rule, ids, [line for _, line in rec["parts"]] + [f"Total: {_num(rec['mg'])} mg/day", rule_line],
                               only_sources=lim.get("sources", []), ingredient=kb.display(c), total=_num(rec["mg"]),
                               limit=_num(limit), basis=basis, entries=_join([names[i] for i in ids], lang))
                    alerts[-1]["ingredient_key"] = c
                elif rec["missing"]:
                    dose_unchecked.append({"ingredient": kb.display(c), "reason": "amount",
                                           "entries": [names[i] for i in rec["missing"]]})

        elif t == "child_min_age":
            if not is_child:
                continue
            for eid, ings in resolved.items():
                # One alert per medicine, naming every ingredient the label does not cover at this age.
                hits = [(ing, limits[ing["canonical"]]) for ing in ings
                        if ing["canonical"] in limits and age < limits[ing["canonical"]].get("child_min_age", 0)]
                if not hits:
                    continue
                min_age = max(lim["child_min_age"] for _, lim in hits)
                srcs = list(dict.fromkeys(sid for _, lim in hits for sid in lim.get("sources", [])))
                make_alert(rule, [eid], [f"{names[eid]}: {kb.display(i['canonical'])} {i['strength']}".strip() for i, _ in hits]
                           + [f"Child's age: {age}; label gives doses from age {min_age}"],
                           only_sources=srcs, entry=names[eid], min_age=min_age, age=age,
                           ingredient=_join([kb.display(i["canonical"]) for i, _ in hits], lang))

        elif t == "expired":
            for e in entries:
                exp = parse_expiry(e.get("expiry"))
                # A pack is usable through the end of its printed expiry month.
                if exp and (exp[0], exp[1]) < (today.year, today.month):
                    label = f"{exp[1]:02d}/{exp[0]}"
                    make_alert(rule, [e["id"]], [f"{e['name']}: printed expiry {e.get('expiry')}", f"Checked on {today.isoformat()}"],
                               entry=e["name"], expiry=label)

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

    # Danger signs are always surfaced, whatever the medicines.
    urgent = [kb.symptom_label(c, lang) for c in patient.get("symptoms") or []
              if kb.symptom_by_code.get(c, {}).get("red_flag")]
    flagged_symptoms = {a["evidence"][0].split(": ", 1)[1] for a in alerts if a["rule_id"] == "R-SFX-001"}
    # Danger signs never get the softer "tell your doctor if it continues" note: they only get the urgent banner.
    symptoms_unlinked = [kb.symptom_label(c, lang) for c in patient.get("symptoms") or []
                         if c in kb.symptom_by_code and not kb.symptom_by_code[c].get("red_flag")
                         and kb.symptom_label(c) not in flagged_symptoms]
    allergies_unmatched = [a for a in patient.get("allergies") or [] if not resolve_allergy(a, kb)[0]]

    return {"alerts": alerts, "not_assessed": not_assessed, "ingredient_view": ingredient_view,
            "urgent": urgent, "symptoms_unlinked": symptoms_unlinked, "allergies_unmatched": allergies_unmatched,
            "dose_unchecked": dose_unchecked, "is_child": is_child,
            "rules_checked": [r["rule_id"] for r in kb.rules]}


def _num(x: float) -> str:
    """1950.0 -> '1950', 2.5 -> '2.5'."""
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}".rstrip("0").rstrip(".")


def resolve_allergy(raw: str, kb: KB) -> tuple[set[str], dict | None]:
    """Allergy text -> (ingredient set, documented group or None). Unknown -> (set(), None)."""
    c = clean(raw)
    if not c:
        return set(), None
    for g in kb.allergy_groups:
        if c == g["code"] or c in {clean(s) for s in g["synonyms"]}:
            return set(g["members"]), g
    rec = kb.normalizer.recognise(raw)
    if rec.canonical:
        return {rec.canonical}, None
    b = kb.brand_matcher.exact(raw)
    if b:
        return {i["canonical"] for i in b["ingredients"]}, None
    return set(), None
