"""Locating a criterion's `source_text` inside the protocol it came from (#54).

Every criterion carries the verbatim sentence the Parser extracted it from. That
makes provenance *claimable*; this module makes it *checkable*, by resolving each
sentence to a character span in the uploaded protocol so a reviewer can click a
criterion and see the passage it was read out of.

Why the matching is not a plain `str.find`:

* PDF extraction wraps lines, so a sentence that reads as one line on screen is
  `"...at least 18\\n  years of age..."` in the stored text.
* The Parser tidies the sentence before storing it (`_clean_source_text` strips a
  folded-in section header and any list enumeration), so the stored form is a
  *substring* of the protocol, not a copy of a whole line.
* Models paraphrase occasionally, however firmly the prompt says verbatim.

So the search runs on a whitespace-collapsed, casefolded projection of both
strings — with an index back to the original offsets, since the highlight has to
land on the text the reader is actually looking at — and falls back to the
longest leading run of words that does match. A span found that way is flagged
`exact=False` so the UI can say the passage is approximate rather than quietly
highlighting the wrong sentence. A sentence that matches nothing gets no span at
all: the caller renders "not found in the protocol", which is the honest answer
and the one an auditor needs to see.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# The buckets whose criteria carry a `source_text`. `unparseable` is a list of
# bare strings rather than criteria, and it is included on purpose: a sentence
# the Parser gave up on is exactly the one a reviewer most wants to find in
# context.
_CRITERIA_BUCKETS = (
    "inclusion_quantitative",
    "inclusion_categorical",
    "exclusion_quantitative",
    "exclusion_categorical",
)
_UNPARSEABLE_BUCKET = "unparseable"

# A partial match has to keep enough words to still identify a passage. Below
# this, "at least" or "patients with" would match the first of a dozen equally
# plausible sentences — a confidently wrong highlight, which is worse for an
# audit than no highlight.
MIN_PARTIAL_WORDS = 5


@dataclass(frozen=True)
class SourceSpan:
    """Where one `source_text` lives in the protocol: `[start, end)`, in
    characters of the original (un-normalized) text, so slicing the protocol with
    it yields the passage to highlight."""

    source_text: str
    start: int
    end: int
    exact: bool


def _project(text: str) -> tuple[str, list[int]]:
    """Casefold and collapse whitespace, keeping a map back to original offsets.

    Returns the projected string and a list the same length, where entry `i` is
    the index in `text` of the character projected to position `i`. A collapsed
    whitespace run maps to its first character, which is what makes a span found
    across a line break start at the right place.
    """
    projected: list[str] = []
    offsets: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if in_space:
                continue
            projected.append(" ")
            offsets.append(index)
            in_space = True
        else:
            projected.append(char.lower())
            offsets.append(index)
            in_space = False
    return "".join(projected), offsets


def _find(haystack: str, offsets: Sequence[int], needle: str) -> tuple[int, int] | None:
    """`needle`'s span in the *original* text, or None. Both sides projected."""
    if not needle:
        return None
    at = haystack.find(needle)
    if at < 0:
        return None
    # `offsets` indexes the last matched character, so +1 turns it into the
    # exclusive end the caller slices with.
    return offsets[at], offsets[at + len(needle) - 1] + 1


def _locate_projected(haystack: str, offsets: Sequence[int], source_text: str) -> SourceSpan | None:
    """One sentence's span against an already-projected protocol — exact if
    possible, else its longest matching leading run of words, else None."""
    needle = _project(source_text)[0].strip()
    if not needle:
        return None

    found = _find(haystack, offsets, needle)
    if found:
        return SourceSpan(source_text, found[0], found[1], exact=True)

    # Fall back to the longest leading run of words that *is* in the protocol:
    # the Parser's own cleanup only ever removes a *prefix* (a header, a list
    # marker), so the head of a sentence is the part most likely to have survived
    # verbatim, and a model that drifts tends to drift towards the end.
    #
    # Binary search rather than one find per length: a prefix of a matching
    # prefix is itself a substring, so "does words[:n] match?" is monotone in n —
    # true for every n below the answer, false above it. That turns the worst case
    # (a long sentence, matched against a long protocol, that only ever fails)
    # from one full scan per word into a handful.
    words = needle.split(" ")
    low, high = MIN_PARTIAL_WORDS, len(words) - 1
    best: tuple[int, int] | None = None
    while low <= high:
        mid = (low + high) // 2
        found = _find(haystack, offsets, " ".join(words[:mid]))
        if found:
            best = found
            low = mid + 1
        else:
            high = mid - 1
    if best:
        return SourceSpan(source_text, best[0], best[1], exact=False)
    return None


def locate(text: str, source_text: str) -> SourceSpan | None:
    """Where one sentence sits in `text`, or None if it cannot be found."""
    haystack, offsets = _project(text)
    return _locate_projected(haystack, offsets, source_text)


def source_texts(criteria: Mapping[str, Any] | None) -> list[str]:
    """Every distinct `source_text` in a parsed extraction, in bucket order.

    Deduplicated because several criteria are routinely read out of one sentence
    ("age 18-75 with an eGFR above 30" is three), and they all highlight the same
    passage — resolving it once keeps the payload proportional to the protocol
    rather than to the extraction.
    """
    if not criteria:
        return []
    seen: set[str] = set()
    ordered: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value not in seen:
            seen.add(value)
            ordered.append(value)

    for bucket in _CRITERIA_BUCKETS:
        for criterion in criteria.get(bucket) or ():
            if isinstance(criterion, Mapping):
                add(criterion.get("source_text"))
    for sentence in criteria.get(_UNPARSEABLE_BUCKET) or ():
        add(sentence)
    return ordered


def locate_all(text: str, sentences: Iterable[str]) -> list[SourceSpan]:
    """The spans for every sentence that could be located, in input order.

    Sentences that match nothing are simply absent: the frontend keys its lookup
    on `source_text`, so a missing entry *is* the "not found" signal, and echoing
    them back with sentinel offsets would invite a caller to slice with them.
    """
    if not text:
        return []
    # Projected once for the whole extraction rather than per sentence: a
    # protocol runs to the low hundreds of thousands of characters, and a run
    # has dozens of criteria.
    haystack, offsets = _project(text)
    spans = (_locate_projected(haystack, offsets, sentence) for sentence in sentences)
    return [span for span in spans if span is not None]
