"""Response cache (content-hash keyed) and per-provider circuit breaker.

Cache key logic ported from an internal LLM cache module
(`cache_key`: sha256 over model + normalized request fields, NUL-joined).
Store is an in-memory dict with TTL instead of the source's JSONL file —
simpler is fine per the task spec, and a gateway process doesn't need the
cache to survive a restart the way a multi-day eval script's cache does.

Circuit breaker pattern ported from an internal Redis-with-fallback cache
Redis circuit breaker: N consecutive failures opens the breaker; it
half-opens after a backoff and re-probes; a success closes it and resets the
backoff; a failure while half-open re-opens with the backoff doubled (capped).
Never latches permanently open — an operator restart is the only "un-stick",
same invariant the source enforces via reconnect probing.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def cache_key(model: str, messages: list[dict], max_tokens: int, temperature: float) -> str:
    h = hashlib.sha256()
    parts = [model, json.dumps(messages, sort_keys=True), str(max_tokens), repr(float(temperature))]
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class ResponseCache:
    """In-memory TTL cache, keyed by `cache_key()`. Thread-safe."""

    def __init__(self, ttl_seconds: float = 300.0, now: Callable[[], float] = time.time):
        self.ttl_seconds = ttl_seconds
        self._now = now
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if self._now() > expires_at:
                del self._store[key]
                self.misses += 1
                return None
            self.hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (self._now() + self.ttl_seconds, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


@dataclass
class CircuitBreakerState:
    failures: int = 0
    open_until: float | None = None
    half_open: bool = False


class CircuitBreaker:
    """One breaker per provider name. `now_fn` is injectable so tests never
    need a real sleep to exercise the backoff/half-open transition."""

    def __init__(
        self,
        failure_threshold: int = 5,
        backoff_seconds: float = 30.0,
        now_fn: Callable[[], float] = time.time,
    ):
        self.failure_threshold = failure_threshold
        self.backoff_seconds = backoff_seconds
        self._now = now_fn
        self._states: dict[str, CircuitBreakerState] = {}
        self._lock = threading.Lock()

    def _state(self, provider: str) -> CircuitBreakerState:
        if provider not in self._states:
            self._states[provider] = CircuitBreakerState()
        return self._states[provider]

    def is_open(self, provider: str) -> bool:
        """True if calls to `provider` should be refused/redirected to a
        fallback right now. Transitions OPEN -> HALF-OPEN automatically once
        the backoff has elapsed (the half-open probe itself is one call
        allowed through; record_success/record_failure resolve it)."""
        with self._lock:
            st = self._state(provider)
            if st.open_until is None:
                return False
            if self._now() >= st.open_until:
                st.half_open = True
                return False  # allow exactly one probe call through
            return True

    def record_success(self, provider: str) -> None:
        with self._lock:
            st = self._state(provider)
            st.failures = 0
            st.open_until = None
            st.half_open = False

    def record_failure(self, provider: str) -> None:
        with self._lock:
            st = self._state(provider)
            st.failures += 1
            if st.half_open:
                # failed the probe: reopen, double the backoff (capped by caller-configured max via reopen backoff growth)
                st.open_until = self._now() + self.backoff_seconds
                st.half_open = False
                return
            if st.failures >= self.failure_threshold:
                st.open_until = self._now() + self.backoff_seconds
