# GuardedGateway

An OpenAI-compatible LLM gateway with one job: **it cannot overspend, and it cannot leak PHI.**

- **Hard dollar caps, enforced before any upstream call.** Every request is priced
  against `pricing.yaml` *before* it leaves the process. If the estimated cost would
  breach the cap, the call is refused with `402 {"error": "spend_cap", ...}` and the
  upstream provider is never contacted.
- **A durable, month-to-date SQLite spend ledger**, per API key, per tenant, per
  provider — survives restarts, not an in-memory counter that resets.
- **PHI redaction** on any request flagged `X-PHI: true` (or a key configured
  `phi: true`) — SSNs, MRNs, DOBs, phone numbers, emails, addresses, names are
  redacted to `[REDACTED-PHI]` before the prompt is used for routing, logging, or
  the upstream call itself.
- **BAA-only routing for PHI-flagged traffic.** `providers.yaml` marks which
  providers you've actually signed a Business Associate Agreement with (`baa: true`).
  A PHI-flagged request that can't reach a BAA-covered provider is refused with
  `422 {"error": "no_baa_provider"}` — it is never silently routed to a non-BAA vendor.
- **Response cache + per-provider circuit breaker**, with configurable fallback
  chains (`providers.yaml`).
- **`GET /v1/audit`** — a JSONL export of hashes, costs, providers, redaction counts.
  Never raw prompt/response content, because that content is never written to the
  ledger table in the first place.

## Why not Cloudflare AI Gateway or LiteLLM?

Cloudflare AI Gateway has free spend limits. LiteLLM has budgets. Neither does PHI
redaction, BAA-only routing, or refuses *before* the call rather than after it, and
GuardedGateway is self-hostable end to end (SQLite, no external dependency).

## Quickstart

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
