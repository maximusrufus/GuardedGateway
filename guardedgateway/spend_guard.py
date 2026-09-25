"""Pre-call spend cap enforcement.

Ported from an internal spend guard (SpendCapExceeded,
CallCapExceeded, refuse_if_over_cap) — same idea, DELIBERATELY DIFFERENT
zero-cap behavior. See PROVENANCE.md for the full writeup; short version:

The original SpendGuardLLM treats `cap_usd <= 0` as "skip checks" (0 was its
convention for "local backend, nothing to guard"). GuardedGateway is a
gateway whose whole product claim is "cannot overspend" for BYOK cloud
traffic, so here `cap == 0` means the opposite: explicitly deny every
cloud-priced call. This is pinned by `test_zero_cap_denies_all_cloud_calls`.
"""

from __future__ import annotations

from dataclasses import dataclass

from guardedgateway import config


class SpendCapExceeded(RuntimeError):
    """Raised BEFORE any upstream call when the estimated cost of this call
    would push month-to-date spend over the configured cap."""

    def __init__(self, message: str, *, spent: float, cap: float, would_add: float):
        super().__init__(message)
        self.spent = spent
        self.cap = cap
        self.would_add = would_add


class CallCapExceeded(SpendCapExceeded):
    """Raised when a per-key/per-period call-count ceiling is hit. Subclasses
    SpendCapExceeded so callers that only catch the parent still refuse."""


@dataclass
class CostEstimate:
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def estimate_cost(model: str, prompt_tokens: int, max_tokens: int) -> CostEstimate:
    """Pre-call worst-case cost estimate: prices the FULL requested max_tokens
    as completion tokens (the worst case, since the caller doesn't know yet
    how much the model will actually emit)."""
    price = config.price_for_model(model)
    prompt_cost = (prompt_tokens / 1000.0) * float(price.get("prompt_per_1k", 0.0))
    completion_cost = (max_tokens / 1000.0) * float(price.get("completion_per_1k", 0.0))
    return CostEstimate(
        prompt_tokens=prompt_tokens,
        completion_tokens=max_tokens,
        cost_usd=prompt_cost + completion_cost,
    )


def actual_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = config.price_for_model(model)
    prompt_cost = (prompt_tokens / 1000.0) * float(price.get("prompt_per_1k", 0.0))
    completion_cost = (completion_tokens / 1000.0) * float(price.get("completion_per_1k", 0.0))
    return prompt_cost + completion_cost


def refuse_if_over_cap(
    *,
    model: str,
    prompt_tokens: int,
    max_tokens: int,
    spent_usd: float,
    cap_usd: float | None,
) -> CostEstimate:
    """Returns the CostEstimate if the call is allowed. Raises
    SpendCapExceeded (with zero upstream calls made) if it is not.

    `cap_usd=None` means "no cap configured" — never refuses on cost grounds
    (a deployment might rely solely on the cloud-block gate instead). This is
    DIFFERENT from `cap_usd=0.0`, which refuses EVERY call that has any
    nonzero estimated cost (i.e. every cloud call; local `fake`/`ollama`
    calls priced at $0.00 still pass, since 0 + 0 is not > 0).
    """
    estimate = estimate_cost(model, prompt_tokens, max_tokens)
    if cap_usd is None:
        return estimate
    if spent_usd + estimate.cost_usd > cap_usd:
        raise SpendCapExceeded(
            f"spend cap ${cap_usd:.4f} for this key/period: already spent "
            f"${spent_usd:.4f}, this call could cost up to ${estimate.cost_usd:.4f}, "
            "which would breach it. Refusing before any upstream call.",
            spent=spent_usd,
            cap=cap_usd,
            would_add=estimate.cost_usd,
        )
    return estimate
