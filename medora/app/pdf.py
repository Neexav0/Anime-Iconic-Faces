"""Pharmacist-review summary PDF (English: the pharmacist-facing document)."""
from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#14302B")
TEAL = colors.HexColor("#0F6E5F")
AMBER = colors.HexColor("#B45309")
AMBER_BG = colors.HexColor("#FEF3E2")
MUTED = colors.HexColor("#5B6B67")
LINE = colors.HexColor("#D5DEDB")


def _esc(s) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_pdf(patient, entries, result, kb) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title="Medora - Pharmacist Review Summary", author="Medora")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=18, textColor=INK, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12, textColor=TEAL, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK)
    small = ParagraphStyle("s", parent=body, fontSize=8, leading=10.5, textColor=MUTED)
    bold = ParagraphStyle("bb", parent=body, fontName="Helvetica-Bold")

    story = [
        Paragraph("MEDORA &middot; Medication Safety Passport", h1),
        Paragraph("Pharmacist review summary &middot; generated " + datetime.now().strftime("%d %b %Y, %H:%M"), small),
        Spacer(1, 6),
    ]
    pt = Table([[Paragraph("<b>Patient</b>", body), Paragraph(_esc(patient.name) or "&mdash;", body),
                 Paragraph("<b>Age</b>", body), Paragraph(str(patient.age) if patient.age else "not given", body),
                 Paragraph("<b>Medicines</b>", body), Paragraph(str(len(entries)), body)]],
               colWidths=[20 * mm, 60 * mm, 12 * mm, 24 * mm, 22 * mm, 40 * mm], hAlign="LEFT")
    pt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, LINE), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F7F6")),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [pt]
    groups = {g["code"]: g["en"] for g in kb.allergy_groups}
    allergies = "not asked" if patient.allergies is None else (", ".join(groups.get(a, a) for a in patient.allergies) or "none known")
    symptoms = ", ".join(kb.symptom_label(c) for c in patient.symptoms or []) or "none reported"
    story += [Spacer(1, 4), Paragraph(f"<b>Reported allergies:</b> {_esc(allergies)} &nbsp;&nbsp; <b>Reported symptoms:</b> {_esc(symptoms)}", body)]
    if result.get("urgent"):
        story.append(Paragraph(f"<font color='#A52A2A'><b>Danger signs reported:</b> {_esc(', '.join(result['urgent']))}: advise urgent medical care.</font>", body))

    # 1. Confirmed medicines
    story.append(Paragraph("1. Confirmed medicines (user-confirmed against the pack)", h2))
    rows = [[Paragraph(f"<b>{h}</b>", small) for h in ("#", "Medicine", "Composition as confirmed", "Source / note", "Reviewed")]]
    for i, e in enumerate(entries, 1):
        comp = "<br/>".join(f"{_esc(x.get('raw'))} {_esc(x.get('strength'))}" for x in (e.ingredients or [])) or "<i>not entered</i>"
        origin = {"photo": "photo", "qr": "strip QR"}.get(e.origin, "typed")
        src = ("catalogue-matched" if e.brand_id else "not in catalogue") + f" &middot; {origin}"
        if e.expiry or e.batch:
            src += "<br/>" + " &middot; ".join(x for x in [f"exp {_esc(e.expiry)}" if e.expiry else "", f"batch {_esc(e.batch)}" if e.batch else ""] if x)
        if e.note:
            src += f"<br/>{_esc(e.note)}"
        rows.append([Paragraph(str(i), body), Paragraph(f"<b>{_esc(e.name)}</b><br/>{_esc(e.form)}", body),
                     Paragraph(comp, body), Paragraph(src, small), ""])
    t = Table(rows, colWidths=[7 * mm, 40 * mm, 62 * mm, 47 * mm, 22 * mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, LINE), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7F0EE")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)

    # 2. Flags for review
    story.append(Paragraph(f"2. Flags for professional review ({len(result['alerts'])})", h2))
    if not result["alerts"]:
        story.append(Paragraph("None of Medora's rules were triggered. This does not mean the list is safe.", body))
    for a in result["alerts"]:
        src = "; ".join(f"{_esc(s['id'])}: {_esc(s['title'])}" + (f" ({_esc(s['url'])})" if s.get("url") and s["url"].startswith("http") else "") for s in a["sources"])
        card = Table([[Paragraph(f"<b>{_esc(a['title'])}</b> &nbsp; <font color='#5B6B67' size='8'>{_esc(a['rule_id'])}</font>", bold)],
                      [Paragraph(_esc(a["message"]), body)],
                      [Paragraph("<b>Evidence:</b> " + " &middot; ".join(_esc(x) for x in a["evidence"]), small)],
                      [Paragraph("<b>Source:</b> " + src, small)],
                      [Paragraph("Pharmacist note: ______________________________________________ &nbsp; Reviewed [&nbsp;&nbsp;&nbsp;]", small)]],
                     colWidths=[178 * mm])
        card.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, AMBER), ("BACKGROUND", (0, 0), (-1, -1), AMBER_BG),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7)]))
        story += [KeepTogether([card, Spacer(1, 5)])]

    # 3. Ingredient view
    story.append(Paragraph("3. Ingredient-level view", h2))
    rows = [[Paragraph(f"<b>{h}</b>", small) for h in ("Active ingredient", "Found in", "Recognised")]]
    for iv in result["ingredient_view"]:
        rows.append([Paragraph(("<b>%s</b>" if iv["count"] > 1 else "%s") % _esc(iv["ingredient"]), body),
                     Paragraph(", ".join(_esc(x) for x in iv["entries"]), body),
                     Paragraph("yes" if iv["recognised"] else "<font color='#B45309'>no</font>", body)])
    t = Table(rows, colWidths=[50 * mm, 104 * mm, 24 * mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, LINE), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7F0EE"))]))
    story.append(t)

    # 4. Not assessed
    story.append(Paragraph(f"4. Not assessed ({len(result['not_assessed'])})", h2))
    if not result["not_assessed"]:
        story.append(Paragraph("All entered ingredients were recognised. Interactions beyond the rules listed below were not assessed.", body))
    reasons = {"all_unknown": "ingredients not in Medora's knowledge base", "some_unknown": "some ingredients not recognised",
               "no_composition": "no composition entered"}
    for n in result["not_assessed"]:
        story.append(Paragraph(f"&bull; <b>{_esc(n['entry'])}</b>: {reasons[n['reason']]}" +
                               (f" ({', '.join(_esc(x) for x in n['items'])})" if n["items"] else ""), body))

    # 5. Scope
    story.append(Paragraph("5. What Medora checked", h2))
    for r in kb.rules:
        story.append(Paragraph(f"&bull; <b>{_esc(r['rule_id'])}</b>: {_esc(r['scope'])}", small))
    story += [Spacer(1, 8), Paragraph(
        "Medora is a prototype that organises user-confirmed information for professional review. It does not diagnose, "
        "recommend doses, or advise starting, stopping or substituting any medicine. The absence of a flag does not mean a "
        "combination is safe. Rule set pending pharmacist review.", small)]
    doc.build(story)
    return buf.getvalue()
