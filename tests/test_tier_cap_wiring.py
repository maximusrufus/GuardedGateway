"""Tier-derived spend cap wiring (FIX 4d): key cap -> tenant tier cap ->
global GG_MONTHLY_CAP_USD env default."""

from __future__ import annotations


def test_effective_cap_prefers_key_cap_over_tier(tenant_store):
    from guardedgateway.app import _effective_cap

    tenant_store.create_tenant("acme")
    tenant_store.set_tier("acme", "health_system")  # would give 8000.0
    assert _effective_cap(250.0, tenant="acme") == 250.0


def test_effective_cap_falls_back_to_tier_default(tenant_store):
    from guardedgateway.app import _effective_cap

    tenant_store.create_tenant("acme")
    tenant_store.set_tier("acme", "team")
    assert _effective_cap(None, tenant="acme") == 500.0


def test_effective_cap_falls_back_to_global_without_tier(tenant_store, monkeypatch):
    from guardedgateway.app import _effective_cap

    tenant_store.create_tenant("acme")
    monkeypatch.setenv("GG_MONTHLY_CAP_USD", "42.0")
    assert _effective_cap(None, tenant="acme") == 42.0


def test_effective_cap_none_when_nothing_configured(tenant_store):
    from guardedgateway.app import _effective_cap

    tenant_store.create_tenant("acme")
    assert _effective_cap(None, tenant="acme") is None
