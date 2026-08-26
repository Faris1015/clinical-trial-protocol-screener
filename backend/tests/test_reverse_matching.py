"""Reverse matching — a patient against every trial (#96).

Four things are being pinned here, in order of how badly they'd hurt if they
broke:

1. **A replayed verdict is the run's own.** The one claim the feature rests on
   (AC 4) is that what this says about `PT-0001` is what that run's cohort table
   says. The test for it compares against `cohort.bucket_counts` over the very
   evaluations the checkpoint holds, so the two cannot be made to agree by
   editing both.
2. **A rematch reuses the run's stored term mappings** rather than asking a model
   (AC 6), asserted by bolting every door to one shut for the duration.
3. **An unanswerable pair reaches a human.** A term the run never put to the
   mapper must not read as absence — that would produce a confident "ineligible"
   with nothing to mark it as a guess.
4. **Runs a human never approved contribute nothing**, in either direction.

The cohort is the simulation suite's arrangement — real `evaluate_patient`
output, never hand-written verdict rows — for the same reason: a test that
invented its own checkpoint could pass against a Matcher that had stopped
producing that shape at all.
"""

import builtins
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from app.exceptions import PatientNotFoundError
from app.graph.nodes import matcher
from app.persistence import ScreeningPage, ScreeningRecord, ScreeningStore
from app.services import cohort, patients, reverse, screening
from tests.auth_helpers import sign_in
from tests.fakes import FAKE_PATIENTS, PROTOCOL_TEXT, FakeChatModel, good_criteria

AGE: dict[str, Any] = {
    "attribute": "age",
    "operator": ">=",
    "value": 18.0,
    "value_high": None,
    "unit": "years",
    "source_text": "Age 18 years or older.",
}
EGFR: dict[str, Any] = {
    "attribute": "egfr",
    "operator": ">=",
    "value": 60.0,
    "value_high": None,
    "unit": "mL/min/1.73m2",
    "source_text": "eGFR at least 60 mL/min/1.73m2.",
}
NSCLC: dict[str, Any] = {
    "category": "diagnosis",
    "value": "NSCLC",
    "negated": False,
    "source_text": "Histologically confirmed non-small cell lung cancer.",
}

CRITERIA: dict[str, Any] = {
    "trial_title": "EGFR-Positive NSCLC Trial",
    "inclusion_quantitative": [AGE, EGFR],
    "inclusion_categorical": [NSCLC],
    "exclusion_quantitative": [],
    "exclusion_categorical": [],
    "unparseable": [],
}

# The term the word-boundary fast path cannot settle, so a verdict for it has to
# come from somewhere — which is the whole subject of this file.
AMBIGUOUS = "adenocarcinoma of the lung"


def _patient(patient_id: str, *, diagnosis: str = "NSCLC stage IV", **labs: float) -> dict:
    return {
        "id": patient_id,
        "name": patient_id.lower(),
        "sex": "F",
        "cohort": "oncology",
        "labs": {"age": 40, "egfr": 70, **labs},
        "diagnoses": [diagnosis],
        "medications": [],
        "history": [],
    }


# In the run's cohort: one match, one who fails eGFR, one the mapper could not
# settle. The fourth is deliberately NOT scored by the run — it is the rematch.
SCORED = [_patient("PT-1"), _patient("PT-2", egfr=40), _patient("PT-3", diagnosis=AMBIGUOUS)]
UNSCORED = _patient("PT-4")

# What the run's term mapper resolved. "uncertain" for the one ambiguous term is
# what puts PT-3 in review, and it is also the entry a rematch reads back.
VERDICTS = {("nsclc", AMBIGUOUS): "uncertain"}
COHORT = [matcher.evaluate_patient(p, CRITERIA, VERDICTS) for p in SCORED]

# The checkpoint as `matcher_node` writes it, mappings and all.
VALUES: dict[str, Any] = {
    "parsed_criteria": CRITERIA,
    "matched_patients": COHORT,
    "approved_at": "2026-01-01T00:00:00+00:00",
    "term_mappings": matcher.serialize_verdicts(
        VERDICTS, matcher.cohort_terms(SCORED + [UNSCORED])
    ),
}

RUN = {
    "thread_id": "run-1",
    "source_filename": "nsclc.pdf",
    "status": "done",
    "created_at": "2026-01-01T00:00:00+00:00",
}


def _match(patient: dict, values: dict[str, Any] | None = None, **overrides: str):
    return reverse.match_run(
        patient, values if values is not None else VALUES, **{**RUN, **overrides}
    )


# --- The fixture itself ------------------------------------------------------


def test_the_run_being_replayed_is_what_the_matcher_produced():
    """The baseline, stated once: everything below is read against these three."""
    assert cohort.bucket_counts(COHORT) == {"eligible": 1, "review": 1, "ineligible": 1}


def test_the_matcher_records_the_term_mappings_it_resolved():
    """`term_mappings` is to the categorical half what `observed` (#95) is to the
    numeric one: without it nothing here can be re-derived without a model."""
    stored = VALUES["term_mappings"]
    assert ["nsclc", AMBIGUOUS, "uncertain"] in stored["verdicts"]
    # Every term the cohort put up, including the unscored patient's — that list
    # is what separates "asked and answered no" from "never asked".
    assert AMBIGUOUS in stored["terms"]
    assert "nsclc stage iv" in stored["terms"]


def test_no_match_verdicts_are_dropped_and_the_term_list_covers_them():
    """Storing the default verdict for every pair would put a five-figure blob in
    every checkpoint to record, over and over, "no". The term list is what makes
    dropping them lossless."""
    stored = matcher.serialize_verdicts(
        {("nsclc", AMBIGUOUS): "uncertain", ("nsclc", "warfarin"): "no_match"},
        ["adenocarcinoma of the lung", "warfarin"],
    )
    assert stored["verdicts"] == [["nsclc", AMBIGUOUS, "uncertain"]]
    # Dropped from the verdicts, still present as a term that *was* put to the
    # mapper — so a reader can tell it was answered rather than skipped.
    cache, asked = reverse.recover_verdicts({"term_mappings": stored})
    assert ("nsclc", "warfarin") not in cache
    assert "warfarin" in asked


# --- Replaying a recorded verdict (AC 4) -------------------------------------


@pytest.mark.parametrize(
    ("patient", "expected"),
    [(SCORED[0], "eligible"), (SCORED[1], "ineligible"), (SCORED[2], "review")],
    ids=["eligible", "ineligible", "review"],
)
def test_a_patient_the_run_scored_reads_back_the_run_s_own_bucket(patient, expected):
    """AC 4 — and it holds by construction, not by two implementations agreeing:
    the verdict is *read from* `matched_patients`, never recomputed beside it."""
    match = _match(patient)
    assert match is not None
    assert match["source"] == "recorded"
    assert match["bucket"] == expected
    recorded = next(e for e in COHORT if e["patient_id"] == patient["id"])
    assert match["bucket"] == cohort.bucket_of(recorded)
    assert match["criterion_results"] == recorded["criterion_results"]
    assert match["summary"] == recorded["summary"]


def test_a_replayed_verdict_reports_nothing_unmapped():
    """There is nothing to map: the run already answered for this patient."""
    assert _match(SCORED[2])["unmapped"] == 0


def test_the_trial_is_named_by_its_extraction_not_by_its_filename():
    assert _match(SCORED[0])["trial_title"] == "EGFR-Positive NSCLC Trial"


def test_a_trial_with_no_parsed_title_falls_back_to_the_uploaded_filename():
    """A row headed by neither would be a link with nothing on it."""
    values = {**VALUES, "parsed_criteria": {**CRITERIA, "trial_title": "  "}}
    assert _match(SCORED[0], values)["trial_title"] == "nsclc.pdf"


# --- Rematching a patient the run never saw ----------------------------------


def test_a_patient_outside_the_run_s_cohort_is_scored_against_its_criteria():
    match = _match(UNSCORED)
    assert match is not None
    assert match["source"] == "rematched"
    # PT-4 is PT-1 by another name — same labs, same diagnosis — so the criteria
    # that cleared one clear the other.
    assert match["bucket"] == "eligible"
    assert match["unmapped"] == 0


def test_a_rematch_reuses_the_run_s_stored_mapping_rather_than_asking_again(monkeypatch):
    """AC 6, as a structural guarantee: every door to a model is bolted shut, and
    the ambiguous term still resolves — to the verdict the *run* reached for it."""

    def forbidden():
        raise AssertionError("reverse matching must not call the LLM")

    monkeypatch.setattr(matcher_mod, "get_llm", forbidden)
    monkeypatch.setattr(critic_mod, "get_llm", forbidden)

    match = _match(_patient("PT-9", diagnosis=AMBIGUOUS))
    assert match["source"] == "rematched"
    # The run recorded "uncertain" for this pair, so the patient needs a human —
    # the same answer the run gave PT-3, reached from the same cached verdict.
    assert match["bucket"] == "review"
    assert match["unmapped"] == 0


def test_a_term_the_run_never_saw_goes_to_a_human_rather_than_reading_as_absence():
    """The one failure mode here that would be invisible.

    "prior pemetrexed" is not in the run's term list, and the fast path cannot
    settle it against "NSCLC" either way. Read as absence it would produce a
    confident *ineligible* with nothing to mark it as a guess; the honest answer
    is that nobody ever asked.
    """
    stranger = _patient("PT-9", diagnosis="prior pemetrexed")
    match = _match(stranger)
    assert match["bucket"] == "review"
    assert match["unmapped"] == 1
    unresolved = [r for r in match["criterion_results"] if r["status"] == "unknown"]
    assert [r["criterion"]["value"] for r in unresolved] == ["NSCLC"]


def test_a_run_scored_before_term_mappings_existed_sends_the_patient_to_a_human():
    """Degrades exactly as an unavailable LLM does inside the Matcher itself:
    "uncertain" → needs review, never a silent pass or fail."""
    values = {k: v for k, v in VALUES.items() if k != "term_mappings"}
    match = _match(_patient("PT-9", diagnosis=AMBIGUOUS), values)
    assert match["bucket"] == "review"
    assert match["unmapped"] == 1


def test_unmapped_counts_criteria_not_term_pairs():
    """ "2 criteria could not be checked" names a gap a reader can go and look at;
    "37 term pairs" is an implementation detail of how the check works."""
    stranger = {**_patient("PT-9", diagnosis="prior pemetrexed"), "medications": ["obscurumab"]}
    # Two unmappable terms, one categorical criterion between them.
    assert _match(stranger)["unmapped"] == 1


def test_a_malformed_mapping_block_degrades_to_asking_a_human():
    """A read-only page over someone else's checkpoint must not 500 on a bad row."""
    for junk in ("not a mapping", {"terms": "nope", "verdicts": [["too", "short"], 7, None]}):
        cache, asked = reverse.recover_verdicts({"term_mappings": junk})
        assert cache == {}
        assert asked == set()


# --- Which runs are asked at all ---------------------------------------------


def test_a_run_no_human_approved_contributes_no_verdict():
    """Answering from unapproved criteria would put a verdict in front of a
    coordinator that the gate exists to prevent existing."""
    values = {k: v for k, v in VALUES.items() if k != "approved_at"}
    assert _match(SCORED[0], values) is None


def test_a_run_that_never_parsed_contributes_no_verdict():
    assert _match(SCORED[0], {"approved_at": RUN["created_at"]}) is None


def test_a_skipped_run_leaves_no_row_rather_than_an_ineligible_one():
    """The distinction the whole `None` return exists for: "we did not ask" is not
    "the answer was no", and counting it as ineligible would be a claim."""
    result = reverse.build_reverse_match(SCORED[0], [], scanned=1, total=1)
    assert result["trials"] == []
    assert result["counts"] == {"eligible": 0, "review": 0, "ineligible": 0}


# --- Ordering and counts -----------------------------------------------------


def test_trials_are_ranked_best_bucket_first_then_newest():
    """The eligible trials answer the question that was asked; the ineligible ones
    are context, and scrolling past thirty of them to find a match answers a
    different one."""
    older = _match(SCORED[0], thread_id="run-0", created_at="2025-06-01T00:00:00+00:00")
    newer = _match(SCORED[0], thread_id="run-2", created_at="2026-06-01T00:00:00+00:00")
    rejected = _match(SCORED[1])

    result = reverse.build_reverse_match(SCORED[0], [older, rejected, newer], scanned=3, total=3)

    assert [t["bucket"] for t in result["trials"]] == ["eligible", "eligible", "ineligible"]
    assert [t["thread_id"] for t in result["trials"][:2]] == ["run-2", "run-0"]
    assert result["counts"] == {"eligible": 2, "review": 0, "ineligible": 1}


def test_the_counts_come_from_the_cohort_module():
    """The same three-bucket reduction the cohort table shows, transposed — not a
    second tally that could drift from it."""
    trials = [_match(p) for p in SCORED]
    result = reverse.build_reverse_match(SCORED[0], trials, scanned=3, total=3)
    assert result["counts"] == cohort.bucket_counts(trials)


# --- Reading the cohort ------------------------------------------------------


@pytest.fixture
def ehr(monkeypatch):
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: [*SCORED, UNSCORED])


@pytest.fixture
def ehr_without_labs(monkeypatch):
    """A hand-edited record the Matcher cannot score — no `labs` to compare against."""
    monkeypatch.setattr(
        matcher_mod,
        "load_patients",
        lambda: [
            {"id": "PT-X", "name": "edited", "diagnoses": [], "medications": [], "history": []}
        ],
    )


def test_the_index_returns_summaries_not_records(ehr):
    page = patients.list_patients(limit=2, offset=0)
    assert page == {
        "items": [
            {
                "id": "PT-1",
                "name": "pt-1",
                "sex": "F",
                "cohort": "oncology",
                "age": 40.0,
                "diagnoses": 1,
                "medications": 0,
                "history": 0,
            },
            {
                "id": "PT-2",
                "name": "pt-2",
                "sex": "F",
                "cohort": "oncology",
                "age": 40.0,
                "diagnoses": 1,
                "medications": 0,
                "history": 0,
            },
        ],
        "total": 4,
        "limit": 2,
        "offset": 0,
    }


def test_the_index_total_counts_the_filtered_cohort_not_the_page(ehr):
    """What lets the caller say whether a next page exists."""
    assert patients.list_patients(limit=1, offset=0)["total"] == 4
    assert patients.list_patients(limit=1, offset=0, search="pt-3")["total"] == 1


def test_a_record_is_returned_as_the_matcher_read_it(ehr):
    """Reshaping it could show a reader something subtly different from what the
    verdicts beside it were reached from."""
    assert patients.get_patient("PT-3") == SCORED[2]


def test_an_unknown_patient_id_is_a_404_not_an_empty_record(ehr):
    with pytest.raises(PatientNotFoundError):
        patients.get_patient("PT-404")


def test_a_hand_edited_record_renders_rather_than_failing_the_index(monkeypatch):
    """The EHR is a JSON file a demo deployment regenerates."""
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: [{"id": "PT-X", "diagnoses": "flu"}])
    assert patients.list_patients(limit=25)["items"][0] == {
        "id": "PT-X",
        "name": "",
        "sex": "",
        "cohort": "",
        "age": None,
        "diagnoses": 0,
        "medications": 0,
        "history": 0,
    }


# --- The walk over runs ------------------------------------------------------


class FakeSnapshot:
    def __init__(self, values: dict[str, Any]):
        self.values = values
        self.next: tuple = ()


class FakeGraph:
    """Checkpoints by thread id, and every write path failing the test.

    The refusal is the point: reverse matching is sold as read-only, and a fake
    that quietly accepted a write would let that be broken silently.
    """

    def __init__(self, values_by_thread: dict[str, dict[str, Any]], *, broken: str | None = None):
        self.values_by_thread = values_by_thread
        self.broken = broken

    async def aget_state(self, config: dict) -> FakeSnapshot:
        thread_id = config["configurable"]["thread_id"]
        if thread_id == self.broken:
            raise RuntimeError("this checkpoint cannot be read")
        return FakeSnapshot(self.values_by_thread[thread_id])

    async def aupdate_state(self, *_a: object, **_k: object) -> None:
        raise AssertionError("reverse matching must not write to the checkpoint")

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict]:
        raise AssertionError("reverse matching must not run the graph")
        yield {}

    async def ainvoke(self, *_a: object, **_k: object) -> dict:
        raise AssertionError("reverse matching must not run the graph")


class FakeStore(ScreeningStore):
    """One page of rows, and every other method failing the test if reached.

    A real subclass rather than a duck-typed stand-in, so the walk is checked
    against the store contract it is declared to take — and the refusals below
    are the same device `FakeGraph` uses: reverse matching reads the runs index
    and nothing else, and a fake that quietly served a second call would let that
    stop being true silently.
    """

    def __init__(self, records: list[ScreeningRecord], total: int | None = None):
        self.records = records
        self.total = len(records) if total is None else total
        self.limit: int | None = None
        self.status: str | None = None

    async def list(self, **kwargs: Any) -> ScreeningPage:
        self.limit = limit = kwargs["limit"]
        self.status = kwargs.get("status")
        offset = kwargs.get("offset", 0)
        return ScreeningPage(items=self.records[offset : offset + limit], total=self.total)

    async def setup(self) -> None:
        raise AssertionError("reverse matching must not touch the store's schema")

    async def create(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("reverse matching must not write to the store")

    async def set_status(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("reverse matching must not write to the store")

    async def exists(self, *_a: Any, **_k: Any) -> bool:
        raise AssertionError("reverse matching reads the runs index, nothing else")

    async def get_input(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("reverse matching reads the runs index, nothing else")

    async def get_record(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("reverse matching reads the runs index, nothing else")

    async def list_parked(self) -> builtins.list[ScreeningRecord]:
        raise AssertionError("reverse matching reads the runs index, nothing else")

    async def mark_gate_entered(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("reverse matching must not write to the store")

    async def mark_reminder_sent(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("reverse matching must not write to the store")

    async def get_meta(self, *_a: Any, **_k: Any) -> str | None:
        raise AssertionError("reverse matching does not read meta")

    async def set_meta(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("reverse matching must not write to the store")


def _record(thread_id: str, created_at: str = "2026-01-01T00:00:00+00:00") -> ScreeningRecord:
    return ScreeningRecord(
        thread_id=thread_id,
        source_filename=f"{thread_id}.pdf",
        status="done",
        created_at=created_at,
    )


async def test_the_walk_puts_the_patient_to_every_approved_run(ehr):
    store = FakeStore([_record("run-1"), _record("run-2", "2026-02-01T00:00:00+00:00")])
    graph = FakeGraph({"run-1": VALUES, "run-2": VALUES})

    result = await screening.match_patient_to_trials(store, graph, "PT-1")

    assert result["patient_id"] == "PT-1"
    assert result["patient"] == SCORED[0]
    assert [t["thread_id"] for t in result["trials"]] == ["run-2", "run-1"]
    assert result["counts"]["eligible"] == 2
    assert (result["scanned"], result["total"]) == (2, 2)


async def test_an_unknown_patient_is_a_404_before_any_checkpoint_is_read(ehr):
    """An empty result would read as "this patient matches nothing"."""
    store = FakeStore([_record("run-1")])
    with pytest.raises(PatientNotFoundError):
        await screening.match_patient_to_trials(store, FakeGraph({}), "PT-404")


async def test_one_unreadable_checkpoint_costs_its_own_run_and_nothing_else(ehr):
    """And it narrows `scanned`, so the window shrinks rather than the page
    quietly claiming it read everything."""
    store = FakeStore([_record("run-1"), _record("run-2")])
    graph = FakeGraph({"run-1": VALUES, "run-2": VALUES}, broken="run-2")

    result = await screening.match_patient_to_trials(store, graph, "PT-1")

    assert [t["thread_id"] for t in result["trials"]] == ["run-1"]
    assert (result["scanned"], result["total"]) == (1, 2)


async def test_a_checkpoint_the_matcher_cannot_read_costs_its_own_run_too(ehr):
    """The second way a checkpoint is unreadable: `aget_state` returns, and what
    it returns is an extraction some older build wrote in a shape the Matcher no
    longer recognizes. A read-only page must not 500 over one of those either.

    The rematch path is where it bites — a recorded verdict is copied out and
    never scored — so this uses a patient the run's cohort does not contain.
    """
    broken_criteria = {k: v for k, v in CRITERIA.items() if k != "inclusion_categorical"}
    store = FakeStore([_record("run-1"), _record("run-2")])
    graph = FakeGraph(
        {"run-1": VALUES, "run-2": {**VALUES, "parsed_criteria": broken_criteria}},
    )

    result = await screening.match_patient_to_trials(store, graph, "PT-4")

    assert [t["thread_id"] for t in result["trials"]] == ["run-1"]
    assert (result["scanned"], result["total"]) == (1, 2)


async def test_a_patient_record_the_matcher_cannot_score_does_not_500_the_page(ehr_without_labs):
    """A hand-edited record with no `labs` is tolerated by the cohort index (it has
    its own test); the trials route must not be the one place it becomes a 500."""
    store = FakeStore([_record("run-1")])
    result = await screening.match_patient_to_trials(store, FakeGraph({"run-1": VALUES}), "PT-X")

    assert result["trials"] == []
    assert (result["scanned"], result["total"]) == (0, 1)


async def test_the_walk_asks_only_for_finished_runs(ehr):
    """`match_run` stays silent for a run no human approved, so loading its
    checkpoint spends the window on something that cannot answer — and on an
    instance with a backlog at the gate, that pushes runs that *can* out of it."""
    store = FakeStore([_record("run-1")])
    await screening.match_patient_to_trials(store, FakeGraph({"run-1": VALUES}), "PT-1")
    assert store.status == "done"


async def test_the_walk_is_bounded_and_says_so(ehr):
    """A trial the patient matches that fell outside the window is a missed match,
    so the payload states the window rather than implying it read everything."""
    store = FakeStore([_record("run-1")], total=500)
    result = await screening.match_patient_to_trials(
        store, FakeGraph({"run-1": VALUES}), "PT-1", sample=1
    )
    assert store.limit == 1
    assert (result["scanned"], result["total"]) == (1, 500)


# --- The routes --------------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        sign_in(c)
        yield c


def test_the_patients_index_pages_like_every_other_index(client, ehr):
    body = client.get("/api/patients?limit=2&offset=1").json()
    assert [row["id"] for row in body["items"]] == ["PT-2", "PT-3"]
    assert (body["total"], body["limit"], body["offset"]) == (4, 2, 1)


def test_the_patients_index_searches_id_and_name(client, ehr):
    assert [r["id"] for r in client.get("/api/patients?q=PT-3").json()["items"]] == ["PT-3"]
    assert [r["id"] for r in client.get("/api/patients?q=pt-4").json()["items"]] == ["PT-4"]


def test_the_patient_route_serves_the_whole_record(client, ehr):
    assert client.get("/api/patients/PT-1").json() == SCORED[0]


def test_an_unknown_patient_route_is_a_404_with_the_error_contract(client, ehr):
    response = client.get("/api/patients/PT-404")
    assert response.status_code == 404
    assert response.json()["error"] == "PatientNotFoundError"


def test_the_trials_route_answers_from_the_stored_runs(client, ehr, monkeypatch):
    monkeypatch.setattr(main, "graph", FakeGraph({"run-1": VALUES}))
    monkeypatch.setattr(main, "_store", lambda: FakeStore([_record("run-1")]))

    body = client.get("/api/patients/PT-1/trials").json()

    assert body["counts"] == {"eligible": 1, "review": 0, "ineligible": 0}
    assert body["trials"][0]["trial_title"] == "EGFR-Positive NSCLC Trial"
    assert body["trials"][0]["source"] == "recorded"


def test_every_new_route_requires_a_signed_in_reviewer(ehr):
    """The guard is per-route and explicit (#50); these are three more of them."""
    with TestClient(main.app, raise_server_exceptions=False) as anonymous:
        for path in ("/api/patients", "/api/patients/PT-1", "/api/patients/PT-1/trials"):
            assert anonymous.get(path).status_code == 401


# --- End to end, through the real graph --------------------------------------


async def test_a_real_run_s_cohort_table_and_the_patient_view_agree(monkeypatch):
    """The claim, end to end (AC 4): screen a protocol for real, then ask one of
    its patients what they qualify for and get the same answer back.

    Nothing is faked between the two readings but the model itself — the Matcher,
    the checkpoint, the store and both endpoints are the real ones.
    """
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert [line async for line in resp.aiter_lines()]
            async with client.stream("POST", f"/api/screenings/{thread_id}/approve") as approve:
                assert [line async for line in approve.aiter_lines()]

            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            evaluation = next(
                e for e in state["values"]["matched_patients"] if e["patient_id"] == "PT-1"
            )

            trials = (await client.get("/api/patients/PT-1/trials")).json()

            assert len(trials["trials"]) == 1
            trial = trials["trials"][0]
            assert trial["thread_id"] == thread_id
            assert trial["source"] == "recorded"
            # The cohort table's own verdict for this patient, read back whole.
            assert trial["bucket"] == cohort.bucket_of(evaluation)
            assert trial["summary"] == evaluation["summary"]
            assert trial["criterion_results"] == evaluation["criterion_results"]
            # And the run wrote its mappings, so a patient it never saw could be
            # scored against it without a model.
            assert "term_mappings" in state["values"]
