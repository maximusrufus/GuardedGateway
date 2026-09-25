# GuardedGateway vs. LiteLLM

Both are self-hostable, open-source gateways that sit in front of multiple LLM
providers. The honest short version: **LiteLLM is the more mature, broader
project.** GuardedGateway is narrower and only worth the switch if you
specifically need pre-call cost refusal plus PHI-aware BAA-only routing.

## Where they overlap

- Both let you set a spend budget and enforce it. LiteLLM's budgets are free
  and self-hosted, same as GuardedGateway's caps — see LiteLLM's docs for how
  its budget/rate-limit config works.
- Both are OpenAI-request-shape compatible gateways with provider fallback
  chains.

## Where GuardedGateway differs

GuardedGateway's cap check runs *before* the upstream call, not after — see
`guardedgateway/app.py::_call_with_gates`, which prices the request against
`pricing.yaml` and raises `402 spend_cap` (or, mid-fallback-chain, moves to the
next provider) without ever calling `providers.dispatch`. This is enforced by
`tests/test_app_cap_and_cloud.py`.

GuardedGateway also has a PHI redaction step (`guardedgateway/phi.py`) and a
BAA-only routing gate (`guardedgateway/providers.py` config + the
`_pick_provider_chain` function in `app.py`) that refuses PHI-flagged traffic
with `422 no_baa_provider` if no provider in the chain is marked `baa: true`.
We are not claiming LiteLLM lacks equivalent PHI/BAA features — we haven't
audited LiteLLM's source to say one way or the other, and you should check
LiteLLM's own docs if that matters to your evaluation. What we can say
concretely is what GuardedGateway does, and point you at the code and tests
that back it.

## Try it against this repo

```bash
# 1. Set a $0 cap — this is a deliberate deny-all, not a no-op
python -m guardedgateway.cli create-tenant demo
python -m guardedgateway.cli create-key demo --cap 0

# 2. Any nonzero-cost call is refused before it reaches a provider
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <key-from-step-1>" \
  -H "Content-Type: application/json" \
  -d '{"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]}'
# -> 402 {"error": "spend_cap", ...} — openai.com was never contacted
```

(Verified against this repo — `pytest -k zero_cap -v` exercises this same
deny-all-at-cap-zero path with mocked transports; 3 tests pass.)

## When LiteLLM is the better choice

If you want the broadest provider/SDK coverage, an established community, a
proxy that already does load balancing and a richer ecosystem of integrations,
and you don't specifically need PHI redaction or BAA-only routing — LiteLLM is
a better default. GuardedGateway is a smaller, more opinionated tool built
around one regulated-deployment shape.
