"""The Stripe Customer Portal must only ever open for the CALLING tenant.

Found 2026-09-25 on the live public deployment: `POST /billing/portal` took a
caller-supplied tenant NAME with no authentication and returned a Stripe
Customer Portal URL for that tenant. Tenant names are human-chosen and
guessable, and a portal session lets its holder read invoices, change the
payment method and cancel the subscription.
"""

from fastapi.testclient import TestClient

from guardedgateway.app import PUBLIC_ROUTES, app

client = TestClient(app)


def test_portal_requires_authentication():
    resp = client.post("/billing/portal")
    assert resp.status_code in (401, 403), "unauthenticated portal access must be refused"


def test_portal_rejects_a_bad_key():
    resp = client.post("/billing/portal", headers={"Authorization": "Bearer gg_not_a_real_key"})
    assert resp.status_code in (401, 403)


def test_portal_ignores_a_caller_supplied_tenant():
    """Passing someone else's tenant name must not open their portal."""
    resp = client.post(
        "/billing/portal?tenant=someone-else",
        headers={"Authorization": "Bearer gg_not_a_real_key"},
    )
    assert resp.status_code in (401, 403)


def test_portal_is_not_in_the_public_allowlist():
    assert ("POST", "/billing/portal") not in PUBLIC_ROUTES
