import logging


def test_phi_flagged_request_routed_to_non_baa_provider_returns_422(client, tenant_store):
    """openai is baa:false in providers.yaml; a PHI-flagged request to it
    (and no BAA fallback configured) must be refused with 422, never routed."""
    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-PHI": "true"},
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "patient info"}]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "no_baa_provider"


def test_phi_flagged_key_config_also_triggers_422(client, tenant_store):
    api_key = tenant_store.create_key("acme", phi=True)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "patient info"}]},
    )
    assert resp.status_code == 422


def test_phi_flagged_request_to_baa_provider_succeeds(client, tenant_store, install_mock_transport):
    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-PHI": "true"},
        json={
            "model": "ollama/qwen2.5:7b",
            "messages": [{"role": "user", "content": "ssn 123-45-6789"}],
        },
    )
    assert resp.status_code == 200


def test_phi_content_redacted_before_reaching_mocked_upstream(
    client, tenant_store, install_mock_transport
):
    """Assert on what the MOCK TRANSPORT actually received, not the response —
    the SSN/DOB/MRN must never appear in the outbound request body."""
    api_key = tenant_store.create_key("acme")
    ssn = "123-45-6789"
    dob = "DOB: 01/15/1980"
    mrn = "MRN: A9988776"
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-PHI": "true"},
        json={
            "model": "ollama/qwen2.5:7b",
            "messages": [{"role": "user", "content": f"patient ssn {ssn}, {dob}, {mrn}"}],
        },
    )
    assert resp.status_code == 200
    assert len(install_mock_transport) == 1
    sent_body = install_mock_transport[0].content.decode("utf-8")
    assert ssn not in sent_body
    assert "01/15/1980" not in sent_body
    assert "A9988776" not in sent_body
    assert "[REDACTED-PHI]" in sent_body


def test_phi_never_reaches_application_logs(client, tenant_store, install_mock_transport, caplog):
    api_key = tenant_store.create_key("acme")
    ssn = "987-65-4321"
    dob = "DOB: 03/04/1990"
    mrn = "MRN: Z1122334"
    with caplog.at_level(logging.DEBUG, logger="guardedgateway"):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-PHI": "true"},
            json={
                "model": "ollama/qwen2.5:7b",
                "messages": [{"role": "user", "content": f"patient ssn {ssn}, {dob}, {mrn}"}],
            },
        )
    assert resp.status_code == 200
    log_text = caplog.text
    assert ssn not in log_text
    assert "03/04/1990" not in log_text
    assert "Z1122334" not in log_text


def test_non_phi_request_not_redacted(client, tenant_store, install_mock_transport):
    api_key = tenant_store.create_key("acme")
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "ollama/qwen2.5:7b",
            "messages": [{"role": "user", "content": "what is 2+2"}],
        },
    )
    assert resp.status_code == 200
    sent_body = install_mock_transport[0].content.decode("utf-8")
    assert "what is 2+2" in sent_body
