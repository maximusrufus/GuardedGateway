# GuardedGateway vs. Portkey

Portkey (https://portkey.ai/pricing) is a hosted LLM gateway/observability
product with a Developer free tier and a Production tier at $49/mo, plus a
broader guardrails/observability feature set than GuardedGateway ships today.

## Where they overlap

- Both gate LLM calls with policy before they reach a provider.
- Both support multi-provider routing with fallbacks.

## Where GuardedGateway differs

- **Self-hostable at $0**, Apache-2.0, no vendor lock-in to a hosted control
  plane — you can read every line of the gate logic in
  `guardedgateway/app.py`, `spend_guard.py`, `phi.py`. Portkey's paid tier is
  hosted; we haven't audited whether or how much of Portkey is separately
  self-hostable, so check their docs directly rather than trusting an
  assumption here.
- **PHI redaction + BAA-only routing is a first-class, tested gate**
  (`tests/test_app_phi_routing.py`, `tests/test_phi.py`), not a general
  guardrails/observability add-on. We're not claiming Portkey lacks PHI
  features — we haven't reviewed Portkey's guardrails catalog closely enough
  to say either way.
- **Narrower scope, on purpose.** GuardedGateway doesn't attempt Portkey's
  broader observability/analytics surface; it does cap enforcement + PHI/BAA
  routing + a durable ledger and stops there.

## Try it against this repo

```bash
# See the exact PHI redaction GuardedGateway applies before a prompt is
# used for routing, logging, or the upstream call:
python -c "
from guardedgateway.phi import redact_phi
text, count, by_label = redact_phi('Patient John Smith, SSN 123-45-6789, call 555-123-4567')
print(text)
print(count, by_label)
"
```

Ran against this repo's own `phi.py` while writing this doc; actual output:

```
Patient John Smith, [REDACTED-PHI], call [REDACTED-PHI]
2 {'phone': 1, 'ssn': 1}
```

Note the plain name "John Smith" wasn't redacted here — `phi.py`'s name
pattern matches titled names (`Mr./Dr. John Smith`) or explicit
`patient ... named John Smith` phrasing, not a bare name with no signal
around it. Worth knowing if you're relying on name redaction specifically;
see `guardedgateway/phi.py`'s `_PHI_PATTERNS` for the exact patterns.

## When Portkey is the better choice

If you want a hosted product with a polished UI, broad observability across
many teams, guardrails beyond PHI (PII types, toxicity, etc.), and you're
fine on their free tier or paying $49/mo for Production — Portkey is likely a
faster path than self-hosting and maintaining GuardedGateway yourself.
GuardedGateway is for teams that specifically want the cap-refusal and
BAA-routing logic self-hosted, small, and auditable.
