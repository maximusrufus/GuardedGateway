"""Stripe billing — Checkout session creation + webhook verification.

Three-tier fallback, all local/test-safe:
1. Real Stripe: only if STRIPE_SECRET_KEY and a STRIPE_PRICE_* env var are
   set. Calls the real Stripe API via an injectable client factory (tests
   never hit the network — see tests/test_billing.py). Uses
   `stripe.StripeClient` (never the deprecated global `stripe.api_key = ...`
   pattern). `STRIPE_SECRET_KEY` should be a **restricted key** (`rk_...`)
   scoped to the minimum permissions this app needs; in production it must
   come from Secret Manager, never a committed `.env` file.
2. Static payment link: PAYMENT_LINK_URL env var, if Stripe isn't configured.
3. "billing not configured": neither is set.

Webhook verification uses `stripe.Webhook.construct_event` semantics but
implemented locally (HMAC-SHA256 over timestamp+payload, matching Stripe's
documented scheme) so this module has no hard dependency on the `stripe`
SDK for signature checking — it's optional, imported lazily only when
actually creating a Checkout/Portal session.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import string
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

APP_NAME = "guardedgateway"

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


def _app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")


def _integration_identifier(flow: str) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase, k=8))
    return f"{APP_NAME}-{flow}-{suffix}"


def get_client(secret_key: str):
    """Instantiate a `StripeClient`. Imports `stripe` lazily so it is only
    required when Stripe is actually configured."""
    import stripe  # noqa: PLC0415

    return stripe.StripeClient(secret_key)


def create_checkout_session(
    tier: str,
    tenant: str,
    *,
    customer_email: str | None = None,
    stripe_checkout_create: Callable[..., dict] | None = None,
) -> CheckoutResult:
    """`stripe_checkout_create` is injected in tests to avoid a real Stripe
    call; production code defaults it to a real `StripeClient`'s
    `v1.checkout.sessions.create`. `client_reference_id` is set to the
    tenant name -- the authoritative link back to this app's tenant store."""
    if tier not in TIER_PRICES_USD:
        return CheckoutResult(status="not_configured", message=f"unknown tier {tier!r}")

    secret, price = _stripe_configured(tier)
    if secret and price:
        creator = stripe_checkout_create
        if creator is None:
            try:
                client = get_client(secret)
                creator = client.v1.checkout.sessions.create
            except ImportError:
                return CheckoutResult(
                    status="not_configured",
                    message="STRIPE_SECRET_KEY is set but the stripe package isn't installed",
                )
        base = _app_base_url()
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price, "quantity": 1}],
            "success_url": f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}/billing/cancel",
            "client_reference_id": tenant,
            "integration_identifier": _integration_identifier(tier),
            "subscription_data": {"metadata": {"tenant": tenant}},
        }
        if customer_email:
            params["customer_email"] = customer_email
        session = creator(params)
        url = session["url"] if isinstance(session, dict) else session.url
        return CheckoutResult(status="stripe_session", url=url)

    static_link = os.environ.get("PAYMENT_LINK_URL")
    if static_link:
        return CheckoutResult(status="payment_link", url=static_link)

    return CheckoutResult(status="not_configured", message="billing not configured")


def create_portal_session(
    customer_id: str, *, stripe_portal_create: Callable[..., dict] | None = None
) -> CheckoutResult:
    """Create a Billing Portal session for a stored Stripe customer id."""
    secret = os.environ.get("STRIPE_SECRET_KEY")
    creator = stripe_portal_create
    if creator is None:
        if not secret:
            return CheckoutResult(status="not_configured", message="billing not configured")
        client = get_client(secret)
        creator = client.v1.billing_portal.sessions.create
    base = _app_base_url()
    session = creator({"customer": customer_id, "return_url": f"{base}/billing/portal-return"})
    url = session["url"] if isinstance(session, dict) else session.url
    return CheckoutResult(status="stripe_session", url=url)


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


def session_tenant(session: dict) -> str | None:
    """Resolve the tenant for a Checkout Session: prefer the first-class
    `client_reference_id`; fall back to subscription metadata only if unset."""
    tenant = session.get("client_reference_id")
    if tenant:
        return tenant
    metadata = session.get("metadata") or {}
    return metadata.get("tenant")
