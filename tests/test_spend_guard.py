import pytest

from guardedgateway import spend_guard


def test_estimate_cost_zero_for_free_model():
    est = spend_guard.estimate_cost("fake/echo", prompt_tokens=100, max_tokens=100)
    assert est.cost_usd == 0.0


def test_estimate_cost_nonzero_for_priced_model():
    est = spend_guard.estimate_cost("openai/gpt-4o", prompt_tokens=1000, max_tokens=1000)
    assert est.cost_usd > 0.0


def test_refuse_if_over_cap_none_cap_never_refuses():
    est = spend_guard.refuse_if_over_cap(
        model="openai/gpt-4o",
        prompt_tokens=1_000_000,
        max_tokens=1_000_000,
        spent_usd=0.0,
        cap_usd=None,
    )
    assert est.cost_usd > 0


def test_refuse_if_over_cap_refuses_before_call():
    with pytest.raises(spend_guard.SpendCapExceeded) as exc_info:
        spend_guard.refuse_if_over_cap(
            model="openai/gpt-4o",
            prompt_tokens=100_000,
            max_tokens=100_000,
            spent_usd=0.0,
            cap_usd=1.0,
        )
    assert exc_info.value.cap == 1.0
    assert exc_info.value.would_add > 1.0


def test_zero_cap_denies_all_cloud_calls():
    """Deliberate behavior change from ReviewHouse's spend_guard.py: there,
    cap_usd<=0 means 'skip checks, nothing to guard'. Here cap==0 must mean
    'deny everything with nonzero cost' — GuardedGateway's whole claim is
    'cannot overspend', so a $0 cap is a real zero ceiling, not a no-op."""
    with pytest.raises(spend_guard.SpendCapExceeded):
        spend_guard.refuse_if_over_cap(
            model="openai/gpt-4o", prompt_tokens=10, max_tokens=10, spent_usd=0.0, cap_usd=0.0
        )


def test_zero_cap_allows_free_model():
    """A $0 cap does not block $0.00-cost local calls — 0 + 0 is not > 0."""
    est = spend_guard.refuse_if_over_cap(
        model="fake/echo", prompt_tokens=1000, max_tokens=1000, spent_usd=0.0, cap_usd=0.0
    )
    assert est.cost_usd == 0.0


def test_actual_cost_matches_pricing():
    cost = spend_guard.actual_cost("openai/gpt-4o-mini", prompt_tokens=1000, completion_tokens=1000)
    assert cost == pytest.approx(0.00015 + 0.0006)
