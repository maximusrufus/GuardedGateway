from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("GG_ENCRYPTION_KEY", "kQqL8pQ3l8m1F0iH7c3d1w4hY6t2r9x0eK5jN2vB8sQ=")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own SQLite DB and a fresh in-process singleton so
    tests never share state or write to a real data/ directory."""
    from guardedgateway import app as app_module
    from guardedgateway import cache as cache_module
    from guardedgateway import config
    from guardedgateway import ledger as ledger_module
    from guardedgateway import providers as providers_module
    from guardedgateway import tenants as tenants_module

    db_path = tmp_path / "gg_test.db"
    monkeypatch.setenv("GG_DB_PATH", str(db_path))
    ledger_module.reset_ledger_for_tests(db_path)
    tenants_module.reset_tenant_store_for_tests(db_path)

    # fresh response cache + circuit breaker per test
    app_module._response_cache = cache_module.ResponseCache(ttl_seconds=300.0)
    app_module.set_breaker(cache_module.CircuitBreaker(failure_threshold=5, backoff_seconds=30.0))

    config.clear_config_cache()

    monkeypatch.delenv("GG_ALLOW_CLOUD", raising=False)
    monkeypatch.delenv("GG_MONTHLY_CAP_USD", raising=False)

    yield

    providers_module.set_client(httpx.AsyncClient(timeout=1.0))


@pytest.fixture
def tenant_store():
    from guardedgateway.tenants import get_tenant_store

    return get_tenant_store()


@pytest.fixture
def ledger():
    from guardedgateway.ledger import get_ledger

    return get_ledger()


@pytest.fixture
def client():
    from guardedgateway.app import app

    return TestClient(app)


@pytest.fixture
def mock_upstream_transport():
    """A configurable httpx.MockTransport recording every request it sees,
    so tests can assert exactly how many times the upstream was actually
    called (e.g. cap refusals must make ZERO calls)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "ollama" in str(request.url) or "/api/chat" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "message": {"content": "mock ollama response"},
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                },
            )
        if "openai.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "mock openai response"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        if "anthropic.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "content": [{"text": "mock anthropic response"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )
        if "generativelanguage" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": "mock gemini response"}]}}],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
                },
            )
        return httpx.Response(500, json={"error": "unhandled mock route"})

    return calls, handler


@pytest.fixture
def install_mock_transport(mock_upstream_transport, monkeypatch):
    from guardedgateway import providers as providers_module

    calls, handler = mock_upstream_transport
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=5.0)
    providers_module.set_client(client)
    return calls
