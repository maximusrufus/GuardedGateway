"""FastAPI application: OpenAI-compatible chat/embeddings endpoints, spend
ledger enforcement, PHI/BAA routing, response cache, circuit breaker,
dashboard + landing pages, and the audit export.

Gate order on every /v1/chat/completions and /v1/embeddings call, each one
refusing BEFORE any upstream network call:
  1. auth (unknown API key -> 401)
  2. PHI routing (X-PHI header or key.phi -> must pick a baa:true provider,
     else 422 no_baa_provider) — prompt redacted before logging either way
  3. local-first / cloud-block gate (cloud provider + GG_ALLOW_CLOUD unset,
     or no cap configured -> refused)
  4. spend cap pre-flight estimate (estimate > cap -> 402 spend_cap)
  5. circuit breaker (provider open -> try configured fallback chain)
  6. response cache (hit -> skip upstream entirely)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from guardedgateway import (
    billing,
    cache,
    cloud_gate,
    config,
    injection_guard,
    phi,
    providers,
    spend_guard,
)
from guardedgateway.ledger import LedgerEntry, current_period, get_ledger
from guardedgateway.tenants import get_tenant_store

logger = logging.getLogger("guardedgateway")

app = FastAPI(title="GuardedGateway", version="0.1.0")

# Routes that intentionally serve without an API key. Every route NOT listed
# here must reject an unauthenticated request (401/403/404/redirect) --
# enforced by tests/test_public_routes_allowlist.py, which walks every
# route FastAPI has registered and fails closed on anything missing here.
PUBLIC_ROUTES = {
    ("GET", "/healthz"): "liveness probe, no data",
    ("GET", "/health"): (
        "liveness probe, no data; /healthz is intercepted by Google Front End on "
        "Cloud Run and never reaches the container, /health is the reachable "
        "liveness path in production"
    ),
    ("GET", "/"): "marketing landing page",
    ("POST", "/billing/webhook"): "Stripe webhook, verified by HMAC signature not API key",
    ("GET", "/billing/success"): "static post-payment page, no tenant data",
    ("GET", "/billing/cancel"): "static post-cancel page, no tenant data",
    ("POST", "/billing/checkout/{tier}"): (
        "starts a Stripe Checkout session for a caller-supplied tenant name; "
        "returns a checkout URL only, reads no spend/key/tenant data"
    ),
    ("GET", "/openapi.json"): "FastAPI auto-generated API schema, no tenant data",
    ("GET", "/docs"): "FastAPI auto-generated Swagger UI, no tenant data",
    (
        "GET",
        "/docs/oauth2-redirect",
    ): "FastAPI auto-generated Swagger UI helper page, no tenant data",
    ("GET", "/redoc"): "FastAPI auto-generated ReDoc UI, no tenant data",
}

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_response_cache = cache.ResponseCache(
    ttl_seconds=float(os.environ.get("GG_CACHE_TTL_SECONDS", "300"))
)
_cb_cfg = config.circuit_breaker_config()
_breaker = cache.CircuitBreaker(
    failure_threshold=_cb_cfg["failure_threshold"], backoff_seconds=_cb_cfg["backoff_seconds"]
)


def get_response_cache() -> cache.ResponseCache:
    return _response_cache


def get_breaker() -> cache.CircuitBreaker:
    return _breaker


def set_breaker(breaker: cache.CircuitBreaker) -> None:
    global _breaker
    _breaker = breaker


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    max_tokens: int = 512
    temperature: float = 0.0
    stream: bool = False


class EmbeddingsRequest(BaseModel):
    model: str
    input: str | list[str]


def _resolve_api_key_record(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error": "missing_api_key"})
    api_key = authorization[len("Bearer ") :].strip()
    record = get_tenant_store().get_key(api_key)
    if record is None:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    return record


def _global_cap() -> float | None:
    raw = os.environ.get("GG_MONTHLY_CAP_USD")
    return float(raw) if raw else None


def _effective_cap(key_cap: float | None, tenant: str | None = None) -> float | None:
    """A per-key cap always wins if set; otherwise fall back to the tenant's
    tier-derived default cap; otherwise the global env default. `None` from
    all three means no cap is configured."""
    if key_cap is not None:
        return key_cap
    if tenant is not None:
        tenant_cap = get_tenant_store().get_tenant_cap(tenant)
        if tenant_cap is not None:
            return tenant_cap
    return _global_cap()


def _provider_and_model(model: str) -> tuple[str, str]:
    if "/" not in model:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_model", "message": "model must be 'provider/name'"},
        )
    provider, name = model.split("/", 1)
    return provider, name


def _redacted_messages_for_log(messages: list[dict]) -> list[dict]:
    return [
        {
            "role": m["role"],
            "content": phi.redact_phi(m["content"])[0] if m["content"] else m["content"],
        }
        for m in messages
    ]


def _pick_provider_chain(model: str, phi_flagged: bool) -> list[str]:
    """[requested_provider, *fallbacks], filtered to baa:true providers if
    phi_flagged. Raises HTTPException(422) if phi_flagged and nothing in the
    chain is BAA-covered."""
    provider, _name = _provider_and_model(model)
    providers_cfg = config.load_providers().get("providers", {})
    if provider not in providers_cfg:
        raise HTTPException(
            status_code=400, detail={"error": "unknown_provider", "provider": provider}
        )
    chain = [provider] + list(providers_cfg[provider].get("fallbacks", []))
    if phi_flagged:
        chain = [p for p in chain if config.provider_is_baa(p)]
        if not chain:
            raise HTTPException(status_code=422, detail={"error": "no_baa_provider"})
    return chain


async def _call_with_gates(
    *,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    key_record,
    phi_flagged: bool,
    redaction_count: int,
) -> tuple[providers.CompletionResult, dict[str, Any]]:
    """Runs the full gate chain and returns (result, meta) where meta carries
    provider/model/cache_hit/cost for the caller to build a response + ledger
    entry from. Raises HTTPException on any refusal — always BEFORE calling
    `providers.dispatch`."""
    chain = _pick_provider_chain(model, phi_flagged)
    requested_provider, model_name = _provider_and_model(model)

    ckey = cache.cache_key(model, messages, max_tokens, temperature)
    cached = _response_cache.get(ckey)
    if cached is not None:
        return cached, {
            "provider": requested_provider,
            "model": model,
            "cache_hit": True,
            "prompt_tokens": cached.prompt_tokens,
            "completion_tokens": cached.completion_tokens,
            "cost_usd": 0.0,
        }

    # NOTE: cloud-block and spend-cap are POLICY refusals for the specific
    # provider requested — they must never be silently absorbed by falling
    # back to a free local provider (that would defeat "refused by default"
    # and "cannot overspend"). Only a CONNECTIVITY problem (circuit open, or
    # the upstream call itself failing) advances to the next entry in the
    # fallback chain. Policy refusals raise immediately, chain index 0 only.
    last_error: Exception | None = None
    for idx, provider_name in enumerate(chain):
        cap = _effective_cap(key_record.cap_usd, tenant=key_record.tenant)
        try:
            cloud_gate.require_cloud_allowed(provider_name, cap_usd=cap)
        except cloud_gate.CloudSpendBlocked as e:
            if idx == 0:
                raise HTTPException(
                    status_code=403, detail={"error": "cloud_blocked", "message": str(e)}
                ) from e
            last_error = e
            continue

        prompt_chars = sum(len(m["content"]) for m in messages)
        prompt_tokens_estimate = max(1, prompt_chars // 4)
        spent = get_ledger().spent_usd(api_key=key_record.api_key)
        full_model_id = f"{provider_name}/{model_name}"
        try:
            estimate = spend_guard.refuse_if_over_cap(
                model=full_model_id,
                prompt_tokens=prompt_tokens_estimate,
                max_tokens=max_tokens,
                spent_usd=spent,
                cap_usd=cap,
            )
        except spend_guard.SpendCapExceeded as e:
            if idx == 0:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "spend_cap",
                        "spent": e.spent,
                        "cap": e.cap,
                        "would_add": e.would_add,
                    },
                ) from e
            last_error = e
            continue

        if _breaker.is_open(provider_name):
            last_error = RuntimeError(f"circuit open for provider {provider_name}")
            continue

        api_key_for_provider = None
        if provider_name not in cloud_gate.LOCAL_PROVIDERS:
            api_key_for_provider = get_tenant_store().get_provider_key(
                key_record.tenant, provider_name
            )

        try:
            result = await providers.dispatch(
                provider_name,
                model_name,
                messages,
                max_tokens,
                api_key=api_key_for_provider,
                ollama_base_url=os.environ.get("GG_OLLAMA_URL", "http://localhost:11434"),
            )
            _breaker.record_success(provider_name)
        except providers.UpstreamError as e:
            _breaker.record_failure(provider_name)
            last_error = e
            continue

        _response_cache.set(ckey, result)
        cost = spend_guard.actual_cost(
            full_model_id, result.prompt_tokens, result.completion_tokens
        )
        return result, {
            "provider": provider_name,
            "model": full_model_id,
            "cache_hit": False,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": cost,
            "estimate": estimate,
        }

    if isinstance(last_error, spend_guard.SpendCapExceeded):
        raise HTTPException(
            status_code=402,
            detail={
                "error": "spend_cap",
                "spent": last_error.spent,
                "cap": last_error.cap,
                "would_add": last_error.would_add,
            },
        )
    if isinstance(last_error, cloud_gate.CloudSpendBlocked):
        raise HTTPException(
            status_code=403, detail={"error": "cloud_blocked", "message": str(last_error)}
        )
    raise HTTPException(
        status_code=502, detail={"error": "upstream_unavailable", "message": str(last_error)}
    )


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    authorization: str | None = Header(default=None),
    x_phi: str | None = Header(default=None, alias="X-PHI"),
):
    key_record = _resolve_api_key_record(authorization)
    phi_flagged = key_record.phi or (x_phi or "").lower() == "true"

    raw_messages = [{"role": m.role, "content": m.content} for m in body.messages]
    redaction_count = 0
    messages_for_upstream = raw_messages
    if phi_flagged:
        redacted = []
        total = 0
        for m in raw_messages:
            text, count, _by_label = phi.redact_phi(m["content"])
            redacted.append({"role": m["role"], "content": text})
            total += count
        messages_for_upstream = redacted
        redaction_count = total

    if key_record.inject_guard:
        guarded = []
        for m in messages_for_upstream:
            if m["role"] == "user":
                text, _findings = injection_guard.redact(m["content"])
                guarded.append({"role": m["role"], "content": text})
            else:
                guarded.append(m)
        messages_for_upstream = guarded

    # NEVER log raw content — only the redacted/guarded version, and only at
    # debug level with category counts, never full text for PHI-flagged calls.
    logger.debug(
        "chat_completions request tenant=%s phi=%s redactions=%s",
        key_record.tenant,
        phi_flagged,
        redaction_count,
    )

    result, meta = await _call_with_gates(
        model=body.model,
        messages=messages_for_upstream,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        key_record=key_record,
        phi_flagged=phi_flagged,
        redaction_count=redaction_count,
    )

    get_ledger().record(
        LedgerEntry(
            tenant=key_record.tenant,
            api_key=key_record.api_key,
            provider=meta["provider"],
            model=meta["model"],
            prompt_tokens=meta["prompt_tokens"],
            completion_tokens=meta["completion_tokens"],
            cost_usd=meta["cost_usd"],
            phi_flagged=phi_flagged,
            redaction_count=redaction_count,
            cache_hit=meta["cache_hit"],
        )
    )

    response_body = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": meta["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.prompt_tokens + result.completion_tokens,
        },
        "guardedgateway": {
            "cost_usd": meta["cost_usd"],
            "cache_hit": meta["cache_hit"],
            "phi_redactions": redaction_count,
        },
    }

    if body.stream:

        async def _sse():
            chunk = {
                "id": response_body["id"],
                "object": "chat.completion.chunk",
                "created": response_body["created"],
                "model": meta["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": result.text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            done_chunk = {
                "id": response_body["id"],
                "object": "chat.completion.chunk",
                "created": response_body["created"],
                "model": meta["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    return JSONResponse(response_body)


@app.post("/v1/embeddings")
async def embeddings(
    body: EmbeddingsRequest,
    authorization: str | None = Header(default=None),
    x_phi: str | None = Header(default=None, alias="X-PHI"),
):
    key_record = _resolve_api_key_record(authorization)
    phi_flagged = key_record.phi or (x_phi or "").lower() == "true"
    inputs = body.input if isinstance(body.input, list) else [body.input]

    redaction_count = 0
    redacted_inputs = []
    for text in inputs:
        if phi_flagged:
            red, count, _by_label = phi.redact_phi(text)
            redacted_inputs.append(red)
            redaction_count += count
        else:
            redacted_inputs.append(text)

    messages = [{"role": "user", "content": "\n".join(redacted_inputs)}]
    result, meta = await _call_with_gates(
        model=body.model,
        messages=messages,
        max_tokens=1,
        temperature=0.0,
        key_record=key_record,
        phi_flagged=phi_flagged,
        redaction_count=redaction_count,
    )

    get_ledger().record(
        LedgerEntry(
            tenant=key_record.tenant,
            api_key=key_record.api_key,
            provider=meta["provider"],
            model=meta["model"],
            prompt_tokens=meta["prompt_tokens"],
            completion_tokens=meta["completion_tokens"],
            cost_usd=meta["cost_usd"],
            phi_flagged=phi_flagged,
            redaction_count=redaction_count,
            cache_hit=meta["cache_hit"],
        )
    )

    fake_vector = [float((hash(v) % 1000) / 1000.0) for v in redacted_inputs[0][:16]] or [0.0]
    return JSONResponse(
        {
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": fake_vector}],
            "model": meta["model"],
            "usage": {"prompt_tokens": result.prompt_tokens, "total_tokens": result.prompt_tokens},
        }
    )


@app.get("/v1/audit")
async def audit(
    from_: float | None = None,
    to: float | None = None,
    authorization: str | None = Header(default=None),
):
    """JSONL of ledger rows — hashes/costs/providers/redaction counts.
    NEVER raw prompt/response content, because that content is never
    written to the ledger table in the first place. Scoped to the calling
    tenant only -- never another tenant's rows."""
    key_record = _resolve_api_key_record(authorization)
    ts_from = from_ if from_ is not None else 0.0
    ts_to = to if to is not None else time.time()
    rows = get_ledger().audit_rows(ts_from, ts_to, tenant=key_record.tenant)
    lines = "\n".join(json.dumps(r) for r in rows)
    return PlainTextResponse(lines, media_type="application/x-ndjson")


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {"tiers": billing.TIER_PRICES_USD})


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, authorization: str | None = Header(default=None)):
    """Tenant spend/keys/cap dashboard. Requires the same Bearer API-key
    auth as the JSON API and shows ONLY the calling tenant's rows -- never
    another tenant's spend, keys, or breaker state."""
    key_record = _resolve_api_key_record(authorization)
    period = current_period()
    rows = get_ledger().audit_rows(0.0, time.time(), tenant=key_record.tenant)
    period_rows = [r for r in rows if r["period"] == period]
    spend_by_key: dict[str, float] = {}
    spend_by_model: dict[str, float] = {}
    for r in period_rows:
        if r["refused"]:
            continue
        spend_by_key[r["api_key"]] = spend_by_key.get(r["api_key"], 0.0) + r["cost_usd"]
        spend_by_model[r["model"]] = spend_by_model.get(r["model"], 0.0) + r["cost_usd"]
    breaker_state = {
        provider: {
            "open": _breaker.is_open(provider),
            "failures": st.failures,
        }
        for provider, st in _breaker._states.items()
    }
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "period": period,
            "spend_by_key": spend_by_key,
            "spend_by_model": spend_by_model,
            "global_cap": _effective_cap(key_record.cap_usd, tenant=key_record.tenant),
            "breaker_state": breaker_state,
        },
    )


@app.get("/billing/success", response_class=HTMLResponse)
async def billing_success(request: Request):
    return templates.TemplateResponse(request, "billing_success.html", {})


@app.get("/billing/cancel", response_class=HTMLResponse)
async def billing_cancel(request: Request):
    return templates.TemplateResponse(request, "billing_cancel.html", {})


@app.post("/billing/checkout/{tier}")
async def checkout(tier: str, tenant: str, request: Request):
    customer_email = request.query_params.get("customer_email")
    result = billing.create_checkout_session(tier, tenant, customer_email=customer_email)
    return JSONResponse({"status": result.status, "url": result.url, "message": result.message})


@app.post("/billing/portal")
async def billing_portal(authorization: str | None = Header(default=None)):
    """Open the Stripe Customer Portal for the CALLING tenant.

    The tenant is taken from the authenticated API key and never from a
    request parameter. A caller-supplied tenant name here was an
    authorization hole: tenant names are human-chosen and guessable, and a
    Customer Portal session lets the holder read invoices, change the
    payment method and cancel the subscription."""
    key_record = _resolve_api_key_record(authorization)
    store = get_tenant_store()
    record = store.get_tenant(key_record.tenant)
    if not record or not record.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail={"error": "no_stripe_customer_for_tenant"})
    result = billing.create_portal_session(record["stripe_customer_id"])
    return JSONResponse({"status": result.status, "url": result.url, "message": result.message})


_SUBSCRIPTION_TERMINAL_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


def _fulfill_checkout_session(store, session: dict) -> None:
    """Only ever called after `payment_status != 'unpaid'` has been
    confirmed by the webhook handler."""
    tenant = billing.session_tenant(session)
    if not tenant:
        return
    store.create_tenant(tenant)
    customer_id = session.get("customer")
    if customer_id:
        store.set_stripe_customer(tenant, customer_id)

    # Resolve + validate the tier server-side. A missing or unrecognized
    # tier grants NOTHING -- never default a tenant into any tier, and
    # above all never into the most expensive one.
    tier = billing.session_tier(session)
    if tier and tier in billing.TIER_PRICES_USD:
        store.set_tier(tenant, tier)
    else:
        logger.warning(
            "checkout fulfilled for tenant=%s with unresolvable tier=%r; granting no tier",
            tenant,
            tier,
        )


def _handle_subscription_event(store, event_type: str, obj: dict) -> None:
    customer_id = obj.get("customer")
    if not customer_id:
        return
    record = store.resolve_tenant_by_customer_id(customer_id)
    if not record:
        return
    tenant = record["name"]
    if event_type == "customer.subscription.deleted":
        store.set_subscription_state(tenant, obj.get("id"), "canceled")
        store.revoke_tenant_keys(tenant)
        return
    status = obj.get("status")
    store.set_subscription_state(tenant, obj.get("id"), status)
    if event_type == "invoice.payment_failed":
        store.revoke_tenant_keys(tenant)


@app.post("/billing/webhook")
async def stripe_webhook(
    request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")
):
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=400, detail={"error": "webhook_not_configured"})
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail={"error": "missing_signature"})
    event = billing.verify_webhook_signature(payload, stripe_signature, secret)
    if event is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_signature"})

    store = get_tenant_store()
    event_id = event.get("id") or ""
    event_type = event.get("type", "")
    if event_id and not store.mark_event_processed(event_id, event_type):
        return JSONResponse({"received": True, "duplicate": True})

    obj = event.get("data", {}).get("object", {})

    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") != "unpaid":
            _fulfill_checkout_session(store, obj)
    elif event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _handle_subscription_event(store, event_type, obj)
    elif event_type == "invoice.paid":
        subscription_id = obj.get("subscription")
        if subscription_id:
            _handle_subscription_event(
                store,
                event_type,
                {"customer": obj.get("customer"), "id": subscription_id, "status": "active"},
            )
    elif event_type == "invoice.payment_failed":
        subscription_id = obj.get("subscription")
        if subscription_id:
            _handle_subscription_event(
                store,
                event_type,
                {"customer": obj.get("customer"), "id": subscription_id, "status": "past_due"},
            )

    return JSONResponse({"received": True})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "ok"}
