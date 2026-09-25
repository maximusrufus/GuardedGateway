"""Integration tests for /billing/webhook and /billing/portal."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time


def _sign(payload: bytes, secret: str) -> str:
    ts = str(int(time.time()))
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _checkout_completed_payload(event_id="evt_1", tenant="tenant-a", payment_status="paid"):
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": tenant,
                    "payment_status": payment_status,
                    "customer": "cus_1",
                }
            },
        }
    ).encode()


def test_webhook_missing_secret_400(client):
    os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
    resp = client.post("/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    assert resp.status_code == 400


def test_webhook_bad_signature_400(client, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    resp = client.post(
        "/billing/webhook", content=b"{}", headers={"Stripe-Signature": "t=1,v1=deadbeef"}
    )
    assert resp.status_code == 400


def test_webhook_async_payment_succeeded_unpaid_no_fulfillment(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = json.dumps(
        {
            "id": "evt_async",
            "type": "checkout.session.async_payment_succeeded",
            "data": {
                "object": {
                    "client_reference_id": "tenant-async",
                    "payment_status": "unpaid",
                    "customer": "cus_async",
                }
            },
        }
    ).encode()
    header = _sign(payload, "whsec_test")
    resp = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp.status_code == 200
    assert tenant_store.get_tenant("tenant-async") is None


def test_webhook_checkout_completed_fulfills(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = _checkout_completed_payload(event_id="evt_fulfill", tenant="tenant-paid")
    header = _sign(payload, "whsec_test")
    resp = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp.status_code == 200
    record = tenant_store.get_tenant("tenant-paid")
    assert record is not None
    assert record["stripe_customer_id"] == "cus_1"


def test_webhook_duplicate_event_no_double_fulfillment(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = _checkout_completed_payload(event_id="evt_dup", tenant="tenant-dup")
    header = _sign(payload, "whsec_test")
    resp1 = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    resp2 = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp2.json().get("duplicate") is True


def test_webhook_subscription_deleted_downgrades(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    tenant_store.create_tenant("tenant-sub")
    key = tenant_store.create_key("tenant-sub", cap_usd=100.0)
    tenant_store.set_stripe_customer("tenant-sub", "cus_sub")

    payload = json.dumps(
        {
            "id": "evt_sub_deleted",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_1", "customer": "cus_sub", "status": "canceled"}},
        }
    ).encode()
    header = _sign(payload, "whsec_test")
    resp = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp.status_code == 200

    record = tenant_store.get_tenant("tenant-sub")
    assert record["subscription_status"] == "canceled"
    key_record = tenant_store.get_key(key)
    assert key_record.cap_usd == 0


def _checkout_completed_payload_with_tier(
    event_id="evt_tier", tenant="tenant-tier", tier="clinic", payment_status="paid"
):
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": tenant,
                    "payment_status": payment_status,
                    "customer": "cus_tier",
                    "metadata": {"tenant": tenant, "tier": tier},
                }
            },
        }
    ).encode()


def test_webhook_checkout_completed_records_valid_tier(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = _checkout_completed_payload_with_tier(tenant="tenant-tier-ok", tier="clinic")
    header = _sign(payload, "whsec_test")
    resp = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp.status_code == 200
    record = tenant_store.get_tenant("tenant-tier-ok")
    assert record["tier"] == "clinic"
    assert record["plan_active"] is True
    assert tenant_store.get_tenant_cap("tenant-tier-ok") == 2000.0


def test_webhook_checkout_completed_unknown_tier_grants_nothing(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = _checkout_completed_payload_with_tier(
        tenant="tenant-tier-bad", tier="platinum-plus-unknown"
    )
    header = _sign(payload, "whsec_test")
    resp = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp.status_code == 200
    record = tenant_store.get_tenant("tenant-tier-bad")
    assert record is not None  # tenant is still created
    assert record["tier"] is None
    assert record["plan_active"] is False


def test_webhook_checkout_completed_missing_tier_grants_nothing(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = _checkout_completed_payload(event_id="evt_no_tier", tenant="tenant-no-tier")
    header = _sign(payload, "whsec_test")
    resp = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp.status_code == 200
    record = tenant_store.get_tenant("tenant-no-tier")
    assert record["tier"] is None
    assert record["plan_active"] is False


def test_webhook_subscription_deleted_clears_tier(client, monkeypatch, tenant_store):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    tenant_store.create_tenant("tenant-sub-tier")
    tenant_store.set_tier("tenant-sub-tier", "health_system")
    tenant_store.set_stripe_customer("tenant-sub-tier", "cus_sub_tier")

    payload = json.dumps(
        {
            "id": "evt_sub_deleted_tier",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_2", "customer": "cus_sub_tier", "status": "canceled"}},
        }
    ).encode()
    header = _sign(payload, "whsec_test")
    resp = client.post("/billing/webhook", content=payload, headers={"Stripe-Signature": header})
    assert resp.status_code == 200

    assert tenant_store.get_tenant_cap("tenant-sub-tier") is None
    assert tenant_store.get_tenant("tenant-sub-tier")["plan_active"] is False


def test_billing_portal_404_for_unknown_tenant(client):
    resp = client.post("/billing/portal", params={"tenant": "no-such-tenant"})
    assert resp.status_code == 404


def test_billing_portal_success(client, monkeypatch, tenant_store):
    tenant_store.create_tenant("tenant-portal")
    tenant_store.set_stripe_customer("tenant-portal", "cus_portal")

    def fake_portal_create(params):
        assert params["customer"] == "cus_portal"
        return {"url": "https://billing.stripe.example/portal_1"}

    from guardedgateway import billing as billing_module

    monkeypatch.setattr(
        billing_module,
        "create_portal_session",
        lambda customer_id, **kw: billing_module.CheckoutResult(
            status="stripe_session", url="https://billing.stripe.example/portal_1"
        ),
    )
    resp = client.post("/billing/portal", params={"tenant": "tenant-portal"})
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://billing.stripe.example/portal_1"
