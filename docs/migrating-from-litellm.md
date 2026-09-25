# Migrating from LiteLLM

This is a mapping guide, not a claim that you should migrate — see
`docs/guardedgateway-vs-litellm.md` for when LiteLLM is the better fit. If you
specifically need pre-call cost refusal and PHI-aware BAA routing, here's how
the pieces line up.

## Request shape

Both are OpenAI-request-shape compatible, so your client code (`model`,
`messages`, `max_tokens`, `temperature`) doesn't change. What changes:

| LiteLLM | GuardedGateway |
|---|---|
| `model: "gpt-4o"` (provider often implicit/configured) | `model: "openai/gpt-4o"` — provider is always explicit in the model string (`_provider_and_model` in `app.py`) |
| Budgets/keys via LiteLLM's virtual key config | `python -m guardedgateway.cli create-tenant <name>` then `create-key <tenant> --cap <usd>` |
| Router/fallback config | `guardedgateway/providers.yaml`'s `fallbacks:` list per provider |

## Step by step

1. **Stand up GuardedGateway** (see the README quickstart) — `pip install -e ".[dev]"`,
   create a tenant + key, run `uvicorn guardedgateway.app:app --port 8000`.

2. **Set your cap.** GuardedGateway's cap is enforced *before* the call, not
   just tracked after — decide your monthly ceiling and pass `--cap` at key
   creation, or set `GG_MONTHLY_CAP_USD` as a global default.

   ```bash
   python -m guardedgateway.cli create-key <tenant> --cap 100.0
   ```

3. **Point your client's base URL at GuardedGateway** instead of LiteLLM's
   proxy, and prefix every `model` value with its provider:
   `gpt-4o` becomes `openai/gpt-4o`, a local Ollama model becomes
   `ollama/qwen2.5:7b`.

4. **BYOK.** GuardedGateway needs your own provider API keys — cloud calls are
   default-deny (`GG_ALLOW_CLOUD=0` by default); set `GG_ALLOW_CLOUD=1` and
   store the provider key for your tenant before any `openai/`, `anthropic/`,
   or `gemini/` call will succeed.

5. **If you handle PHI**, flag those requests with the `X-PHI: true` header
   (or create the key with `--phi`) and mark the providers you've signed a
   BAA with as `baa: true` in `providers.yaml`. A PHI-flagged request that
   can't reach a BAA-covered provider is refused (`422 no_baa_provider`)
   instead of silently going to whatever's configured — verify this before
   you rely on it:

   ```bash
   curl -s http://localhost:8000/v1/chat/completions \
     -H "Authorization: Bearer <phi-key>" \
     -H "X-PHI: true" \
     -H "Content-Type: application/json" \
     -d '{"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "test"}]}'
   # -> 422 {"error": "no_baa_provider"} unless openai is marked baa:true
   ```

   (This exact call shape is exercised by `tests/test_app_phi_routing.py`.)

## What you lose moving off LiteLLM

LiteLLM's broader provider/SDK catalog, its more mature router feature set,
and its larger community/ecosystem. If those matter more to you than pre-call
cost refusal and BAA-only routing, don't migrate — see the comparison doc.
