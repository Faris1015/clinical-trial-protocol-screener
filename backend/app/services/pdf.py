"""PDF ingestion: locate the eligibility section before any LLM sees the text.

Protocols run 80-200 pages; eligibility criteria live in one section. Scoring
pages by hint density and taking a window keeps the LLM input to ~4k tokens,
which is what makes a local 8B model viable.
"""

import pymupdf

from app.exceptions import ExtractionError

SECTION_HINTS = ["inclusion criteria", "exclusion criteria", "eligibility", "study population"]


def extract_eligibility_text(pdf_bytes: bytes, max_pages: int | None = None) -> str:
    # pymupdf signals unreadable input with FileDataError/EmptyFileError,
    # which subclass RuntimeError and ValueError respectively.
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        page_count = doc.page_count
    except (RuntimeError, ValueError) as exc:
        raise ExtractionError(f"Could not read PDF: {exc}") from exc
    # Bound the work before materializing text: a small-but-many-page PDF can
    # sit under the byte cap yet expand to hundreds of MB of extracted text.
    if max_pages is not None and page_count > max_pages:
        raise ExtractionError(f"PDF has {page_count} pages, exceeding the {max_pages}-page limit.")
    try:
        pages = [doc[i].get_text() for i in range(page_count)]
    except (RuntimeError, ValueError) as exc:
        raise ExtractionError(f"Could not read PDF: {exc}") from exc
    if not pages:
        return ""
    scores = [sum(p.lower().count(h) for h in SECTION_HINTS) for p in pages]
    if max(scores) == 0:
        # No obvious eligibility section — return everything and let the
        # Router flag it as suspicious.
        return "\n".join(pages)
    center = scores.index(max(scores))
    window = pages[max(0, center - 2) : center + 4]
    return "\n".join(window)
