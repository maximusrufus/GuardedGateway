# GuardedGateway vs. OpenRouter

These solve different problems. OpenRouter is a **hosted** multi-model router
you send API calls to over the network; GuardedGateway is something **you
run** (self-hosted or on our hosted tiers) in front of your own provider
credentials.

## What OpenRouter is

A hosted API that routes to many providers/models behind one endpoint, billed
through OpenRouter's own credit system. Per OpenRouter's own pricing page
(https://openrouter.ai/docs/api-reference/limits), using their credits carries
roughly a 5.5% fee on top of the underlying model price. You don't operate any
infrastructure — you just call their API.

## What GuardedGateway is

Something you deploy (self-host for free, or use our hosted tiers) that sits
in front of *your own* provider API keys. It never takes a cut of model
pricing — it isn't a billing intermediary for model usage the way OpenRouter
is. Its job is refusing a call before it happens if it would breach your cap,
and routing PHI-flagged traffic only to BAA-covered providers
(`guardedgateway/app.py`, `providers.yaml`).

## Practical difference

- **You don't need your own API keys with OpenRouter** for many models
  (it holds relationships with providers on your behalf, for a fee).
  GuardedGateway is BYOK — see `.env.example` and
  `guardedgateway/tenants.py`'s per-tenant provider key storage — you bring
  your own OpenAI/Anthropic/Gemini keys, GuardedGateway just gates the calls.
- **PHI/BAA**: GuardedGateway will refuse to route a PHI-flagged request to a
  provider you haven't marked `baa: true` (`422 no_baa_provider`). We have not
  reviewed OpenRouter's terms for BAA coverage — check with OpenRouter
  directly if that's a requirement for you, don't take our word for it either
  way.
- **Cost model**: OpenRouter's ~5.5% credit fee is on top of every call.
  GuardedGateway (self-hosted) adds no per-call fee; the hosted tiers are flat
  monthly ($199/$499/$999).

## Try it against this repo

```bash
# GuardedGateway just gates a call to whatever provider you configure —
# here, the free "fake" provider so no real key is needed to see the shape:
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "fake/echo", "messages": [{"role": "user", "content": "hi"}]}'
```

(Verified against this repo's running dev server as part of writing this
doc — see the commit for details.)

## When OpenRouter is the better choice

If you want the widest model catalog with zero infrastructure to run, don't
want to hold provider API keys yourself, and don't need PHI/BAA-aware
routing or a local, durable spend ledger you control — OpenRouter's hosted
model is simpler. GuardedGateway is for when you specifically want the gate
logic (cap refusal, PHI redaction, BAA routing) running as code you can read
and audit in front of your own keys.
