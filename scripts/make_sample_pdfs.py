"""Generate sample protocol PDFs for manually testing the screener.

Each PDF targets a different code path in the pipeline:

  01_nsclc_osimertinib.pdf   Clean oncology protocol. Includes a deliberately
                             vague "adequate renal function" line so the Critic
                             (RENAL-001) rejects the first parse and forces a
                             corrected extraction — exercises the self-correcting
                             Parser<->Critic loop.
  02_diabetes_cardio.pdf     Clean metabolic/cardio protocol using hba1c, bmi,
                             systolic_bp — different attribute vocabulary and a
                             different match profile against the synthetic EHR.
  03_long_protocol.pdf       ~14-page protocol with the eligibility section buried
                             in the middle. Exercises pdf.py's page-scoring window
                             (it should still find and extract just the right pages).
  04_not_a_protocol.pdf      A meeting-minutes document with no eligibility section.
                             Should be rejected by the Router.

Run:  python scripts/make_sample_pdfs.py
Output: sample_pdfs/*.pdf
"""

import pathlib

import pymupdf

OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "sample_pdfs"

MARGIN = 56  # 0.78"
PAGE_W, PAGE_H = pymupdf.paper_size("letter")
FONT = "helv"
BODY_SIZE = 11
LEADING = BODY_SIZE * 1.45


def render(blocks: list[tuple[str, str]]) -> pymupdf.Document:
    """Flow (style, text) blocks across pages.

    style is one of: 'title', 'h1', 'h2', 'body', 'gap'.
    """
    sizes = {"title": 17, "h1": 14, "h2": 12, "body": BODY_SIZE, "gap": BODY_SIZE}
    bold = {"title", "h1", "h2"}

    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    y = MARGIN
    width = PAGE_W - 2 * MARGIN

    def new_page() -> None:
        nonlocal page, y
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        y = MARGIN

    for style, text in blocks:
        size = sizes[style]
        lead = size * 1.5 if style in bold else LEADING
        fontname = "hebo" if style in bold else FONT

        if style == "gap":
            y += lead
            continue

        # Estimate the box height this text needs, growing until it all fits.
        # insert_textbox returns a negative number when the text overflows.
        remaining = text
        while remaining:
            if y > PAGE_H - MARGIN - lead:
                new_page()
            box = pymupdf.Rect(MARGIN, y, MARGIN + width, PAGE_H - MARGIN)
            rc = page.insert_textbox(
                box, remaining, fontsize=size, fontname=fontname, lineheight=1.5
            )
            if rc >= 0:
                # Everything fit. Advance y by the number of lines drawn.
                lines = _count_lines(remaining, size, width)
                y += lines * lead + lead * 0.35
                remaining = ""
            else:
                # Overflowed: fill this page, continue on the next.
                new_page()
    return doc


def _count_lines(text: str, size: float, width: float) -> int:
    # Rough wrap estimate using average glyph width for Helvetica.
    char_w = size * 0.5
    per_line = max(1, int(width / char_w))
    total = 0
    for para in text.split("\n"):
        total += max(1, -(-len(para) // per_line))
    return total


# --- Content -----------------------------------------------------------------

NSCLC = [
    ("title", "Protocol EGFR-2024-017"),
    ("h1", "A Phase II Study of Osimertinib in EGFR-Mutated NSCLC"),
    ("gap", ""),
    ("h1", "Section 5. Study Population"),
    ("h2", "5.1 Inclusion Criteria"),
    ("body", "Patients must meet all of the following to be eligible:"),
    ("body",
     "1. Age >= 18 years at the time of consent.\n"
     "2. Histologically confirmed non-small cell lung cancer, stage IV.\n"
     "3. Documented EGFR exon 19 deletion or L858R mutation.\n"
     "4. ECOG performance status <= 1.\n"
     "5. Adequate renal function.\n"
     "6. Left ventricular ejection fraction >= 50%."),
    ("gap", ""),
    ("h2", "5.2 Exclusion Criteria"),
    ("body", "Patients are excluded if any of the following apply:"),
    ("body",
     "1. Prior EGFR TKI therapy.\n"
     "2. Systolic blood pressure > 160 mmHg despite antihypertensive treatment.\n"
     "3. Absolute neutrophil count < 1.5 x 10^9/L.\n"
     "4. Women of childbearing potential who are pregnant or breastfeeding."),
    ("gap", ""),
    ("body",
     "Note: inclusion criterion #5 is intentionally vague. The Critic rule "
     "RENAL-001 should reject the first parse and force a corrected extraction "
     "with a numeric eGFR threshold."),
]

DIABETES = [
    ("title", "Protocol DM-CARDIO-2025-004"),
    ("h1", "A Phase III Study of a GLP-1 Agonist in Type 2 Diabetes with Cardiovascular Risk"),
    ("gap", ""),
    ("h1", "Section 6. Eligibility"),
    ("h2", "6.1 Inclusion Criteria"),
    ("body", "Subjects must satisfy every criterion below:"),
    ("body",
     "1. Age between 40 and 75 years.\n"
     "2. Diagnosis of type 2 diabetes mellitus.\n"
     "3. HbA1c >= 7.0% and <= 10.0% at screening.\n"
     "4. Body mass index >= 27 kg/m2.\n"
     "5. Estimated glomerular filtration rate >= 45 mL/min/1.73m2.\n"
     "6. ECOG performance status <= 2."),
    ("gap", ""),
    ("h2", "6.2 Exclusion Criteria"),
    ("body", "Subjects are excluded for any of the following:"),
    ("body",
     "1. Systolic blood pressure >= 180 mmHg at screening.\n"
     "2. Prior treatment with any GLP-1 receptor agonist.\n"
     "3. Platelet count < 100 x 10^9/L.\n"
     "4. Women of childbearing potential who are pregnant or breastfeeding."),
]


def long_protocol() -> list[tuple[str, str]]:
    filler = (
        "This section is provided for background and does not contain eligibility "
        "criteria. It describes the scientific rationale, dosing schedule, "
        "pharmacokinetic sampling, statistical analysis plan, and administrative "
        "procedures in the level of detail typical of a full trial protocol. "
    ) * 6
    blocks: list[tuple[str, str]] = [
        ("title", "Protocol ONC-2025-101"),
        ("h1", "A Randomized Phase III Study of Investigational Agent XR-9 in Advanced Solid Tumors"),
        ("gap", ""),
    ]
    lead_sections = [
        "Section 1. Background and Rationale",
        "Section 2. Study Objectives and Endpoints",
        "Section 3. Study Design",
        "Section 4. Investigational Product and Dosing",
    ]
    for s in lead_sections:
        blocks += [("h1", s), ("body", filler), ("gap", "")]

    # The real eligibility section, buried in the middle.
    blocks += [
        ("h1", "Section 5. Study Population and Eligibility"),
        ("h2", "5.1 Inclusion Criteria"),
        ("body", "To be eligible, patients must meet all of the following:"),
        ("body",
         "1. Age >= 18 years.\n"
         "2. Histologically or cytologically confirmed advanced solid tumor.\n"
         "3. ECOG performance status <= 1.\n"
         "4. Estimated glomerular filtration rate >= 60 mL/min/1.73m2.\n"
         "5. Absolute neutrophil count >= 1.5 x 10^9/L.\n"
         "6. Platelet count >= 100 x 10^9/L.\n"
         "7. Left ventricular ejection fraction >= 45%."),
        ("gap", ""),
        ("h2", "5.2 Exclusion Criteria"),
        ("body", "Patients meeting any of the following are excluded:"),
        ("body",
         "1. Prior platinum chemotherapy within 6 months.\n"
         "2. Systolic blood pressure > 170 mmHg.\n"
         "3. Diastolic blood pressure > 110 mmHg.\n"
         "4. Women of childbearing potential who are pregnant or breastfeeding."),
        ("gap", ""),
    ]

    trailing_sections = [
        "Section 6. Statistical Considerations",
        "Section 7. Safety Monitoring and Adverse Event Reporting",
        "Section 8. Ethics, Consent, and Regulatory",
        "Section 9. Data Management",
    ]
    for s in trailing_sections:
        blocks += [("h1", s), ("body", filler), ("gap", "")]
    return blocks


NOT_A_PROTOCOL = [
    ("title", "Site Operations Committee — Meeting Minutes"),
    ("h1", "Quarterly Review, Q2 2025"),
    ("gap", ""),
    ("h2", "Attendees"),
    ("body", "R. Okafor (chair), L. Martins, S. Petrov, and the site coordination team."),
    ("gap", ""),
    ("h2", "Agenda"),
    ("body",
     "1. Freezer maintenance schedule and temperature logging.\n"
     "2. Courier turnaround times for lab samples.\n"
     "3. Budget reconciliation for the last quarter.\n"
     "4. Staffing plan for the upcoming monitoring visit."),
    ("gap", ""),
    ("h2", "Discussion"),
    ("body",
     "The committee reviewed vendor performance and agreed to renew the courier "
     "contract. Facilities will replace the backup freezer gasket before the next "
     "audit. No decisions were recorded regarding patient-facing procedures."),
    ("gap", ""),
    ("h2", "Action Items"),
    ("body",
     "- L. Martins to circulate the revised budget by Friday.\n"
     "- S. Petrov to schedule the freezer service call.\n"
     "This document is purely operational and records no patient-facing procedures."),
]


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    jobs = [
        ("01_nsclc_osimertinib.pdf", NSCLC),
        ("02_diabetes_cardio.pdf", DIABETES),
        ("03_long_protocol.pdf", long_protocol()),
        ("04_not_a_protocol.pdf", NOT_A_PROTOCOL),
    ]
    for name, blocks in jobs:
        doc = render(blocks)
        path = OUT_DIR / name
        doc.save(str(path))
        print(f"  {name:30s} {doc.page_count:2d} page(s)  ->  {path}")
        doc.close()


if __name__ == "__main__":
    main()