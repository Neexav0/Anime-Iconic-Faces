# Medora: AI Medication Safety Passport

You photograph a medicine strip, then confirm every field yourself. Medora maps the brand to its active ingredients, flags duplicates and a few sourced warnings, and prepares a summary for your pharmacist.

## Run

```powershell
cd medora
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Then open http://localhost:8000. Settings live in `.env` (see `.env.example`):
- `GEMINI_API_KEY`: needed for photo reading. Without it, the photo path falls back to manual entry.
- `DATABASE_URL`: points at MySQL. Leave it empty and the app uses SQLite.

## Test

```powershell
.venv\Scripts\python -m pytest -q tests
```

The same labelled cases also render live at `/eval`.

## Design guarantees

- **The vision model only transcribes.** The schema has no field for opinions, and the prompt forbids filling in ingredients from memory.
- **Nothing is stored before you confirm it.** The rule engine only ever sees confirmed entries.
- **Rules are data.** They live in `app/data/rules.json`, each with fixed English and Hindi messages and cited sources. No LLM writes or decides any alert.
- **Recognition is deterministic.** An ingredient counts only by exact name, listed synonym or salt-stripping. Fuzzy matches are suggestions you must accept.
- **Pack and catalogue are cross-checked.** If the composition read from the pack differs from the catalogue, a review flag appears (R-XCHK-001).
- **Unknowns are shown, never hidden.** Anything Medora can't check appears under "Not assessed".
- **Photos are never written to disk.** They stay in memory until you confirm or discard them.

## Layout

| File | Purpose |
|---|---|
| `app/rules_engine.py` | Pure rule engine (no DB, no LLM) |
| `app/matching.py` | Name normalisation and catalogue matching |
| `app/vision.py` | Gemini transcription with schema lock and cache fallback |
| `app/data/*.json` | Ingredients, catalogue, rules, sources (seeded into the DB) |
| `app/pdf.py` | Pharmacist summary PDF |
| `tests/cases.json` | Labelled synthetic test set |
