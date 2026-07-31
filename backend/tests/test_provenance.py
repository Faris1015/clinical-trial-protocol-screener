"""Criterion → source passage resolution and its route (#54).

Two halves: the matcher (`app/services/provenance.py`), which has to survive the
ways a stored `source_text` legitimately differs from the protocol it was read
out of; and `GET /api/screenings/{id}/protocol`, which pairs the upload with the
resolved spans so the viewer can highlight one.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import provenance
from tests.auth_helpers import sign_in

PROTOCOL = (
    "Phase II single-arm study of an investigational agent in adults.\n\n"
    "Inclusion criteria:\n"
    "1. Age 18 years or older at the time of consent.\n"
    "2. Estimated glomerular filtration rate of at least 30 mL/min/1.73m2,\n"
    "   measured within 14 days of enrollment.\n\n"
    "Exclusion criteria:\n"
    "- Participation in another interventional trial within the prior 30 days.\n"
)

AGE_SENTENCE = "Age 18 years or older at the time of consent."


def _span(text: str, sentence: str) -> provenance.SourceSpan:
    span = provenance.locate(text, sentence)
    assert span is not None, f"expected to locate {sentence!r}"
    return span


# --- Matching ---------------------------------------------------------------


def test_a_verbatim_sentence_resolves_to_the_passage_it_came_from():
    span = _span(PROTOCOL, AGE_SENTENCE)

    assert PROTOCOL[span.start : span.end] == AGE_SENTENCE
    assert span.exact is True


def test_a_sentence_wrapped_across_lines_still_resolves():
    # What PDF extraction does to one sentence: the stored source_text reads as a
    # single line, the protocol has it broken and indented.
    span = _span(
        PROTOCOL,
        "Estimated glomerular filtration rate of at least 30 mL/min/1.73m2, "
        "measured within 14 days of enrollment.",
    )

    highlighted = PROTOCOL[span.start : span.end]
    assert highlighted.startswith("Estimated glomerular")
    assert highlighted.endswith("enrollment.")
    assert span.exact is True


def test_a_sentence_the_parser_stripped_a_list_marker_from_still_resolves():
    # `_clean_source_text` in the Parser drops the enumeration, so the stored
    # form is a substring of the protocol line rather than a copy of it.
    span = _span(PROTOCOL, AGE_SENTENCE)

    assert PROTOCOL[span.start - 3 : span.start] == "1. "


def test_matching_ignores_case_and_repeated_whitespace():
    span = _span(PROTOCOL, "age   18 YEARS or older at the time of consent.")

    assert PROTOCOL[span.start : span.end] == AGE_SENTENCE
    assert span.exact is True


def test_a_paraphrased_tail_falls_back_to_the_longest_matching_prefix():
    span = _span(PROTOCOL, "Age 18 years or older at screening, per the site's own records.")

    # The longest leading run of words that is actually in the protocol — the
    # paraphrased tail ("at screening, per...") is dropped, not guessed at.
    assert PROTOCOL[span.start : span.end] == "Age 18 years or older at"
    # Flagged, so the viewer can say the passage is approximate rather than
    # presenting a partial hit as the sentence itself.
    assert span.exact is False


def test_the_fallback_keeps_every_word_that_matches():
    # A long head, so the search for the boundary takes several steps: it has to
    # land on the last word that is really there, not the first one it tries.
    span = _span(
        PROTOCOL,
        "Estimated glomerular filtration rate of at least 30 mL/min/1.73m2, measured within "
        "14 days of the first dose of study drug.",
    )

    assert PROTOCOL[span.start : span.end].split() == (
        "Estimated glomerular filtration rate of at least 30 mL/min/1.73m2, measured within 14 "
        "days of"
    ).split()
    assert span.exact is False


def test_a_sentence_that_is_not_in_the_protocol_resolves_to_nothing():
    assert (
        provenance.locate(PROTOCOL, "Documented left ventricular ejection fraction above 50%.")
        is None
    )


def test_a_match_too_short_to_identify_a_passage_is_refused():
    # "Age 18 years" alone is four words — under the floor, so the fallback
    # declines rather than pointing at the first vaguely similar line.
    assert provenance.locate(PROTOCOL, "Age 18 years the applicant must be resident abroad") is None


@pytest.mark.parametrize("sentence", ["", "   \n  "])
def test_a_blank_source_text_resolves_to_nothing(sentence):
    assert provenance.locate(PROTOCOL, sentence) is None


def test_an_empty_protocol_resolves_nothing():
    assert provenance.locate_all("", [AGE_SENTENCE]) == []


# --- Collecting the sentences to resolve ------------------------------------


def test_every_bucket_contributes_its_source_text_including_unparseable():
    criteria = {
        "inclusion_quantitative": [{"source_text": "a"}],
        "inclusion_categorical": [{"source_text": "b"}],
        "exclusion_quantitative": [{"source_text": "c"}],
        "exclusion_categorical": [{"source_text": "d"}],
        "unparseable": ["e"],
    }

    assert provenance.source_texts(criteria) == ["a", "b", "c", "d", "e"]


def test_one_sentence_that_yielded_several_criteria_is_resolved_once():
    # "Age 18-75" is two criteria out of one passage; they highlight the same
    # span, so the payload carries it once.
    criteria = {
        "inclusion_quantitative": [
            {"source_text": AGE_SENTENCE},
            {"source_text": AGE_SENTENCE},
        ]
    }

    assert provenance.source_texts(criteria) == [AGE_SENTENCE]


@pytest.mark.parametrize("criteria", [None, {}, {"inclusion_quantitative": None}])
def test_an_extraction_with_nothing_in_it_yields_no_sentences(criteria):
    assert provenance.source_texts(criteria) == []


def test_locate_all_keeps_input_order_and_drops_what_it_cannot_find():
    spans = provenance.locate_all(
        PROTOCOL,
        [
            "Participation in another interventional trial within the prior 30 days.",
            "Known hypersensitivity to the investigational agent.",
            AGE_SENTENCE,
        ],
    )

    assert [s.source_text for s in spans] == [
        "Participation in another interventional trial within the prior 30 days.",
        AGE_SENTENCE,
    ]


# --- Route ------------------------------------------------------------------


class FakeSnapshot:
    def __init__(self, values: dict, pending: tuple = ()):
        self.values = values
        self.next = pending


class FakeGraph:
    """Returns one fixed snapshot — reading provenance never runs the pipeline."""

    def __init__(self, snapshot: FakeSnapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def astream(  # pragma: no cover - provenance never drives the graph
        self, input: object, config: object = None, *, stream_mode: object = None
    ) -> AsyncIterator[dict]:
        raise NotImplementedError
        yield {}

    async def ainvoke(self, *_a: object, **_k: object) -> dict:  # pragma: no cover
        raise NotImplementedError

    async def aupdate_state(  # pragma: no cover
        self, _config: object, _values: dict, as_node: str | None = None
    ) -> None:
        raise NotImplementedError


def _criteria() -> dict:
    return {
        "trial_title": "Demo",
        "inclusion_quantitative": [
            {
                "attribute": "age",
                "operator": ">=",
                "value": 18,
                "value_high": None,
                "unit": "years",
                "source_text": AGE_SENTENCE,
            }
        ],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": ["Adequate organ function per investigator assessment."],
    }


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        sign_in(c)
        yield c


def _create(client) -> str:
    upload = client.post(
        "/api/screenings", files={"file": ("protocol.md", PROTOCOL.encode(), "text/markdown")}
    )
    assert upload.status_code == 200
    return str(upload.json()["thread_id"])


def test_protocol_endpoint_returns_the_upload_and_its_spans(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({"parsed_criteria": _criteria()})))

    body = client.get(f"/api/screenings/{thread_id}/protocol").json()

    assert body["thread_id"] == thread_id
    assert body["source_filename"] == "protocol.md"
    assert body["text"] == PROTOCOL
    # The extraction's one criterion resolves; the unparseable sentence is not in
    # this protocol, so it is absent rather than carrying sentinel offsets.
    assert [s["source_text"] for s in body["spans"]] == [AGE_SENTENCE]
    span = body["spans"][0]
    assert body["text"][span["start"] : span["end"]] == AGE_SENTENCE
    assert span["exact"] is True


def test_a_run_with_no_checkpoint_still_serves_its_protocol(client, monkeypatch):
    # Uploaded but never streamed: there are no criteria to resolve, but the
    # upload itself is exactly what such a run does have.
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({})))

    body = client.get(f"/api/screenings/{thread_id}/protocol").json()

    assert body["text"] == PROTOCOL
    assert body["spans"] == []


def test_an_unknown_thread_is_a_404(client, monkeypatch):
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({})))

    assert client.get("/api/screenings/nope/protocol").status_code == 404


def test_the_protocol_is_reviewer_only():
    with TestClient(main.app, raise_server_exceptions=False) as anonymous:
        assert anonymous.get("/api/screenings/any/protocol").status_code == 401
