"""Offline TypeSafe transport fixtures shared by Jev tool tests."""

from __future__ import annotations

from collections.abc import Callable, Generator

import httpx2
import pytest
from typesafe_sdk import RetryPolicy, TypeSafeClient

from jev_mcp import server
from jev_mcp.client import JevClient, RequestLimiter

ResponseAnswers = dict[str, object]
GatewayFactory = Callable[[ResponseAnswers], JevClient]


@pytest.fixture
def install_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[GatewayFactory, None, None]:
    """Install a real SDK client backed by an in-memory httpx2 transport."""

    gateways: list[TypeSafeClient] = []

    def factory(answers: ResponseAnswers) -> JevClient:
        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                json={
                    "model": "jev-test",
                    "usage": {"input_tokens": 100, "output_tokens": 0},
                    "answers": answers,
                },
            )

        sdk_client = TypeSafeClient(
            api_key="unit-test-key",
            retry=RetryPolicy(max_retries=0),
            timeout=8,
            transport=httpx2.MockTransport(handler),
        )
        gateway = JevClient(
            client=sdk_client,
            limiter=RequestLimiter(max_requests=1000),
        )
        gateways.append(sdk_client)
        monkeypatch.setattr(server, "get_client", lambda: gateway)
        return gateway

    yield factory

    for sdk_client in gateways:
        sdk_client.close()
