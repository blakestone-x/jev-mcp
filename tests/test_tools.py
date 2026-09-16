"""Offline behavior tests for all policy-bearing Jev tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import cast

import httpx2
import pytest
from pydantic import ValidationError
from typesafe_sdk import RetryPolicy, TypeSafeClient

from jev_mcp import server
from jev_mcp.client import JevClient, RequestLimiter
from jev_mcp.models import AskInput, ScoreInput


def as_object(value: object) -> Mapping[str, object]:
    """Narrow a JSON object returned by a tool for readable test assertions."""

    if not isinstance(value, dict):
        raise AssertionError("expected a JSON object")
    return cast(Mapping[str, object], value)


def as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError("expected a string list")
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            raise AssertionError("expected a string list")
        result.append(item)
    return result


def choice_answer(
    choice: str,
    confidence: float,
    probabilities: dict[str, float],
) -> dict[str, object]:
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
    }


def score_answer(
    score: float,
    confidence: float = 0.8,
    level_count: int = 4,
) -> dict[str, object]:
    labels = ["low", "medium", "high", "critical"][:level_count]
    return {
        "type": "score",
        "score": score,
        "confidence": confidence,
        "legend": {str(index): label for index, label in enumerate(labels)},
        "probabilities": {str(index): (index + 1) / level_count for index in range(level_count)},
    }


def noul_answer(probability: float) -> dict[str, object]:
    return {"type": "noul", "noul": probability}


def test_envelope_shape_and_cost(
    install_gateway: Callable[[dict[str, object]], JevClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JEV_MCP_PRICE_PER_MTOK", raising=False)
    install_gateway({"choice": choice_answer("repair", 0.95, {"repair": 0.95, "inspect": 0.05})})
    result = server.jev_classify("The condenser needs a repair visit.", {"repair": "repair", "inspect": "inspect"})

    assert set(result) == {"ok", "model", "answers", "usage", "latency_ms"}
    assert result["ok"] is True
    assert result["model"] == "jev-test"
    assert as_object(result["usage"]) == {
        "input_tokens": 100,
        "output_tokens": 0,
        "est_cost_usd": pytest.approx(0.0000042),
    }
    latency = result["latency_ms"]
    assert isinstance(latency, (int, float))
    assert latency >= 0


def test_cost_uses_environment_price(
    install_gateway: Callable[[dict[str, object]], JevClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEV_MCP_PRICE_PER_MTOK", "0.1")
    install_gateway({"choice": choice_answer("repair", 0.8, {"repair": 0.8})})

    result = server.jev_classify("repair", {"repair": "repair"})

    assert as_object(result["usage"])["est_cost_usd"] == pytest.approx(0.00001)


def test_classify_preserves_explicit_other_description(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    custom_description = "CUSTOM_OTHER_DESCRIPTION_SENTINEL"

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = as_object(json.loads(request.content.decode("utf-8")))
        choice = as_object(as_object(body["questions"])["choice"])
        captured.update(as_object(choice["criteria"]))
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 1, "output_tokens": 0},
                "answers": {"choice": choice_answer("other", 0.8, {"other": 0.8})},
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_classify("text", {"other": custom_description}, add_other=True)
    finally:
        sdk_client.close()

    assert result["ok"] is True
    assert captured["other"] == custom_description


@pytest.mark.parametrize(
    ("confidence", "expected_band"),
    [(0.95, "act"), (0.50, "verify"), (0.49, "review")],
)
def test_classify_band_thresholds(
    confidence: float,
    expected_band: str,
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    install_gateway({"choice": choice_answer("repair", confidence, {"repair": confidence})})
    result = server.jev_classify("repair", {"repair": "repair"})

    assert as_object(result["answers"])["band"] == expected_band


def test_classify_ensemble_marks_disagreement_for_review(monkeypatch: pytest.MonkeyPatch) -> None:
    orders: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = as_object(json.loads(request.content.decode("utf-8")))
        choice = as_object(as_object(body["questions"])["choice"])
        criteria = as_object(choice["criteria"])
        orders.append(list(criteria))
        selected = "repair" if len(orders) == 1 else "inspect"
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "answers": {"choice": choice_answer(selected, 0.95, {selected: 0.95})},
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_classify(
            "text",
            {"repair": "repair", "inspect": "inspect"},
            ensemble=True,
        )
    finally:
        sdk_client.close()

    answers = as_object(result["answers"])
    assert orders[0] != orders[1]
    assert answers["agreement"] is False
    assert answers["alternate_choice"] == "inspect"
    assert answers["band"] == "review"


def test_check_returns_nested_probabilities_and_disjoint_sets(
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    install_gateway(
        {
            "flags": noul_answer(0.8),
            "middle": noul_answer(0.5),
            "low": noul_answer(0.2),
        }
    )
    result = server.jev_check(
        "The evidence is mixed.",
        {"flags": "The flag is supported.", "middle": "The evidence is uncertain.", "low": "The evidence is strong."},
    )

    answers = as_object(result["answers"])
    assert answers["probabilities"] == {"flags": 0.8, "middle": 0.5, "low": 0.2}
    flags = as_string_list(answers["flags"])
    uncertain = as_string_list(answers["uncertain"])
    assert flags == ["flags"]
    assert uncertain == ["middle"]
    assert set(flags).isdisjoint(uncertain)


def test_score_and_raw_ask_return_sdk_answers(
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    install_gateway({"score": score_answer(2.0, level_count=3), "q": noul_answer(0.75)})

    score_result = server.jev_score("degraded", ["low", "medium", "high"], "severity")
    ask_questions: dict[str, dict[str, object]] = {"q": {"type": "noul"}}
    ask_result = server.jev_ask(
        {"message": "degraded"},
        ask_questions,
    )

    score_answers = as_object(score_result["answers"])
    ask_answers = as_object(ask_result["answers"])
    assert score_answers["score"] == 2.0
    assert score_answers["normalized"] == pytest.approx(1.0)
    assert as_object(ask_answers["q"])["noul"] == pytest.approx(0.75)


@pytest.mark.parametrize(
    ("risk", "noul_probability", "expected"),
    [(2.1, 0.0, "block"), (0.5, 0.4, "review"), (1.2, 0.0, "review")],
)
def test_screen_gate_uses_risk_then_nouls(
    risk: float,
    noul_probability: float,
    expected: str,
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    answers: dict[str, object] = {
        question_id: noul_answer(noul_probability if question_id == "requests_secrets_or_exfil" else 0.0)
        for question_id in server.questions.SCREEN_NOULS
    }
    answers["risk"] = score_answer(risk, confidence=0.82)
    install_gateway(answers)

    result = server.jev_screen("untrusted text")

    screen_answers = as_object(result["answers"])
    assert screen_answers["verdict"] == expected
    assert screen_answers["risk_confidence"] == pytest.approx(0.82)
    if noul_probability:
        assert screen_answers["reasons"] == {"requests_secrets_or_exfil": 0.4}


def test_match_windows_450_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = {f"candidate-{index}": f"Candidate text {index}" for index in range(450)}
    calls: list[tuple[int, bool]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = as_object(json.loads(request.content.decode("utf-8")))
        state = as_object(body["state"])
        window_candidates = as_object(state["candidates"])
        question_map = as_object(body["questions"])
        ids = list(window_candidates)
        calls.append((len(ids), "exists" in question_map))

        if "exists" in question_map:
            if ids[0] == "candidate-0":
                selected, exists = ids[0], 0.55
            elif ids[0] == "candidate-200":
                selected, exists = "candidate-250", 0.80
            else:
                selected, exists = ids[-1], 0.40
            answers = {
                "choice": choice_answer(selected, 0.75, {selected: 0.75}),
                "exists": noul_answer(exists),
            }
        else:
            answers = {
                "choice": choice_answer(
                    "candidate-250",
                    0.88,
                    {"candidate-250": 0.88, "candidate-0": 0.07, "candidate-449": 0.05},
                )
            }
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "answers": answers,
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_match("Candidate 250", candidates)
    finally:
        sdk_client.close()

    answers = as_object(result["answers"])
    assert calls == [(200, True), (200, True), (50, True), (3, False)]
    assert answers["best_id"] == "candidate-250"
    assert answers["best_text"] == "Candidate text 250"
    assert answers["confidence"] == pytest.approx(0.88)
    assert answers["exists"] == pytest.approx(0.80)
    assert answers["abstain_probability"] == pytest.approx(0.0)
    assert answers["windows"] == 3
    assert answers["exists_by_window"] == pytest.approx([0.55, 0.80, 0.40])
    assert answers["top3"] == [
        {"id": "candidate-250", "probability": 0.88},
        {"id": "candidate-0", "probability": 0.07},
        {"id": "candidate-449", "probability": 0.05},
    ]
    assert answers["band"] == "match"
    assert answers["best_id"] is not None
    assert as_object(result["usage"])["input_tokens"] == 40


@pytest.mark.parametrize(
    ("selected", "confidence", "exists"),
    [
        ("candidate", 0.80, 0.80),
        ("none", 0.80, 0.80),
        ("candidate", 0.40, 0.80),
        ("candidate", 0.80, 0.20),
    ],
)
def test_match_band_match_always_has_a_selected_candidate(
    selected: str,
    confidence: float,
    exists: float,
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    probabilities = {selected: confidence}
    install_gateway(
        {
            "choice": choice_answer(selected, confidence, probabilities),
            "exists": noul_answer(exists),
        }
    )

    result = server.jev_match("query", {"candidate": "candidate"})
    answers = as_object(result["answers"])
    if answers["band"] == "match":
        assert answers["best_id"] is not None


def test_match_abstention_omits_confidence_and_reports_none_probability(
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    install_gateway(
        {
            "choice": choice_answer("none", 0.90, {"none": 0.90, "candidate": 0.10}),
            "exists": noul_answer(0.80),
        }
    )

    result = server.jev_match("query", {"candidate": "candidate"})
    answers = as_object(result["answers"])
    assert answers["best_id"] is None
    assert answers["best_text"] is None
    assert answers["confidence"] is None
    assert answers["abstain_probability"] == pytest.approx(0.90)
    assert answers["band"] == "review"


def test_match_reports_five_windows_and_per_window_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = {f"candidate-{index}": f"Candidate text {index}" for index in range(1000)}
    exists_values = [0.10, 0.20, 0.40, 0.30, 0.60]
    calls: list[tuple[int, bool]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = as_object(json.loads(request.content.decode("utf-8")))
        state = as_object(body["state"])
        window_candidates = as_object(state["candidates"])
        question_map = as_object(body["questions"])
        ids = list(window_candidates)
        calls.append((len(ids), "exists" in question_map))
        if "exists" in question_map:
            index = len([call for call in calls if call[1]]) - 1
            answers = {
                "choice": choice_answer(ids[0], 0.75, {ids[0]: 0.75}),
                "exists": noul_answer(exists_values[index]),
            }
        else:
            answers = {"choice": choice_answer(ids[0], 0.80, {ids[0]: 0.80})}
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "answers": answers,
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_match("query", candidates, window=200)
    finally:
        sdk_client.close()

    answers = as_object(result["answers"])
    assert calls == [(200, True)] * 5 + [(5, False)]
    assert answers["windows"] == 5
    assert answers["exists_by_window"] == pytest.approx(exists_values)
    assert answers["exists"] == pytest.approx(0.60)


def test_match_deadline_reports_completed_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    current = 0.0
    call_count = 0

    def monotonic() -> float:
        return current

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal call_count, current
        call_count += 1
        current += 0.4 if call_count == 1 else 0.7
        body = as_object(json.loads(request.content.decode("utf-8")))
        state = as_object(body["state"])
        candidate_ids = list(as_object(state["candidates"]))
        selected = candidate_ids[0]
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "answers": {
                    "choice": choice_answer(selected, 0.75, {selected: 0.75}),
                    "exists": noul_answer(0.50),
                },
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)
    monkeypatch.setattr(server, "perf_counter", monotonic)
    monkeypatch.setenv("JEV_MCP_MATCH_DEADLINE_S", "1.0")

    try:
        result = server.jev_match(
            "query",
            {f"candidate-{index}": f"Candidate {index}" for index in range(600)},
            window=200,
        )
    finally:
        sdk_client.close()

    assert result == {
        "ok": False,
        "status": 504,
        "error": "match deadline exceeded",
        "windows_completed": 2,
        "windows_total": 3,
    }


@pytest.mark.parametrize(
    ("alternate_choice", "expected_agreement", "expected_band"),
    [("label-0", True, "verify"), ("label-1", False, "review")],
)
def test_classify_ensemble_with_ten_labels_reports_agreement(
    alternate_choice: str,
    expected_agreement: bool,
    expected_band: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal call_count
        call_count += 1
        selected = "label-0" if call_count == 1 else alternate_choice
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 10, "output_tokens": 0},
                "answers": {"choice": choice_answer(selected, 0.80, {selected: 0.80})},
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        labels = {f"label-{index}": f"Label {index}" for index in range(10)}
        result = server.jev_classify("text", labels, ensemble=True)
    finally:
        sdk_client.close()

    answers = as_object(result["answers"])
    assert answers["agreement"] is expected_agreement
    assert answers["band"] == expected_band


def test_classify_rejects_provider_choice_outside_labels(
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    install_gateway({"choice": choice_answer("hostile", 0.9, {"hostile": 0.9})})

    result = server.jev_classify("text", {"repair": "repair"}, add_other=False)

    assert result == {
        "ok": False,
        "status": 502,
        "error": "TypeSafe returned an answer outside the submitted options",
    }


def test_score_rejects_provider_legend_outside_levels(
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    hostile_answer = score_answer(1.0, level_count=2)
    legend = cast(dict[str, object], hostile_answer["legend"])
    legend["2"] = "unexpected"
    install_gateway({"score": hostile_answer})

    result = server.jev_score("text", ["low", "high"], "severity")

    assert result == {
        "ok": False,
        "status": 502,
        "error": "TypeSafe returned an answer outside the submitted options",
    }


def test_ask_rejects_provider_choice_outside_criteria(
    install_gateway: Callable[[dict[str, object]], JevClient],
) -> None:
    install_gateway({"choice": choice_answer("hostile", 0.9, {"hostile": 0.9})})

    result = server.jev_ask(
        "text",
        {"choice": {"type": "choice", "criteria": {"yes": "yes"}}},
    )

    assert result == {
        "ok": False,
        "status": 502,
        "error": "TypeSafe returned an answer outside the submitted options",
    }


def test_health_uses_first_served_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "models": [
                    {"name": "jev-latest", "description": "Jev", "release_date": "2026-09-15"},
                    {"name": "jev-preview", "description": "Jev preview", "release_date": "2026-09-10"},
                ]
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_health()
    finally:
        sdk_client.close()

    assert result["model"] == "jev-latest"
    models = as_object(result["answers"])["models"]
    assert models == [
        {"name": "jev-latest", "description": "Jev", "release_date": "2026-09-15"},
        {"name": "jev-preview", "description": "Jev preview", "release_date": "2026-09-10"},
    ]


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_error", "expected_retry_after"),
    [
        ("401", 401, "TypeSafe API request failed", None),
        ("403", 403, "TypeSafe API request failed", None),
        ("404", 404, "TypeSafe API request failed", None),
        ("422", 422, "TypeSafe API request failed", None),
        ("429", 429, "TypeSafe API request failed", None),
        ("429-retry", 429, "TypeSafe API request failed", 1250.0),
        ("500", 500, "TypeSafe API request failed", None),
        ("529", 529, "TypeSafe API request failed", None),
        ("connection", 503, "TypeSafe service unavailable", None),
        ("timeout", 504, "TypeSafe request timed out", None),
        ("malformed", 502, "TypeSafe API response invalid", None),
    ],
)
def test_sdk_failures_use_safe_status_and_error_mapping(
    failure: str,
    expected_status: int,
    expected_error: str,
    expected_retry_after: float | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = f"ERROR_BODY_SENTINEL_{failure}"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        if failure == "connection":
            raise httpx2.ConnectError(sentinel)
        if failure == "timeout":
            raise httpx2.ReadTimeout(sentinel)
        if failure == "malformed":
            return httpx2.Response(200, json={"model": sentinel})
        status = int(failure.split("-", maxsplit=1)[0])
        headers = {"retry-after-ms": "1250"} if failure == "429-retry" else {}
        return httpx2.Response(status, json={"message": sentinel}, headers=headers)

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_classify("state", {"repair": "repair"})
    finally:
        sdk_client.close()

    assert result["ok"] is False
    assert result["status"] == expected_status
    assert result["error"] == expected_error
    assert result.get("retry_after_ms") == expected_retry_after
    assert sentinel not in json.dumps(result)


def test_response_boundary_ignores_unknown_sdk_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "usage": {"input_tokens": 1, "output_tokens": 0, "future_usage_field": "ignored"},
                "answers": {"choice": choice_answer("repair", 0.8, {"repair": 0.8})},
                "future_response_field": "ignored",
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_classify("state", {"repair": "repair"})
    finally:
        sdk_client.close()

    assert result["ok"] is True


def test_health_boundary_ignores_unknown_sdk_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "models": [
                    {
                        "name": "jev-latest",
                        "description": "Jev",
                        "release_date": "2026-09-15",
                        "future_model_field": "ignored",
                    }
                ],
                "future_response_field": "ignored",
            },
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_health()
    finally:
        sdk_client.close()

    assert result["ok"] is True
    assert as_object(result["answers"])["models"] == [
        {"name": "jev-latest", "description": "Jev", "release_date": "2026-09-15"}
    ]


def test_health_reports_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"models": [{"name": "jev-latest", "description": "Jev", "release_date": "2026-09-15"}]},
        )

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    monkeypatch.setenv("JEV_MCP_MODEL", "jev-preview")
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)

    try:
        result = server.jev_health()
    finally:
        sdk_client.close()

    assert result["model"] == "jev-preview"


def test_match_rejects_empty_candidates() -> None:
    result = server.jev_match("query", {})

    assert result["ok"] is False
    assert result["status"] == 400
    assert "candidate" in json.dumps(result)


def test_match_rejects_candidates_over_configured_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_MCP_MATCH_MAX_CANDIDATES", "2")
    result = server.jev_match("query", {"one": "one", "two": "two", "three": "three"})

    assert result["ok"] is False
    assert result["status"] == 400
    assert "JEV_MCP_MATCH_MAX_CANDIDATES" in json.dumps(result)
    assert "three" not in json.dumps(result)


@pytest.mark.parametrize("window", [19, 255])
def test_match_rejects_window_outside_service_bounds(window: int) -> None:
    result = server.jev_match("query", {"one": "one"}, window=window)

    assert result["ok"] is False
    assert result["status"] == 400
    assert "window" in json.dumps(result)


def test_request_budget_covers_state_and_screen_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_MCP_MAX_REQUEST_CHARS", "10")
    state_sentinel = "STATE_TOO_LARGE_SENTINEL"

    state_result = server.jev_score(state_sentinel, ["low", "high"], "question")
    text_result = server.jev_screen(state_sentinel)

    for result in (state_result, text_result):
        assert result["ok"] is False
        assert result["status"] == 400
        assert "JEV_MCP_MAX_REQUEST_CHARS" in json.dumps(result)
        assert state_sentinel not in json.dumps(result)


def test_request_budget_accepts_legacy_state_limit_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_MCP_MAX_REQUEST_CHARS", raising=False)
    monkeypatch.setenv("JEV_MCP_MAX_STATE_CHARS", "10")

    result = server.jev_score("state", ["low", "high"], "question")

    assert result["ok"] is False
    assert result["status"] == 400
    assert "JEV_MCP_MAX_REQUEST_CHARS" in json.dumps(result)


def test_request_budget_covers_question_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_MCP_MAX_REQUEST_CHARS", "200")
    calls = (
        lambda: server.jev_ask(
            "state",
            {"q": {"type": "choice", "instructions": "x" * 500, "criteria": {"yes": "yes"}}},
        ),
        lambda: server.jev_classify("state", {"label": "x" * 500}, add_other=False),
        lambda: server.jev_score("state", ["x" * 500, "high"], "question"),
        lambda: server.jev_check("state", {"flag": "x" * 500}),
    )

    for call in calls:
        result = call()
        assert result["ok"] is False
        assert result["status"] == 400
        assert "JEV_MCP_MAX_REQUEST_CHARS" in json.dumps(result)


def test_request_budget_caps_large_label_descriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_MCP_MAX_REQUEST_CHARS", raising=False)
    monkeypatch.delenv("JEV_MCP_MAX_STATE_CHARS", raising=False)
    description = "D" * 200_000
    labels = {f"label-{index}": description for index in range(254)}

    result = server.jev_classify("state", labels, add_other=False)

    assert result["ok"] is False
    assert result["status"] == 400
    assert "JEV_MCP_MAX_REQUEST_CHARS" in json.dumps(result)


def test_match_request_budget_caps_large_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_MCP_MAX_REQUEST_CHARS", raising=False)
    monkeypatch.delenv("JEV_MCP_MAX_STATE_CHARS", raising=False)
    description = "C" * 200_000
    candidates = {f"candidate-{index}": description for index in range(60)}

    result = server.jev_match("query", candidates)

    assert result["ok"] is False
    assert result["status"] == 400
    assert "JEV_MCP_MAX_REQUEST_CHARS" in json.dumps(result)


def test_validation_details_are_bounded() -> None:
    malformed_questions: dict[str, dict[str, object]] = {}
    for index in range(12):
        malformed_questions[str(index)] = {"type": "unsupported", "criteria": {}}
    result = server.jev_ask("state", malformed_questions)

    details = result["details"]
    assert isinstance(details, list)
    assert len(details) <= 10
    for detail in cast(list[object], details):
        detail_object = as_object(detail)
        assert isinstance(detail_object["msg"], str)
        assert len(detail_object["msg"]) <= 160


def test_validation_locations_are_bounded() -> None:
    huge_question_name = "Q" * 300_000
    result = server.jev_ask("state", {huge_question_name: {"type": "unsupported"}})

    assert result["ok"] is False
    assert len(json.dumps(result)) < 4_096
    details = result["details"]
    assert isinstance(details, list)
    for detail in cast(list[object], details):
        loc = as_object(detail)["loc"]
        assert isinstance(loc, list)
        assert all(isinstance(part, str) and len(part) <= 64 for part in cast(list[object], loc))


def test_raw_choice_rejects_256_options() -> None:
    malformed_questions = {"choice": {"type": "choice", "criteria": {str(index): "x" for index in range(256)}}}
    typed_questions = cast(dict[str, dict[str, object]], malformed_questions)
    result = server.jev_ask("state", typed_questions)

    assert result["ok"] is False
    assert result["status"] == 400
    details = result["details"]
    assert isinstance(details, list)
    assert set(as_object(details[0])) == {"loc", "msg"}


def test_score_rejects_one_level() -> None:
    result = server.jev_score("state", ["only"], "question")

    assert result["ok"] is False
    assert result["status"] == 400
    details = result["details"]
    assert isinstance(details, list)
    assert set(as_object(details[0])) == {"loc", "msg"}


def test_validation_models_reject_invalid_shapes() -> None:
    with pytest.raises(ValidationError):
        AskInput.model_validate({"state": "state", "questions": {"q": {"type": "score", "criteria": ["one"]}}})
    with pytest.raises(ValidationError):
        ScoreInput.model_validate({"state": "state", "levels": ["one"], "question": "question"})


def test_validation_failure_has_no_input_echo() -> None:
    sentinel = "VALIDATION_INPUT_SENTINEL_7b2f"
    result = server.jev_score(sentinel, ["only"], "QUESTION_SENTINEL")

    assert result["ok"] is False
    details = result["details"]
    assert isinstance(details, list)
    assert set(as_object(details[0])) == {"loc", "msg"}
    assert sentinel not in json.dumps(result)
    assert "QUESTION_SENTINEL" not in json.dumps(result)


def test_state_text_never_enters_logs(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    state_text = "UNIQUE_STATE_SHOULD_NEVER_APPEAR_IN_LOGS_7b2f"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"message": "server failed"})

    sdk_client = TypeSafeClient(
        api_key="unit-test-key",
        retry=RetryPolicy(max_retries=0),
        transport=httpx2.MockTransport(handler),
    )
    gateway = JevClient(client=sdk_client, limiter=RequestLimiter(max_requests=1000))
    monkeypatch.setattr(server, "get_client", lambda: gateway)
    caplog.set_level(logging.DEBUG)

    try:
        result = server.jev_classify(state_text, {"repair": "repair"})
    finally:
        sdk_client.close()

    assert result["ok"] is False
    assert state_text not in "\n".join(record.getMessage() for record in caplog.records)
