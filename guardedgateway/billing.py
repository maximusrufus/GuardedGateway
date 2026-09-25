"""Stripe billing stub — Checkout session creation + webhook verification.

Three-tier fallback, all local/test-safe:
1. Real Stripe: only if STRIPE_SECRET_KEY and a STRIPE_PRICE_* env var are
   set. Calls the real Stripe API via `stripe_client` (injectable so tests
   never hit the network — see tests/test_billing.py).
2. Static payment link: PAYMENT_LINK_URL env var, if Stripe isn't configured.
3. "billing not configured": neither is set.

Webhook verification uses `stripe.Webhook.construct_event` semantics but
implemented locally (HMAC-SHA256 over timestamp+payload, matching Stripe's
documented scheme) so this module has no hard dependency on the `stripe`
SDK — it's optional, imported lazily only when STRIPE_SECRET_KEY is set.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TIER_PRICES_USD = {
    "team": 199,
    "clinic": 499,
    "health_system": 999,
}


@dataclass
class CheckoutResult:
    status: str  # "stripe_session", "payment_link", "not_configured"
    url: str | None = None
    message: str | None = None


def _stripe_configured(tier: str) -> tuple[str | None, str | None]:
    secret = os.environ.get("STRIPE_SECRET_KEY")
    price = os.environ.get(f"STRIPE_PRICE_{tier.upper()}")
    return secret, price


def create_checkout_session(
    tier: str,
    *,
    stripe_checkout_create: Callable[..., dict] | None = None,
) -> CheckoutResult:
    """`stripe_checkout_create` is injected in tests to avoid a real Stripe
    call; production code (not exercised in tests) would default it to
    `stripe.checkout.Session.create`."""
    if tier not in TIER_PRICES_USD:
        return CheckoutResult(status="not_configured", message=f"unknown tier {tier!r}")

    secret, price = _stripe_configured(tier)
    if secret and price:
        creator = stripe_checkout_create
        if creator is None:
            try:
                import stripe

                stripe.api_key = secret
                creator = stripe.checkout.Session.create
            except ImportError:
                return CheckoutResult(
                    status="not_configured",
                    message="STRIPE_SECRET_KEY is set but the stripe package isn't installed",
                )
        session = creator(
            mode="subscription",
            line_items=[{"price": price, "quantity": 1}],
            success_url=os.environ.get("STRIPE_SUCCESS_URL", "https://example.com/success"),
            cancel_url=os.environ.get("STRIPE_CANCEL_URL", "https://example.com/cancel"),
        )
        url = session["url"] if isinstance(session, dict) else session.url
        return CheckoutResult(status="stripe_session", url=url)

    static_link = os.environ.get("PAYMENT_LINK_URL")
    if static_link:
        return CheckoutResult(status="payment_link", url=static_link)

    return CheckoutResult(status="not_configured", message="billing not configured")


def verify_webhook_signature(
    payload: bytes, sig_header: str, webhook_secret: str, *, tolerance_s: int = 300
) -> dict[str, Any] | None:
    """Verify a Stripe-style webhook signature header
    ('t=<ts>,v1=<hmac>'). Returns the parsed JSON body if valid, else None.
    Pure-python re-implementation of Stripe's documented HMAC scheme so it
    can be unit tested without the stripe SDK or a network call."""
    import json

    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        timestamp = parts["t"]
        signature = parts["v1"]
    except (KeyError, ValueError):
        return None

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(webhook_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None

    if abs(time.time() - int(timestamp)) > tolerance_s:
        return None

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
