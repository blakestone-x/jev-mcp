"""Reusable TypeSafe client, key loading, and the process-local request limiter."""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Mapping

from typesafe_sdk import (
    Choice,
    ListModelsResponse,
    Noul,
    RetryPolicy,
    Score,
    SystemOneResponse,
    TypeSafeClient,
)

from .models import StateValue

logger = logging.getLogger(__name__)
logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)

DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_RETRY_BUDGET_SECONDS = 20.0
DEFAULT_RATE_LIMIT = 8
RATE_LIMIT_WINDOW_SECONDS = 1.0

SDKQuestion = Choice | Noul | Score


class ConfigurationError(RuntimeError):
    """Raised when the process cannot find a usable TypeSafe API key."""


def load_api_key(
    env: Mapping[str, str] | None = None,
) -> str:
    """Load the API key from the process environment."""

    environment = os.environ if env is None else env
    from_environment = environment.get("TYPESAFE_API_KEY", "").strip()
    if from_environment:
        return from_environment

    raise ConfigurationError("TYPESAFE_API_KEY is not set")


def load_model(
    env: Mapping[str, str] | None = None,
) -> str:
    """Load the configured model name, falling back to the public default."""

    environment = os.environ if env is None else env
    configured = environment.get("JEV_MCP_MODEL", "").strip()
    return configured or DEFAULT_MODEL


class RequestLimiter:
    """A thread-safe sliding-window limiter for the shared SDK client."""

    def __init__(
        self,
        max_requests: int = DEFAULT_RATE_LIMIT,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Wait until this request is inside the configured sliding window."""

        while True:
            now = time.monotonic()
            with self._lock:
                while self._timestamps and now - self._timestamps[0] >= self._window_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max_requests:
                    self._timestamps.append(now)
                    return
                delay = self._window_seconds - (now - self._timestamps[0])
            time.sleep(max(delay, 0.001))


class JevClient:
    """Own one configured SDK client and serialize calls through one limiter."""

    def __init__(
        self,
        *,
        client: TypeSafeClient | None = None,
        limiter: RequestLimiter | None = None,
        model: str | None = None,
    ) -> None:
        self._owns_client = client is None
        self._model = load_model() if model is None else model
        self._client = (
            TypeSafeClient(
                api_key=load_api_key(),
                model=self._model,
                retry=RetryPolicy(max_retries=3, timeout=DEFAULT_RETRY_BUDGET_SECONDS),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            if client is None
            else client
        )
        self._limiter = RequestLimiter() if limiter is None else limiter

    @property
    def model(self) -> str:
        """Return the model configured for this gateway."""

        return self._model

    def evaluate(
        self,
        *,
        state: StateValue,
        questions: Mapping[str, SDKQuestion],
    ) -> SystemOneResponse:
        """Evaluate one request through the shared client and limiter."""

        self._limiter.wait()
        return self._client.system_one(state=state, questions=questions)

    def list_models(self) -> ListModelsResponse:
        """List models through the shared client and limiter."""

        self._limiter.wait()
        return self._client.models.list()

    def close(self) -> None:
        """Close only the SDK client owned by this gateway."""

        if self._owns_client:
            self._client.close()


_shared_client: JevClient | None = None
_shared_client_lock = threading.Lock()


def get_client() -> JevClient:
    """Return the process-wide reusable Jev client."""

    global _shared_client
    if _shared_client is None:
        with _shared_client_lock:
            if _shared_client is None:
                _shared_client = JevClient()
    return _shared_client


def close_client() -> None:
    """Close and clear the process-wide client, primarily for shutdown/tests."""

    global _shared_client
    with _shared_client_lock:
        if _shared_client is not None:
            _shared_client.close()
            _shared_client = None


atexit.register(close_client)
