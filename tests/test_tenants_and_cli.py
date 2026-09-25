from guardedgateway import cli


def test_create_key_round_trip(tenant_store):
    api_key = tenant_store.create_key("acme", cap_usd=10.0, phi=True)
    record = tenant_store.get_key(api_key)
    assert record is not None
    assert record.tenant == "acme"
    assert record.cap_usd == 10.0
    assert record.phi is True


def test_unknown_key_returns_none(tenant_store):
    assert tenant_store.get_key("gg-does-not-exist") is None


def test_set_cap_updates_existing_key(tenant_store):
    api_key = tenant_store.create_key("acme")
    ok = tenant_store.set_cap(api_key, 50.0)
    assert ok is True
    assert tenant_store.get_key(api_key).cap_usd == 50.0


def test_set_cap_missing_key_returns_false(tenant_store):
    assert tenant_store.set_cap("gg-nope", 1.0) is False


def test_provider_key_encrypted_round_trip(tenant_store):
    tenant_store.set_provider_key("acme", "openai", "sk-real-secret-value")
    decrypted = tenant_store.get_provider_key("acme", "openai")
    assert decrypted == "sk-real-secret-value"
    # the raw sqlite row must never contain the plaintext key
    with tenant_store._conn as conn:
        row = conn.execute(
            "SELECT encrypted_key FROM provider_keys WHERE tenant='acme' AND provider='openai'"
        ).fetchone()
    assert "sk-real-secret-value" not in row[0]


def test_cli_create_tenant(capsys, tenant_store):
    rc = cli.main(["create-tenant", "cli-tenant"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cli-tenant" in out


def test_cli_create_key_and_set_cap(capsys, tenant_store):
    rc = cli.main(["create-key", "cli-tenant", "--cap", "25"])
    assert rc == 0
    api_key = capsys.readouterr().out.strip()
    assert api_key.startswith("gg-")

    rc2 = cli.main(["set-cap", api_key, "99.5"])
    assert rc2 == 0
    assert tenant_store.get_key(api_key).cap_usd == 99.5


def test_cli_set_cap_unknown_key_errors(capsys, tenant_store):
    rc = cli.main(["set-cap", "gg-unknown", "1.0"])
    assert rc == 1
