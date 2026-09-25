"""Walks every route registered on the app and asserts that any route not in
the explicit PUBLIC_ROUTES allowlist (guardedgateway/app.py) rejects an
unauthenticated request. This is the regression test for the /dashboard
page that used to render every tenant's spend/keys/caps with no
credential."""

from __future__ import annotations

import pytest

from guardedgateway.app import PUBLIC_ROUTES, app


def _iter_routes():
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            yield method, path


@pytest.mark.parametrize("method,path", list(_iter_routes()))
def test_unauthenticated_request_is_denied_unless_allowlisted(client, path, method):
    if (method, path) in PUBLIC_ROUTES:
        pytest.skip(f"{method} {path} is in PUBLIC_ROUTES: {PUBLIC_ROUTES[(method, path)]}")

    if "{tier}" in path:
        # parameterized checkout route is in PUBLIC_ROUTES; concrete calls
        # are covered by tests/test_billing.py
        pytest.skip(f"{method} {path} is a parameterized route already allowlisted")

    if method == "GET":
        resp = client.get(path, follow_redirects=False)
    elif method == "POST":
        resp = client.post(path, follow_redirects=False)
    else:
        pytest.skip(f"unhandled method {method}")

    assert resp.status_code in (401, 403, 404, 422) or (300 <= resp.status_code < 400), (
        f"{method} {path} returned {resp.status_code} with no credentials "
        f"and is not in PUBLIC_ROUTES -- it must reject unauthenticated access"
    )


def test_dashboard_requires_auth(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 401


def test_dashboard_rejects_bad_key(client):
    resp = client.get(
        "/dashboard", headers={"Authorization": "Bearer bogus"}, follow_redirects=False
    )
    assert resp.status_code == 401


def test_dashboard_shows_only_calling_tenant(client, tenant_store):
    key_a = tenant_store.create_key("tenant-a")
    key_b = tenant_store.create_key("tenant-b")

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_a}"},
        json={"model": "fake/echo", "messages": [{"role": "user", "content": "a's message"}]},
    )
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_b}"},
        json={"model": "fake/echo", "messages": [{"role": "user", "content": "b's message"}]},
    )

    resp_a = client.get("/dashboard", headers={"Authorization": f"Bearer {key_a}"})
    assert resp_a.status_code == 200
    assert key_a in resp_a.text
    assert key_b not in resp_a.text

    resp_b = client.get("/dashboard", headers={"Authorization": f"Bearer {key_b}"})
    assert resp_b.status_code == 200
    assert key_b in resp_b.text
    assert key_a not in resp_b.text


def test_audit_shows_only_calling_tenant(client, tenant_store):
    key_a = tenant_store.create_key("tenant-a")
    key_b = tenant_store.create_key("tenant-b")

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_a}"},
        json={"model": "fake/echo", "messages": [{"role": "user", "content": "a's message"}]},
    )
    client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_b}"},
        json={"model": "fake/echo", "messages": [{"role": "user", "content": "b's message"}]},
    )

    resp_a = client.get("/v1/audit", headers={"Authorization": f"Bearer {key_a}"})
    assert resp_a.status_code == 200
    assert key_a in resp_a.text
    assert key_b not in resp_a.text
