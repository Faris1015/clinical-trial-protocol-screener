"""PDF ingestion: locate the eligibility section before any LLM sees the text.

Protocols run 80-200 pages; eligibility criteria live in one section. Scoring
pages by hint density and taking a window keeps the LLM input to ~4k tokens,
which is what makes a local 8B model viable.
"""
import pymupdf

SECTION_HINTS = ["inclusion criteria", "exclusion criteria", "eligibility", "study population"]


def extract_eligibility_text(pdf_bytes: bytes) -> str:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages = [page.get_text() for page in doc]
    if not pages:
        return ""
    scores = [sum(p.lower().count(h) for h in SECTION_HINTS) for p in pages]
    if max(scores) == 0:
        # No obvious eligibility section — return everything and let the
        # Router flag it as suspicious.
        return "\n".join(pages)
    center = scores.index(max(scores))
    window = pages[max(0, center - 2): center + 4]
    return "\n".join(window)
