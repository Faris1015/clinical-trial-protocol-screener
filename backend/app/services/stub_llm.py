"""Zero-inference stub chat model for load testing and offline demos (#10).

`LLM_PROVIDER=stub` wires this in place of Ollama/Anthropic so the whole
screening pipeline runs end-to-end with no GPU, no network, and deterministic
timing. That isolates the app's own overhead — routing, SSE fan-out, the
concurrency gate, the checkpointer — from real model latency, which is the whole
point of a load-test baseline (see docs/performance.md).

The graph touches the model at exactly three call sites, each via
``get_llm().with_structured_output(Schema).invoke(messages)``:

    Parser  -> CriteriaSchema    (extract eligibility criteria)
    Critic  -> SemanticReview    (layer-2 semantic audit)
    Matcher -> TermMapping       (categorical term resolution)

We answer each with a canned, schema-valid instance chosen so a screening runs
cleanly to the human-in-the-loop gate and produces matches on approval: a
Parser extraction the deterministic Critic accepts, an empty SemanticReview (no
objections -> no Parser loop-back), and an empty TermMapping (the fast path
settles the rest). ``STUB_LATENCY_SECONDS`` optionally sleeps per call to model
a slow backend.

This is NOT the test double in tests/fakes.py: that one is scripted per-test to
drive specific graph branches; this one is a fixed, production-importable model
whose only job is to keep the pipeline flowing under load.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.schemas.criteria import CriteriaSchema
from app.schemas.review import SemanticReview, TermMapping

# A minimal extraction the deterministic Critic passes (one numeric inclusion
# with a threshold + unit; no vague organ-function language that would trip a
# rule). Every stub screening reports the same criteria — fine, timing is what a
# load test measures, not extraction quality.
_STUB_CRITERIA = CriteriaSchema(
    trial_title="Load-test protocol (stub extraction)",
    inclusion_quantitative=[
        {
            "attribute": "age",
            "operator": ">=",
            "value": 18,
            "value_high": None,
            "unit": "years",
            "source_text": "Age 18 years or older at the time of consent.",
        }
    ],
    inclusion_categorical=[],
    exclusion_quantitative=[],
    exclusion_categorical=[],
    unparseable=[],
)

# The extraction is clean, so the semantic audit finds nothing (an empty review
# means the Critic does not loop the Parser) and no categorical terms need LLM
# resolution (the Matcher's fast path handles the age criterion alone).
_STUB_REVIEW = SemanticReview(findings=[])
_STUB_TERM_MAPPING = TermMapping(results=[])


class _StubStructuredModel:
    """The ``with_structured_output(Schema)`` view: returns one canned instance.

    Bound to the schema at ``with_structured_output`` time so ``invoke`` can
    return the right type for whichever node is calling — the graph never
    branches on model *content* here, only on its shape being valid.
    """

    def __init__(self, schema: object, latency_seconds: float) -> None:
        self._schema = schema
        self._latency = latency_seconds

    def invoke(self, _messages: object, *_args: object, **_kwargs: object) -> Any:
        # A synchronous sleep on purpose: the real providers block too, and
        # LangGraph runs these sync nodes in a threadpool, so this models the
        # exact place inference time is spent under load.
        if self._latency > 0:
            time.sleep(self._latency)
        if self._schema is CriteriaSchema:
            return _STUB_CRITERIA
        if self._schema is SemanticReview:
            return _STUB_REVIEW
        if self._schema is TermMapping:
            return _STUB_TERM_MAPPING
        raise ValueError(f"StubChatModel has no canned output for schema {self._schema!r}")


class StubChatModel(FakeListChatModel):
    """A BaseChatModel whose structured-output view returns canned schema instances.

    Subclasses LangChain's ``FakeListChatModel`` only to satisfy the
    ``BaseChatModel`` type that ``get_llm`` is annotated to return; the graph
    only ever calls ``with_structured_output(...).invoke(...)``, which we
    override to bypass the (unused) text-generation path entirely.
    """

    latency_seconds: float = 0.0

    def __init__(self, latency_seconds: float = 0.0) -> None:
        # FakeListChatModel requires a non-empty response list even though the
        # structured path never reaches it.
        super().__init__(responses=["stub"], latency_seconds=latency_seconds)

    def with_structured_output(  # type: ignore[override]
        self, schema: object, **_kwargs: object
    ) -> _StubStructuredModel:
        return _StubStructuredModel(schema, self.latency_seconds)
