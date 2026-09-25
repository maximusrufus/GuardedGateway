"""The price we CHARGE must equal the price we PUBLISH.

Found 2026-09-25 against the live Stripe account: the bootstrap script shipped
prices that had never matched the product spec, the public website, or the
Stripe business description. TradeInvoice would have charged a contractor $79
where the site advertised $24. IDRGateKit's numbers were a copy-paste of
another product's tiers.

These amounts are the published prices on https://www.ripplarity.com and in the
Stripe business description. Changing a price here means changing it there in
the same commit -- that is the entire point of this test.
"""

from scripts.stripe_bootstrap import TIERS

PUBLISHED = {
    "team": (19900, "recurring", "$199/month"),
    "clinic": (49900, "recurring", "$499/month"),
    "health_system": (99900, "recurring", "$999/month"),
}


def test_bootstrap_amounts_match_published_prices():
    for tier, (amount, kind, human) in PUBLISHED.items():
        assert tier in TIERS, f"{tier} missing from TIERS"
        _env, actual_amount, actual_kind = TIERS[tier]
        assert actual_amount == amount, (
            f"{tier}: charging {actual_amount} but publishing {amount} ({human})"
        )
        assert actual_kind == kind, f"{tier}: billing kind drifted"


def test_no_extra_tiers_are_sold_unpublished():
    extra = set(TIERS) - set(PUBLISHED)
    assert not extra, f"tiers present in TIERS but not published anywhere: {extra}"
