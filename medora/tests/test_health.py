from app.kb import kb_from_json
from app.rules_engine import evaluate

KB = kb_from_json()


def E(i, name, *raws):
    return {"id": i, "name": name, "brand_id": None, "ingredients": [{"raw": r, "strength": ""} for r in raws]}


def rules(res):
    return sorted(a["rule_id"] for a in res["alerts"])


def test_every_ingredient_has_uses_and_valid_side_effect_codes():
    for canon in KB.ingredients:
        assert KB.uses(canon), canon
        for code in KB.drug_info[canon]["side_effects"]:
            assert code in KB.symptom_by_code, (canon, code)
    for g in KB.allergy_groups:
        assert all(m in KB.ingredients for m in g["members"]) and g["source"] in KB.sources


def test_penicillin_group_flags_augmentin_with_group_note():
    res = evaluate([E(1, "Augmentin 625 Duo", "Amoxicillin", "Potassium Clavulanate")], {"allergies": ["penicillin"]}, KB)
    a = [a for a in res["alerts"] if a["rule_id"] == "R-ALG-001"]
    assert len(a) == 1 and "belongs to this group" in a[0]["simple"] and "Penicillin" in a[0]["simple"]
    assert any(s["id"] == "DM-AMOX" for s in a[0]["sources"])


def test_aspirin_allergy_flags_other_nsaids_but_not_paracetamol():
    res = evaluate([E(1, "Brufen 400", "Ibuprofen"), E(2, "Dolo 650", "Paracetamol")], {"allergies": ["Aspirin"]}, KB)
    alg = [a for a in res["alerts"] if a["rule_id"] == "R-ALG-001"]
    assert [a["entries"] for a in alg] == [["Brufen 400"]]


def test_sulfa_allergy_flags_hctz_and_direct_and_brand_allergies():
    res = evaluate([E(1, "Telma-H", "Telmisartan", "Hydrochlorothiazide"), E(2, "Dolo 650", "Paracetamol")],
                   {"allergies": ["sulfa", "paracetamol", "Combiflam"]}, KB)
    alg = sorted(a["entries"][0] for a in res["alerts"] if a["rule_id"] == "R-ALG-001")
    # sulfa -> Telma-H ; paracetamol -> Dolo ; Combiflam (ibuprofen + paracetamol) -> Dolo again
    assert alg == ["Dolo 650", "Dolo 650", "Telma-H"]


def test_unknown_allergy_is_reported_not_silently_ignored():
    res = evaluate([E(1, "Dolo 650", "Paracetamol")], {"allergies": ["shellfish"]}, KB)
    assert rules(res) == [] and res["allergies_unmatched"] == ["shellfish"]


def test_symptom_maps_to_label_listed_side_effects_with_sources():
    res = evaluate([E(1, "Amlokind 5", "Amlodipine"), E(2, "Sinarest", "Paracetamol", "Chlorpheniramine")],
                   {"symptoms": ["ankle_swelling", "drowsiness", "constipation"]}, KB)
    sfx = {a["evidence"][0]: a for a in res["alerts"] if a["rule_id"] == "R-SFX-001"}
    assert set(sfx) == {"Reported symptom: Swollen ankles or feet", "Reported symptom: Drowsy or sleepy"}
    assert all(a["sources"] and "pharmacist" in a["simple"].lower() for a in sfx.values())
    assert res["symptoms_unlinked"] == ["Constipation"]


def test_danger_signs_always_surface():
    res = evaluate([], {"symptoms": ["breathing", "headache"]}, KB)
    assert res["urgent"] == ["Trouble breathing"]
    assert res["symptoms_unlinked"] == ["Headache"]  # danger signs never get the softer note
    res = evaluate([E(1, "Warf 5", "Warfarin")], {"symptoms": ["black_stool"]}, KB)
    assert res["urgent"] and rules(res) == ["R-SFX-001"]
