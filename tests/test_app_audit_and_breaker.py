import json

import httpx


def test_audit_endpoint_never_returns_prompt_content(client, tenant_store):
    api_key = tenant_store.create_key("acme")
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "fake/echo",
            "messages": [{"role": "user", "content": "secret content xyz"}],
        },
    )
    resp = client.get("/v1/audit", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    assert "secret content xyz" not in resp.text
    lines = [json.loads(line) for line in resp.text.strip().splitlines() if line]
    assert len(lines) == 1
    assert "cost_usd" in lines[0]
    assert "model" in lines[0]
    assert "prompt" not in lines[0] and "content" not in lines[0]


def test_audit_requires_auth(client):
    resp = client.get("/v1/audit")
    assert resp.status_code == 401


def test_circuit_breaker_opens_after_repeated_upstream_failures(client, tenant_store, monkeypatch):
    from guardedgateway import providers as providers_module

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(failing_handler)
    providers_module.set_client(httpx.AsyncClient(transport=transport, timeout=5.0))

    api_key = tenant_store.create_key("acme")
    # ollama has fallback ["fake"], so the call still succeeds via fallback
    # after the breaker opens — assert via the dashboard that the breaker
    # for 'ollama' shows open after enough failures.
    for i in range(6):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "ollama/qwen2.5:7b",
                "messages": [{"role": "user", "content": f"msg {i}"}],
            },
        )
        assert resp.status_code == 200  # falls back to 'fake' each time

    dash = client.get("/dashboard", headers={"Authorization": f"Bearer {api_key}"})
    assert dash.status_code == 200
    assert "OPEN" in dash.text


def test_upstream_failure_falls_back_to_fake_provider(client, tenant_store):
    from guardedgateway import providers as providers_module

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(failing_handler)
    providers_module.set_client(httpx.AsyncClient(transport=transport, timeout=5.0))

    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "ollama/qwen2.5:7b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"].startswith("[fake/qwen2.5:7b] echo:")
