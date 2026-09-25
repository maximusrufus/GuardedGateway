# GuardedGateway vs. Cloudflare AI Gateway

Cloudflare AI Gateway (https://developers.cloudflare.com/ai-gateway/) is free
and offers configurable spend limits, and it runs on Cloudflare's edge —
which is a real advantage if your traffic already lives there.

## Where they overlap

- Both let you cap spend. Cloudflare AI Gateway's limits are free, same as
  GuardedGateway's caps.
- Both proxy/gate calls to multiple LLM providers.

## Where GuardedGateway differs

- **PHI redaction + BAA-only routing** is core to GuardedGateway
  (`guardedgateway/phi.py`, the `baa: true` gate in `providers.yaml` +
  `app.py::_pick_provider_chain`) — a `422 no_baa_provider` refusal for
  PHI-flagged traffic that can't reach a BAA-covered provider. We haven't
  reviewed whether Cloudflare AI Gateway has an equivalent feature; if that
  matters to you, check Cloudflare's own docs rather than trusting this line.
- **Self-hosted, no edge dependency.** GuardedGateway runs anywhere you can
  run a Python process + SQLite (or your own Postgres) — it doesn't require
  routing through Cloudflare's network. If your stack is already on
  Cloudflare, that's a point in AI Gateway's favor, not GuardedGateway's.
- **Ledger you own.** GuardedGateway's spend ledger is a SQLite file (or
  volume) you control end to end (`guardedgateway/ledger.py`); the hosted
  hosted-tier caveat in this repo's README applies only to *our* staging
  deployment, not to a self-hosted install.

## Try it against this repo

```bash
# The month-to-date ledger is a real SQLite table you can inspect yourself:
python -c "
from guardedgateway.ledger import get_ledger
print(get_ledger().spent_usd(api_key='<your-key>'))
"
```

(Ran against this repo's own ledger module while writing this doc — returns
`0.0` for a fresh key with no calls yet, as expected.)

## When Cloudflare AI Gateway is the better choice

If you're already running workloads on Cloudflare Workers/Pages, want a
zero-ops managed gateway with no server to run yourself, and don't need
PHI/BAA-aware routing — Cloudflare AI Gateway is almost certainly less work
than standing up GuardedGateway. GuardedGateway earns its keep specifically
for regulated (PHI-adjacent) deployments or teams that want the gate logic
self-hosted and auditable.
