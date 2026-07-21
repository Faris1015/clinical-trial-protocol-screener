# Sample protocol PDFs

Test fixtures for manually exercising the screener. Upload them via the frontend
(or `POST /api/screenings`). Regenerate with `python scripts/make_sample_pdfs.py`.

| File | What it exercises | Expected behavior |
|---|---|---|
| `01_nsclc_osimertinib.pdf` | Clean oncology protocol with one deliberately vague line ("adequate renal function") | Router admits → Parser extracts → **Critic (RENAL-001) rejects the first parse** and forces a corrected extraction with a numeric eGFR threshold → human gate → matcher |
| `02_diabetes_cardio.pdf` | Metabolic/cardio protocol using `hba1c`, `bmi`, `systolic_bp`, `egfr` (incl. a `between` age range) | Clean parse, different attribute mix and match profile against the synthetic EHR |
| `03_long_protocol.pdf` | ~5-page protocol with the eligibility section buried in the middle behind filler sections | `pdf.py` page-scoring locates the eligibility pages; still admits and parses |
| `04_not_a_protocol.pdf` | Meeting minutes — no eligibility section | **Router rejects** ("Input does not appear to contain an eligibility section") |
| `05_clean_pass.pdf` | Fully compliant protocol: every lab threshold numeric and in-range, one explicit lower age bound, no vague organ-function language, no childbearing/pregnancy keywords | **Passes end-to-end** on the default model → Patient Matcher. Clears the deterministic Critic (zero findings, no rule fires) and the LLM semantic pass (non-blocking warns only). See the note below. |

All content is synthetic and simplified — not medical or regulatory guidance.

## Note on `05_clean_pass.pdf` and the LLM semantic layer

`05_clean_pass.pdf` is engineered to pass **Layer 1** of the Regulatory Critic
(the deterministic rule engine in [`compliance_rules.yaml`](../backend/app/rules/compliance_rules.yaml)):
verified to produce **0 findings** — no rule fires, given a faithful parse.

**Layer 2** (the LLM semantic review) is a separate, non-deterministic gate whose
behavior depends on the configured model:

- **`qwen2.5:7b`** (the default) — clears the layer: verified **0 blocking
  findings** across repeated runs (any findings are non-blocking `warn`s), so the
  protocol passes end-to-end to the Patient Matcher.
- **`LLM_PROVIDER=stub`** — semantic review returns no findings; also passes.
- **`LLM_PROVIDER=anthropic`** (or another capable model) — passes, because the
  reviewer correctly finds no contradictions.
- **`llama3.1:8b`** (the earlier default) — **do not expect a pass**. This weaker
  model **hallucinates false-positive `reject` findings on almost any input**,
  including an empty extraction: it has been observed to flag the standard eGFR
  unit `mL/min/1.73m2` as a "unit mismatch" and to invent contradictory age bounds
  that are not present. Under it, no PDF reliably clears the semantic layer — the
  blocker is the reviewer model, not the protocol. This is the concrete reason the
  default moved off `llama3.1:8b`.
