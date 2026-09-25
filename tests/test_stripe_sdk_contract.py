"""Pins our injected billing fakes' contract to the REAL Stripe SDK's contract.

`stripe.StripeClient(...).v1.checkout.sessions.create` (and the
billing_portal equivalent) take a single positional `params` dict, not
`**kwargs`. `billing.py`'s non-injected (real) path passes
`client.v1.checkout.sessions.create` / `client.v1.billing_portal.sessions.create`
straight through as `creator`, so `create_checkout_session`/
`create_portal_session` must call `creator(params_dict)`, never
`creator(**params_dict)` -- or the real SDK raises TypeError even though
`stripe_checkout_create`/`stripe_portal_create` injection lets local tests
never notice.

Imports the real `stripe` SDK only -- no network call, no API key needed.
"""

from __future__ import annotations

import inspect

import pytest

from guardedgateway import billing


def test_real_sdk_checkout_sessions_create_is_positional_params():
    from stripe.checkout._session_service import SessionService

    params = list(inspect.signature(SessionService.create).parameters)
    assert params[:3] == ["self", "params", "options"]


def test_real_sdk_billing_portal_sessions_create_is_positional_params():
    from stripe.billing_portal._session_service import SessionService

    params = list(inspect.signature(SessionService.create).parameters)
    assert params[:3] == ["self", "params", "options"]


def test_create_checkout_session_calls_injected_creator_with_positional_dict(monkeypatch):
    """The injected fake mirrors the real SDK's `create(params)` shape --
    only accepting a single positional dict, never `**kwargs` -- so this
    test fails if create_checkout_session reverts to `creator(**params)`."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_fake")
    monkeypatch.setenv("STRIPE_PRICE_TEAM", "price_fake_team")

    captured = {}

    def fake_create(params):
        captured.update(params)
        return {"url": "https://checkout.stripe.com/fake-session"}

    result = billing.create_checkout_session("team", "tenant-1", stripe_checkout_create=fake_create)
    assert result.status == "stripe_session"
    assert captured["client_reference_id"] == "tenant-1"


def test_create_checkout_session_rejects_kwargs_only_creator(monkeypatch):
    """If create_checkout_session regresses to `creator(**params)`, an
    autospec'd real-signature fake must reject it with TypeError."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_fake")
    monkeypatch.setenv("STRIPE_PRICE_TEAM", "price_fake_team")

    def strict_create(params):
        return {"url": "https://checkout.stripe.com/fake-session"}

    # Simulate a caller reverting to kwargs by calling the strict fake wrong.
    with pytest.raises(TypeError):
        strict_create(mode="subscription", line_items=[])


def test_create_portal_session_calls_injected_creator_with_positional_dict():
    captured = {}

    def fake_create(params):
        captured.update(params)
        return {"url": "https://billing.stripe.com/portal_1"}

    result = billing.create_portal_session("cus_1", stripe_portal_create=fake_create)
    assert result.status == "stripe_session"
    assert captured["customer"] == "cus_1"
