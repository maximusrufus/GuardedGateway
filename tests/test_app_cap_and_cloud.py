
import pytest


@pytest.fixture
def priced_openai_pricing(tmp_path, monkeypatch):
    """Deterministic pricing: every openai/gpt-4o-mini call, at max_tokens=5
    and the mock's fixed usage (prompt=10, completion=5), costs exactly
    $0.50 — so with cap=$1.00 the 3rd call must be refused."""
    from guardedgateway import config

    pricing_file = tmp_path / "pricing.yaml"
    pricing_file.write_text(
        """
models:
  "fake/echo":
    prompt_per_1k: 0.0
    completion_per_1k: 0.0
    baa: true
  "openai/gpt-4o-mini":
    prompt_per_1k: 0.0
    completion_per_1k: 100.0
    baa: false
default:
  prompt_per_1k: 0.0
  completion_per_1k: 0.0
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("GG_PRICING_FILE", str(pricing_file))
    config.clear_config_cache()
    yield
    config.clear_config_cache()


def test_third_call_refused_with_zero_upstream_calls_on_that_call(
    client, tenant_store, install_mock_transport, priced_openai_pricing, monkeypatch
):
    monkeypatch.setenv("GG_ALLOW_CLOUD", "1")
    api_key = tenant_store.create_key("acme", cap_usd=1.0)
    tenant_store.set_provider_key("acme", "openai", "sk-test-key")

    def make_request(i: int):
        return client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": f"distinct message {i}"}],
                "max_tokens": 5,
            },
        )

    r1 = make_request(1)
    assert r1.status_code == 200
    r2 = make_request(2)
    assert r2.status_code == 200
    assert len(install_mock_transport) == 2

    r3 = make_request(3)
    assert r3.status_code == 402
    body = r3.json()["detail"]
    assert body["error"] == "spend_cap"
    # the critical assertion: refusal happened BEFORE any network call —
    # the upstream mock must still show exactly 2 calls, not 3.
    assert len(install_mock_transport) == 2


def test_cloud_model_refused_by_default_when_allow_cloud_unset(
    client, tenant_store, install_mock_transport
):
    api_key = tenant_store.create_key("acme", cap_usd=5.0)
    tenant_store.set_provider_key("acme", "openai", "sk-test-key")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "cloud_blocked"
    assert len(install_mock_transport) == 0


def test_zero_cap_key_refuses_cloud_even_with_allow_cloud(
    client, tenant_store, install_mock_transport, monkeypatch
):
    monkeypatch.setenv("GG_ALLOW_CLOUD", "1")
    api_key = tenant_store.create_key("acme", cap_usd=0.0)
    tenant_store.set_provider_key("acme", "openai", "sk-test-key")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 402
    assert len(install_mock_transport) == 0
