import pytest

from guardedgateway import cloud_gate


def test_local_provider_never_blocked(monkeypatch):
    monkeypatch.delenv("GG_ALLOW_CLOUD", raising=False)
    cloud_gate.require_cloud_allowed("fake", cap_usd=None)
    cloud_gate.require_cloud_allowed("ollama", cap_usd=None)


def test_cloud_provider_blocked_by_default(monkeypatch):
    monkeypatch.delenv("GG_ALLOW_CLOUD", raising=False)
    with pytest.raises(cloud_gate.CloudSpendBlocked):
        cloud_gate.require_cloud_allowed("openai", cap_usd=5.0)


def test_cloud_provider_blocked_without_cap_even_if_allowed(monkeypatch):
    monkeypatch.setenv("GG_ALLOW_CLOUD", "1")
    with pytest.raises(cloud_gate.CloudSpendBlocked):
        cloud_gate.require_cloud_allowed("openai", cap_usd=None)


def test_cloud_provider_allowed_with_flag_and_cap(monkeypatch):
    monkeypatch.setenv("GG_ALLOW_CLOUD", "1")
    cloud_gate.require_cloud_allowed("openai", cap_usd=5.0)
