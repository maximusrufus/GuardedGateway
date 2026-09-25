"""Local-first + cloud-spend-block gates, checked before ANY upstream call.

Ported from ReviewHouse's `reviewhouse/local_first.py` (LocalFirstRequired)
and the `CloudSpendBlocked`/`cloud_llm_allowed()` pair in
`reviewhouse/llm.py`. Adapted for a live request-serving gateway rather than
a batch eval script:

ReviewHouse's `local_first.require_local_first()` keys off "has this SCRIPT
(sys.argv[0]) recorded a recent local run" — a receipt file per task. That
concept doesn't map onto a gateway serving concurrent requests from many
callers with no single "task identity", so GuardedGateway's local-first gate
is the coarser but unambiguous gateway-appropriate version: a cloud-prefixed
model is refused outright unless `GG_ALLOW_CLOUD=1` is set for this process
AND a spend cap is configured (env `GG_MONTHLY_CAP_USD`, or a per-key cap on
the request's ApiKey record) — both conditions enforced together, so cloud
traffic cannot be "unblocked" without also being capped. This is documented
here as a deliberate simplification, not a silent behavior change — see
PROVENANCE.md.
"""

from __future__ import annotations

import os


class CloudSpendBlocked(RuntimeError):
    """Raised when a cloud-prefixed model is requested but GG_ALLOW_CLOUD is
    not set to '1', or no spend cap is configured for the call. DEFAULT-DENY:
    building, testing, and a fresh deploy must never be able to bill a cloud
    provider by accident."""


LOCAL_PROVIDERS = {"fake", "ollama"}
CLOUD_PROVIDERS = {"openai", "anthropic", "gemini"}


def cloud_allowed_env() -> bool:
    return os.environ.get("GG_ALLOW_CLOUD") == "1"


def require_cloud_allowed(provider: str, *, cap_usd: float | None) -> None:
    """Raises CloudSpendBlocked before any network call if `provider` is a
    cloud provider and either the env flag is off or no cap is configured."""
    if provider not in CLOUD_PROVIDERS:
        return
    if not cloud_allowed_env():
        raise CloudSpendBlocked(
            f"cloud provider '{provider}' is BLOCKED by default (local-first / "
            "cloud-spend-block gate). Set GG_ALLOW_CLOUD=1 to allow BYOK cloud "
            "calls for this deployment, and configure a spend cap "
            "(GG_MONTHLY_CAP_USD or a per-key cap). Use 'ollama/...' or "
            "'fake/echo' for local, zero-cost testing."
        )
    if cap_usd is None:
        raise CloudSpendBlocked(
            f"cloud provider '{provider}' is allowed (GG_ALLOW_CLOUD=1) but no "
            "spend cap is configured for this key/deployment. GuardedGateway "
            "refuses to make BYOK cloud calls with no dollar ceiling — set "
            "GG_MONTHLY_CAP_USD or a per-key cap."
        )
