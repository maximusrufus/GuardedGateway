from guardedgateway.ledger import LedgerEntry, current_period, get_ledger


def test_record_and_spent_usd():
    ledger = get_ledger()
    ledger.record(
        LedgerEntry(
            tenant="t1",
            api_key="k1",
            provider="openai",
            model="openai/gpt-4o",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.5,
        )
    )
    assert ledger.spent_usd(api_key="k1") == 0.5


def test_spent_usd_scoped_to_key():
    ledger = get_ledger()
    ledger.record(
        LedgerEntry(
            tenant="t1",
            api_key="k1",
            provider="openai",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=1.0,
        )
    )
    ledger.record(
        LedgerEntry(
            tenant="t1",
            api_key="k2",
            provider="openai",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=2.0,
        )
    )
    assert ledger.spent_usd(api_key="k1") == 1.0
    assert ledger.spent_usd(api_key="k2") == 2.0
    assert ledger.spent_usd(tenant="t1") == 3.0


def test_refused_calls_do_not_count_as_spend():
    ledger = get_ledger()
    ledger.record(
        LedgerEntry(
            tenant="t1",
            api_key="k3",
            provider="openai",
            model="m",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            refused=True,
            refusal_reason="spend_cap",
        )
    )
    assert ledger.spent_usd(api_key="k3") == 0.0


def test_audit_rows_never_carry_prompt_text():
    ledger = get_ledger()
    ledger.record(
        LedgerEntry(
            tenant="t1",
            api_key="k4",
            provider="fake",
            model="fake/echo",
            prompt_tokens=1,
            completion_tokens=1,
            cost_usd=0.0,
        )
    )
    rows = ledger.audit_rows(0.0, 9999999999.0)
    assert len(rows) == 1
    keys = set(rows[0].keys())
    assert "prompt" not in keys and "response" not in keys and "content" not in keys


def test_current_period_format():
    assert len(current_period(1735689600.0)) == 7  # 'YYYY-MM'
