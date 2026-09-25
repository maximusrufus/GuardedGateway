# GuardedGateway

GuardedGateway is an OpenAI-compatible LLM gateway that stands in front of your
provider calls to enforce a spend cap and a PHI/BAA policy.

**The differentiator:** it refuses a call *before* it reaches the provider when
that call would breach a dollar cap (`402 spend_cap`, upstream never contacted —
gate order and code: `guardedgateway/app.py`'s `_call_with_gates`), it redacts
PHI out of the prompt for any request flagged `X-PHI: true` or a `phi: true` key
(`guardedgateway/phi.py`, applied before routing, logging, or the upstream call),
and it routes PHI-flagged traffic only to providers you've marked `baa: true` in
`providers.yaml`, refusing with `422 no_baa_provider` rather than silently
falling back to a non-BAA vendor. All three are enforced in the same request
path documented at the top of `guardedgateway/app.py` and covered by
`tests/test_app_cap_and_cloud.py` and `tests/test_app_phi_routing.py`.

Other things it does:

- **A durable, month-to-date SQLite spend ledger**, per API key, per tenant, per
  provider — survives restarts, not an in-memory counter that resets. (Durability
  depends on where the SQLite file lives — see "Hosted storage" below.)
- **Response cache + per-provider circuit breaker**, with configurable fallback
  chains (`providers.yaml`).
- **`GET /v1/audit`** — a JSONL export of hashes, costs, providers, redaction counts.
  Never raw prompt/response content, because that content is never written to the
  ledger table in the first place.

## 60-second quickstart

```bash
pip install -e ".[dev]"
cp .env.example .env   # optional — safe defaults with none

# create a tenant + API key
python -m guardedgateway.cli create-tenant acme
python -m guardedgateway.cli create-key acme --cap 25.0

uvicorn guardedgateway.app:app --port 8000
```

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <api-key-from-above>" \
  -H "Content-Type: application/json" \
  -d '{"model": "fake/echo", "messages": [{"role": "user", "content": "hello"}]}'
```

For a real local model, run [Ollama](https://ollama.com) and use `model: "ollama/qwen2.5:7b"`.
Cloud providers (`openai/`, `anthropic/`, `gemini/`) are **BYOK and default-deny**:
set `GG_ALLOW_CLOUD=1` and configure a spend cap before any cloud call will be attempted.

(Every command above was run against this repo as part of writing this README —
see the commit message for what, if anything, needed fixing.)

## Pricing

Free, Apache-2.0, self-hosted — clone it, run it, no license fee. Hosted tiers
(you skip running it yourself) are $199/$499/$999/mo; see `guardedgateway/billing.py`
and the Stripe section below for what each tier wires up.

## How this differs from the alternatives

GuardedGateway is narrow on purpose: pre-call cost refusal + PHI-aware BAA
routing for a regulated (or just cost-anxious) deployment. If you don't need
either of those, a general multi-provider proxy is probably simpler for you.
The only competitor facts below are ones we can point at a public pricing page
for — we haven't independently verified anything else about how these tools
work internally, so we're not claiming to know their feature lists in detail.

- **[LiteLLM](https://www.litellm.ai/)** — budgets are free and self-hosted,
  same as GuardedGateway's caps. If you want the widest provider/SDK coverage
  and don't need PHI redaction or BAA-only routing, LiteLLM is a reasonable,
  more mature choice; see `docs/guardedgateway-vs-litellm.md`.
- **[OpenRouter](https://openrouter.ai/docs/api-reference/limits)** — a hosted
  multi-model router charging roughly a 5.5% credit fee; you don't run
  anything yourself. If you're fine sending traffic through a third-party
  hosted router and don't need PHI/BAA controls, it's less operational work
  than self-hosting GuardedGateway; see `docs/guardedgateway-vs-openrouter.md`.
- **[Cloudflare AI Gateway](https://developers.cloudflare.com/ai-gateway/)** —
  free, with configurable spend limits, and it's already where a lot of teams'
  edge traffic runs. If you're already on Cloudflare's stack and don't need
  PHI/BAA routing, it's likely the path of least resistance; see
  `docs/guardedgateway-vs-cloudflare-ai-gateway.md`.
- **[Portkey](https://portkey.ai/pricing)** — a Developer free tier and a
  Production tier at $49/mo, with a broad observability/guardrails feature
  set. If you want a hosted product with a UI and don't need self-hosting or
  BAA-only routing, Portkey's paid tier is worth a look; see
  `docs/guardedgateway-vs-portkey.md`.

## Hosted storage — durability status

The public staging deployment (see "Live staging" below) runs on Cloud Run
with `min-instances 0` and **no persistent volume or Postgres attached yet** —
the SQLite ledger there is ephemeral and will be lost on a cold start/redeploy.
Self-hosted deployments are durable as long as you mount `GG_DB_PATH` (default
`data/guardedgateway.db`) on a persistent volume, which the `Dockerfile`
documents and `docker-compose.yml` does by default. This is a real, current
limitation of the *hosted* offering, not of the code.

## Model routing

`model` is always `"<provider>/<name>"`: `fake/echo`, `ollama/qwen2.5:7b`,
`openai/gpt-4o`, `anthropic/claude-sonnet`, `gemini/gemini-2.5-pro`.

## Pricing & providers config

`guardedgateway/pricing.yaml` and `guardedgateway/providers.yaml` ship with the
package and can be overridden per-deployment via `GG_PRICING_FILE` /
`GG_PROVIDERS_FILE`. Prices are labeled "as of 2026-09" — cloud prices drift; update
the file for your own account rather than trusting it as a live feed.

## Zero cap means zero cloud spend

`cap == 0` is a deliberate, explicit **deny-all** for any nonzero-cost call — not a
no-op. See `guardedgateway/spend_guard.py` and `PROVENANCE.md` for why this differs
from the an internal module code it was ported from.

## Stripe

- **Key management**: `STRIPE_SECRET_KEY` should be a **restricted key**
  (`rk_...`) scoped to only what this app needs (Checkout Sessions write,
  Billing Portal write, Customers read, Subscriptions read, Webhook
  Endpoints read) -- never a full secret key. In production, source it from
  **Google Secret Manager**, not a committed `.env`. `scripts/check_no_stripe_keys.py`
  (wired into `.pre-commit-config.yaml`) fails the build if a live/test
  secret is ever committed.
- **Bootstrap**: `python scripts/stripe_bootstrap.py` idempotently creates
  one Stripe Product per tier (team, clinic, health_system) plus their
  Prices, and a webhook endpoint if `STRIPE_WEBHOOK_URL` is set.
- **Webhook events subscribed** (`POST /billing/webhook`):
  `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
  `checkout.session.async_payment_failed`, `customer.subscription.created`,
  `customer.subscription.updated`, `customer.subscription.deleted`,
  `invoice.paid`, `invoice.payment_failed`. Every event is signature-verified
  first (400 on failure) and idempotency-deduped by event id before any
  tenant state changes.
- **Customer Portal**: `POST /billing/portal?tenant=...` returns a
  Stripe-hosted Customer Portal URL for a tenant with a stored Stripe
  customer id.
- **`APP_BASE_URL`** builds Checkout/Portal success, cancel, and return
  URLs -- set it in every real environment (defaults to `http://localhost:8000`
  for local dev only).
- **Tax**: enable Stripe Tax + register in each jurisdiction before charging
  US/EU customers -- `automatic_tax` is **not** enabled by default and
  Stripe silently collects no tax without an active registration.

## Admin CLI

```bash
guardedgateway create-tenant <name>
guardedgateway create-key <tenant> [--cap 25.0] [--phi] [--inject-guard]
guardedgateway set-cap <api-key> <new-cap>
```

## Dashboard

`GET /dashboard` — spend by key/model this period, global cap, circuit breaker state.

## Tests

```bash
pytest -v          # all upstream calls are mocked via httpx.MockTransport — never real network
ruff check .
```

## License

Apache-2.0. See `LICENSE`.

## Provenance

Several modules port logic (gate order, redaction patterns, circuit-breaker shape)
from other local repos, adapted for a standalone open-source gateway. See
`PROVENANCE.md` for the full file-by-file list and what changed.

## Live staging

https://guardedgateway-udrj5akpma-uc.a.run.app (Cloud Run, us-central1, project ripplarity-products (Ripplarity Inc), min-instances 0; ephemeral storage until a volume or Postgres is configured; Stripe not yet configured).

Liveness path in production is `GET /health`, not `/healthz` — Google Front End on Cloud Run intercepts the exact path `/healthz` and returns its own 404 page before the request ever reaches the container, so a monitor pointed at `/healthz` will misreport the service as down. `/healthz` is still registered for other environments; point Cloud Run health checks and uptime monitors at `/health`.
