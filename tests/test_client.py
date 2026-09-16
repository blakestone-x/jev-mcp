"""Key-loading and configuration tests."""

import threading
import time

import pytest
from typesafe_sdk import RetryPolicy, TypeSafeClient

from jev_mcp.client import (
    DEFAULT_RETRY_BUDGET_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    ConfigurationError,
    JevClient,
    RequestLimiter,
    load_api_key,
    load_model,
)


def test_load_api_key_reads_environment_only() -> None:
    assert load_api_key({"TYPESAFE_API_KEY": " environment-key "}) == "environment-key"


def test_load_api_key_rejects_missing_key() -> None:
    with pytest.raises(ConfigurationError, match="TYPESAFE_API_KEY is not set"):
        load_api_key({})


def test_load_model_uses_environment_with_default() -> None:
    assert load_model({}) == "jev-latest"
    assert load_model({"JEV_MCP_MODEL": " jev-preview "}) == "jev-preview"


def test_jev_client_uses_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-test-key")
    monkeypatch.setenv("JEV_MCP_MODEL", "jev-preview")
    client = JevClient()
    try:
        assert client.model == "jev-preview"
    finally:
        client.close()


def test_jev_client_uses_bounded_timeout_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeTypeSafeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setenv("TYPESAFE_API_KEY", "unit-test-key")
    monkeypatch.setattr("jev_mcp.client.TypeSafeClient", FakeTypeSafeClient)
    client = JevClient()
    client.close()

    assert captured["timeout"] == DEFAULT_TIMEOUT_SECONDS == 8.0
    retry = captured["retry"]
    assert isinstance(retry, RetryPolicy)
    assert retry.timeout == DEFAULT_RETRY_BUDGET_SECONDS == 20.0
    assert TypeSafeClient is not FakeTypeSafeClient


def test_request_limiter_never_admits_more_than_eight_in_any_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = 0.0
    admissions: list[float] = []

    def monotonic() -> float:
        return current

    def sleep(delay: float) -> None:
        nonlocal current
        current += delay

    monkeypatch.setattr("jev_mcp.client.time.monotonic", monotonic)
    monkeypatch.setattr("jev_mcp.client.time.sleep", sleep)

    limiter = RequestLimiter(max_requests=8)
    for _ in range(17):
        limiter.wait()
        admissions.append(current)

    assert admissions == [0.0] * 8 + [1.0] * 8 + [2.0]
    for window_start in admissions:
        in_window = sum(window_start <= timestamp < window_start + 1.0 for timestamp in admissions)
        assert in_window <= 8


def test_request_limiter_holds_under_real_threads() -> None:
    limiter = RequestLimiter()
    barrier = threading.Barrier(4)
    admissions: list[float] = []
    admissions_lock = threading.Lock()

    def worker(call_count: int) -> None:
        barrier.wait()
        for _ in range(call_count):
            limiter.wait()
            with admissions_lock:
                admissions.append(time.monotonic())

    threads = [threading.Thread(target=worker, args=(count,)) for count in (5, 4, 4, 4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    timestamps = sorted(admissions)
    assert len(timestamps) == 17
    for window_start in timestamps:
        in_window = sum(window_start <= timestamp < window_start + 1.0 for timestamp in timestamps)
        assert in_window <= 8
