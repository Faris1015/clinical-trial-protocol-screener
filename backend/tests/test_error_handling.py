"""Defensive error handling (#4): retry policy, parser repair loop, data-store guards."""

import json
from typing import Any, cast

import pytest
from langchain_core.runnables import Runnable
from tenacity import wait_none

import app.services.llm as llm_service
from app.exceptions import DataStoreError, ExtractionError, LLMUnavailableError
from app.graph.nodes.critic import load_rules
from app.graph.nodes.matcher import load_patients
from app.graph.nodes.parser import parser_node, parser_router
from app.graph.state import ScreenerState
from app.services.llm import invoke_with_retry, is_transient
from app.services.pdf import extract_eligibility_text

VALID_CRITERIA = {
    "trial_title": "Test Trial",
    "inclusion_quantitative": [
        {
            "attribute": "age",
            "operator": ">=",
            "value": 18,
            "value_high": None,
            "unit": "years",
            "source_text": "Age >= 18 years",
        }
    ],
    "inclusion_categorical": [],
    "exclusion_quantitative": [],
    "exclusion_categorical": [],
    "unparseable": [],
}


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    """Retries should not sleep in tests."""
    monkeypatch.setattr(llm_service, "_RETRY_WAIT", wait_none())


class FlakyRunnable:
    """Fails `failures` times with `exc`, then returns `result`."""

    def __init__(self, exc: Exception, failures: int, result: object = "ok"):
        self.exc = exc
        self.failures = failures
        self.result = result
        self.calls = 0

    def invoke(self, _input: object) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return self.result


class StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


# --- is_transient ---------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ConnectionError("refused"), True),
        (TimeoutError("timed out"), True),
        (StatusError(500), True),
        (StatusError(503), True),
        (StatusError(429), True),
        (StatusError(400), False),
        (StatusError(422), False),
        (ValueError("bad input"), False),
    ],
)
def test_is_transient(exc, transient):
    assert is_transient(exc) is transient


# --- invoke_with_retry ----------------------------------------------------


def test_transient_failures_are_retried_until_success():
    runnable = FlakyRunnable(ConnectionError("refused"), failures=2)
    assert invoke_with_retry(cast(Runnable, runnable), "input") == "ok"
    assert runnable.calls == 3


def test_exhausted_transient_failures_raise_llm_unavailable():
    runnable = FlakyRunnable(ConnectionError("refused"), failures=99)
    with pytest.raises(LLMUnavailableError):
        invoke_with_retry(cast(Runnable, runnable), "input")
    assert runnable.calls == llm_service.MAX_LLM_ATTEMPTS


def test_non_transient_error_is_never_retried():
    runnable = FlakyRunnable(ValueError("validation failed"), failures=99)
    with pytest.raises(ValueError, match="validation failed"):
        invoke_with_retry(cast(Runnable, runnable), "input")
    assert runnable.calls == 1


# --- parser node ----------------------------------------------------------


BASE_STATE: ScreenerState = {
    "raw_protocol_text": "Inclusion criteria: age >= 18",
    "source_filename": "test.md",
    "parsed_criteria": None,
    "compliance_passed": False,
    "critic_feedback": None,
    "parse_attempts": 0,
    "compliance_findings": [],
    "matched_patients": [],
    "events": [],
    "current_step": "parsing",
}


class ScriptedLLM:
    """Stands in for get_llm().with_structured_output(...): replays `outputs`."""

    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[Any] = []

    def with_structured_output(self, _schema: object) -> "ScriptedLLM":
        return self

    def invoke(self, messages: object) -> object:
        self.calls.append(messages)
        out = self.outputs[min(len(self.calls), len(self.outputs)) - 1]
        if isinstance(out, Exception):
            raise out
        return out


def _patch_llm(monkeypatch, outputs: list[object]) -> ScriptedLLM:
    fake = ScriptedLLM(outputs)
    monkeypatch.setattr("app.graph.nodes.parser.get_llm", lambda: fake)
    return fake


def test_parser_valid_output_first_try(monkeypatch):
    fake = _patch_llm(monkeypatch, [VALID_CRITERIA])
    update = parser_node(BASE_STATE)
    assert update["current_step"] == "critiquing"
    assert update["parsed_criteria"]["trial_title"] == "Test Trial"
    assert len(fake.calls) == 1


def test_parser_repairs_invalid_output_once(monkeypatch):
    fake = _patch_llm(monkeypatch, [{"garbage": True}, VALID_CRITERIA])
    update = parser_node(BASE_STATE)
    assert update["current_step"] == "critiquing"
    assert len(fake.calls) == 2
    # The repair prompt carries the validation error back to the model
    repair_messages = fake.calls[1]
    assert "failed schema validation" in repair_messages[-1][1]


def test_parser_unrepairable_output_fails_gracefully(monkeypatch):
    fake = _patch_llm(monkeypatch, [{"garbage": True}, {"still": "garbage"}])
    update = parser_node(BASE_STATE)
    assert update["current_step"] == "failed"
    assert update["events"][0]["agent"] == "parser"
    assert update["events"][0]["status"] == "failed"
    assert len(fake.calls) == 2  # exactly one repair attempt, then give up


def test_parser_llm_down_fails_gracefully(monkeypatch):
    _patch_llm(monkeypatch, [ConnectionError("Ollama is down")])
    update = parser_node(BASE_STATE)
    assert update["current_step"] == "failed"
    assert "unavailable" in update["events"][0]["detail"].lower()


def test_parser_router_routes_failure_to_end():
    assert parser_router({**BASE_STATE, "current_step": "failed"}) == "failed"
    assert parser_router({**BASE_STATE, "current_step": "critiquing"}) == "critic"


# --- data-store guards ----------------------------------------------------


class FakeSettings:
    def __init__(self, **paths):
        self.__dict__.update(paths)


def test_missing_patients_file_raises_datastore_error(monkeypatch, tmp_path):
    settings = FakeSettings(patients_path=tmp_path / "nope.json")
    monkeypatch.setattr("app.graph.nodes.matcher.get_settings", lambda: settings)
    with pytest.raises(DataStoreError, match="unavailable"):
        load_patients()


def test_corrupt_patients_file_raises_datastore_error(monkeypatch, tmp_path):
    corrupt = tmp_path / "patients.json"
    corrupt.write_text("{not json[")
    settings = FakeSettings(patients_path=corrupt)
    monkeypatch.setattr("app.graph.nodes.matcher.get_settings", lambda: settings)
    with pytest.raises(DataStoreError, match="not valid JSON"):
        load_patients()


def test_non_list_patients_file_raises_datastore_error(monkeypatch, tmp_path):
    wrong = tmp_path / "patients.json"
    wrong.write_text(json.dumps({"patients": []}))
    settings = FakeSettings(patients_path=wrong)
    monkeypatch.setattr("app.graph.nodes.matcher.get_settings", lambda: settings)
    with pytest.raises(DataStoreError, match="JSON array"):
        load_patients()


def test_corrupt_rules_file_raises_datastore_error(monkeypatch, tmp_path):
    corrupt = tmp_path / "rules.yaml"
    corrupt.write_text("{invalid: yaml: [")
    settings = FakeSettings(rules_path=corrupt)
    monkeypatch.setattr("app.graph.nodes.critic.get_settings", lambda: settings)
    with pytest.raises(DataStoreError, match="not valid YAML"):
        load_rules()


def test_missing_rules_file_raises_datastore_error(monkeypatch, tmp_path):
    settings = FakeSettings(rules_path=tmp_path / "nope.yaml")
    monkeypatch.setattr("app.graph.nodes.critic.get_settings", lambda: settings)
    with pytest.raises(DataStoreError, match="unavailable"):
        load_rules()


# --- PDF extraction -------------------------------------------------------


def test_garbage_pdf_bytes_raise_extraction_error():
    with pytest.raises(ExtractionError, match="Could not read PDF"):
        extract_eligibility_text(b"this is not a pdf")


def test_empty_pdf_bytes_raise_extraction_error():
    with pytest.raises(ExtractionError):
        extract_eligibility_text(b"")
