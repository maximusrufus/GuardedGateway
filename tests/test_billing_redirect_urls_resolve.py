"""Pins every Stripe `success_url`/`cancel_url` this app ever hands to
Checkout Sessions to a route that is actually registered on the FastAPI app.

A customer who pays and is redirected to a URL the app never registered
lands on a 404 -- their money is taken, fulfillment happens in the webhook,
and they see "Not Found". This test fails BEFORE that can happen again.
"""

from __future__ import annotations

from guardedgateway import billing
from guardedgateway.app import app


def _registered_paths():
    return {getattr(r, "path", None) for r in app.routes if getattr(r, "path", None)}


def _strip(url: str) -> str:
    path = url.split("://", 1)[-1]
    path = "/" + path.split("/", 1)[1] if "/" in path else "/"
    return path.split("?", 1)[0]


def test_all_checkout_redirect_urls_resolve_to_registered_routes(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_x")
    monkeypatch.setenv("APP_BASE_URL", "https://app.guardedgateway.example")
    for tier in billing.TIER_PRICES_USD:
        monkeypatch.setenv(f"STRIPE_PRICE_{tier.upper()}", f"price_{tier}")

    registered = _registered_paths()
    calls = []

    def fake_create(params):
        calls.append(dict(params))
        return {"id": "sess_123", "url": "https://checkout.stripe.example/sess_123"}

    for tier in billing.TIER_PRICES_USD:
        billing.create_checkout_session(tier, "tenant-1", stripe_checkout_create=fake_create)

    assert calls, "no checkout sessions were created"
    urls = [c["success_url"] for c in calls] + [c["cancel_url"] for c in calls]
    for url in urls:
        path = _strip(url)
        assert path in registered, (
            f"Stripe redirect URL {url!r} resolves to path {path!r}, "
            f"which is not a registered route on the app"
        )
