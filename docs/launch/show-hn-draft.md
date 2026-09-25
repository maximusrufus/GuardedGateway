<!--
DRAFT ONLY. Not posted anywhere. Per task instructions this stays local.
-->

## Title (< 80 chars)

Show HN: GuardedGateway – LLM gateway that refuses before it overspends or leaks PHI

(76 chars)

## First comment (150–250 words, plain HN register)

I built GuardedGateway because I wanted an LLM gateway that refuses a call
*before* it reaches the provider, not after. Most spend-cap tools I looked at
track usage and alert you once you're over; this one prices the request
against a config file first and returns a 402 with the upstream never
contacted if it would breach your cap. It also redacts PHI (SSNs, DOBs,
emails, phone numbers, some name patterns) out of the prompt before it's
used for routing or logging, and refuses to route PHI-flagged traffic to any
provider you haven't marked as BAA-covered.

It's OpenAI-request-shape compatible, self-hostable (SQLite, no external
dependency), Apache-2.0. There's also a hosted version if you don't want to
run it yourself.

What it does NOT do yet:
- `stream: true` is accepted and returns valid SSE, but it's not real
  token-by-token streaming from the provider — the gateway waits for the
  full completion, then emits it as one SSE chunk (`guardedgateway/app.py`'s
  `_sse()`). If you need true incremental streaming, this isn't there yet.
- The name-redaction pattern is heuristic (title + name, or "patient named
  X"), not full NER — a bare, unsignaled name in a prompt won't be caught.
- The public hosted staging instance currently runs with no persistent
  volume attached, so its ledger is ephemeral on redeploy/cold-start —
  self-hosted deployments with a mounted volume don't have this problem, but
  it's a real gap on the hosted side right now, not marketing spin.
- It doesn't verify you've actually signed a BAA with a provider — `baa:
  true` in config is a declaration you make, not something the tool checks.

I'd like feedback on: whether the PHI redaction patterns are missing obvious
cases, whether the gate order (auth → PHI/BAA → cloud-block → cap → circuit
breaker → cache) makes sense for your use case, and whether the pricing
model is fair. Code and comparison docs against LiteLLM/OpenRouter/Cloudflare
AI Gateway/Portkey are in the repo.
