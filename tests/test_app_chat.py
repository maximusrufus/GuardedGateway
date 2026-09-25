def test_chat_completions_fake_provider_end_to_end(client, tenant_store, install_mock_transport):
    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "fake/echo",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 50,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"].startswith("[fake/echo] echo:")
    assert body["guardedgateway"]["cost_usd"] == 0.0


def test_chat_completions_missing_auth(client):
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "fake/echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_chat_completions_invalid_key(client):
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gg-not-real"},
        json={"model": "fake/echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_chat_completions_streaming_returns_sse(client, tenant_store):
    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "fake/echo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "data:" in resp.text
    assert "[DONE]" in resp.text


def test_embeddings_endpoint(client, tenant_store):
    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "fake/echo", "input": "some text"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) == 1


def test_ollama_provider_calls_mocked_upstream(client, tenant_store, install_mock_transport):
    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "ollama/qwen2.5:7b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "mock ollama response"
    assert len(install_mock_transport) == 1


def test_dashboard_renders(client, tenant_store):
    api_key = tenant_store.create_key("acme")
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "fake/echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    resp = client.get("/dashboard", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


def test_landing_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "GuardedGateway" in resp.text
    assert "199" in resp.text and "499" in resp.text and "999" in resp.text


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health(client):
    # /healthz is intercepted by Google Front End on Cloud Run and never
    # reaches the container; /health is the reachable liveness path in prod.
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
