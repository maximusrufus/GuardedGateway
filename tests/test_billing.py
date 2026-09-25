import json
import time

from guardedgateway import billing


def test_checkout_not_configured_by_default(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("PAYMENT_LINK_URL", raising=False)
    result = billing.create_checkout_session("team", "tenant-1")
    assert result.status == "not_configured"


def test_checkout_falls_back_to_payment_link(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setenv("PAYMENT_LINK_URL", "https://buy.stripe.com/test123")
    result = billing.create_checkout_session("clinic", "tenant-1")
    assert result.status == "payment_link"
    assert result.url == "https://buy.stripe.com/test123"


def test_checkout_uses_mocked_stripe_when_configured(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_fake")
    monkeypatch.setenv("STRIPE_PRICE_TEAM", "price_fake_team")
    monkeypatch.setenv("APP_BASE_URL", "https://gateway.guardedgateway.example")

    captured = {}

    def fake_create(params):
        captured.update(params)
        return {"url": "https://checkout.stripe.com/fake-session"}

    result = billing.create_checkout_session("team", "tenant-1", stripe_checkout_create=fake_create)
    assert result.status == "stripe_session"
    assert result.url == "https://checkout.stripe.com/fake-session"
    assert captured["line_items"][0]["price"] == "price_fake_team"
    assert captured["client_reference_id"] == "tenant-1"
    assert "payment_method_types" not in captured
    assert captured["integration_identifier"].startswith("guardedgateway-team-")


def test_checkout_customer_email_passed_through(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "rk_test_fake")
    monkeypatch.setenv("STRIPE_PRICE_TEAM", "price_fake_team")

    captured = {}

    def fake_create(params):
        captured.update(params)
        return {"url": "https://checkout.stripe.com/fake-session"}

    billing.create_checkout_session(
        "team", "tenant-1", customer_email="a@example.com", stripe_checkout_create=fake_create
    )
    assert captured["customer_email"] == "a@example.com"


def test_checkout_unknown_tier():
    result = billing.create_checkout_session("nonexistent", "tenant-1")
    assert result.status == "not_configured"


def test_create_portal_session_mocked():
    def fake_create(params):
        assert params["customer"] == "cus_1"
        return {"url": "https://billing.stripe.com/portal_1"}

    result = billing.create_portal_session("cus_1", stripe_portal_create=fake_create)
    assert result.status == "stripe_session"
    assert result.url == "https://billing.stripe.com/portal_1"


def test_create_portal_session_not_configured(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    result = billing.create_portal_session("cus_1")
    assert result.status == "not_configured"


def test_webhook_signature_verification_valid():
    secret = "whsec_test"
    payload = json.dumps({"type": "checkout.session.completed"}).encode()
    ts = str(int(time.time()))
    import hashlib
    import hmac

    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    event = billing.verify_webhook_signature(payload, header, secret)
    assert event is not None
    assert event["type"] == "checkout.session.completed"


def test_webhook_signature_rejects_bad_signature():
    payload = b'{"type": "x"}'
    header = "t=123,v1=deadbeef"
    event = billing.verify_webhook_signature(payload, header, "whsec_test")
    assert event is None


def test_webhook_signature_rejects_stale_timestamp():
    secret = "whsec_test"
    payload = b'{"type": "x"}'
    import hashlib
    import hmac

    stale_ts = str(int(time.time()) - 10000)
    signed = f"{stale_ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={stale_ts},v1={sig}"
    event = billing.verify_webhook_signature(payload, header, secret)
    assert event is None


def test_session_tenant_prefers_client_reference_id():
    session = {"client_reference_id": "tenant-1", "metadata": {"tenant": "fallback"}}
    assert billing.session_tenant(session) == "tenant-1"


def test_session_tenant_falls_back_to_metadata():
    session = {"client_reference_id": None, "metadata": {"tenant": "fallback"}}
    assert billing.session_tenant(session) == "fallback"
