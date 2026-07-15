# Sample protocol PDFs

Test fixtures for manually exercising the screener. Upload them via the frontend
(or `POST /api/screenings`). Regenerate with `python scripts/make_sample_pdfs.py`.

| File | What it exercises | Expected behavior |
|---|---|---|
| `01_nsclc_osimertinib.pdf` | Clean oncology protocol with one deliberately vague line ("adequate renal function") | Router admits → Parser extracts → **Critic (RENAL-001) rejects the first parse** and forces a corrected extraction with a numeric eGFR threshold → human gate → matcher |
| `02_diabetes_cardio.pdf` | Metabolic/cardio protocol using `hba1c`, `bmi`, `systolic_bp`, `egfr` (incl. a `between` age range) | Clean parse, different attribute mix and match profile against the synthetic EHR |
| `03_long_protocol.pdf` | ~5-page protocol with the eligibility section buried in the middle behind filler sections | `pdf.py` page-scoring locates the eligibility pages; still admits and parses |
| `04_not_a_protocol.pdf` | Meeting minutes — no eligibility section | **Router rejects** ("Input does not appear to contain an eligibility section") |

All content is synthetic and simplified — not medical or regulatory guidance.
