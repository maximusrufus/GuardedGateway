# Provenance

GuardedGateway is a new, standalone repo. Nothing here `import`s across repos —
every file below was **read in full**, then re-implemented/ported by hand into this
package, adapted for a live request-serving gateway (an internal module's sources are
batch eval scripts; an internal module's are a full patient-billing app). No verbatim
copy-paste of a whole file; logic and structure were ported, module names and
framing rewritten for this product.

| GuardedGateway file | Ported from | What changed |
|---|---|---|
| `guardedgateway/spend_guard.py` | an internal module (`SpendCapExceeded`, `CallCapExceeded`, `refuse_if_over_cap`, `SpendGuardLLM`) | Re-implemented as stateless functions (`estimate_cost`, `actual_cost`, `refuse_if_over_cap`) driven by `pricing.yaml` instead of a `price_fn` callable + a stateful wrapper class, because the gateway needs cost estimation before it knows which class/wrapper chain applies. **Deliberate behavior change**: the source treats `cap_usd <= 0` as "skip checks" (its convention for "local backend, nothing to guard"). GuardedGateway's `cap == 0` means the opposite — explicit denial of every nonzero-cost call — because "cannot overspend" is the product's headline claim, not an internal batch-script convenience. Pinned by `tests/test_spend_guard.py::test_zero_cap_denies_all_cloud_calls`. |
| `guardedgateway/cloud_gate.py` | an internal module (`LocalFirstRequired`, `require_local_first`) + an internal module (`CloudSpendBlocked`, `cloud_llm_allowed`) | The source's local-first gate keys off "has this SCRIPT (`sys.argv[0]`) recorded a recent local run" via a receipt file — a batch-eval-specific concept with no gateway analog (a gateway serves many concurrent callers, not one script running one task). Re-implemented as the coarser but unambiguous gateway version: a cloud-prefixed model requires **both** `GG_ALLOW_CLOUD=1` **and** a configured spend cap, checked together so cloud traffic can never be unblocked without also being capped. `CloudSpendBlocked` and the default-deny posture are otherwise the same idea as the source. |
| `guardedgateway/phi.py` | an internal module lines ~505-660 (`_PHI_PATTERNS`, `scan_phi`, `redact_phi`) + an internal module (`scrub_phi`, `scrub_dict`) | Merged both pattern catalogs (an internal module's is broader: MRN/insurance-id/address/name in addition to SSN/DOB that an internal module's covers) into one `_PHI_PATTERNS` list. `redact_phi` now returns `(text, count, counts_by_label)` instead of just text, so callers can log "redacted 2 ssn" without ever touching raw content. `scrub_dict` ported near-verbatim from an internal module's dict-recursion approach, adapted to call the merged `redact_phi`. |
| `guardedgateway/injection_guard.py` | an internal module (`InstructionPatternFilter`) | Trimmed to the core regex catalog (`ignore previous instructions`, role-reassignment, chat-control tokens). The source's Unicode-homoglyph-folding and base64-decode-and-rescan layers were **not** ported (out of scope for a first release — GuardedGateway's injection guard is opt-in per-key, not a security-critical last line of defense the way the source's is for arbitration prompts); noted here as a deliberately skipped hardening layer, not a silent omission. |
| `guardedgateway/cache.py` (`CircuitBreaker`) | an internal module (Redis circuit-breaker: failure counter, exponential backoff, half-open re-probe) | Re-implemented generically per-provider-name instead of per-Redis-connection, with an injectable clock (`now_fn`) so tests never sleep for a real backoff. Preserves the source's core invariant: **never latches permanently closed-to-fallback** — a success always resets to closed, a failed half-open probe reopens (with the same fixed backoff here, vs the source's doubling backoff — simplified since a provider outage duration doesn't scale the way a Redis TCP failure's retry cost does). |
| `guardedgateway/cache.py` (`cache_key`, `ResponseCache`) | an internal module (`cache_key`, `CachedLLM`) | Same sha256-over-NUL-joined-fields key derivation. Store is an in-memory dict with TTL instead of the source's append-only JSONL file — per the task spec ("simple is fine — don't need Redis") and because a gateway process restarting is a normal event a batch script's multi-day cache isn't optimized for. |
| `guardedgateway/ledger.py` | Concept only from an internal module (referenced in `spend_guard.py`, not read directly as a file — inferred from its usage: a durable, cross-process, month-to-date total) | Implemented fresh against SQLite (not JSON) because GuardedGateway is a multi-worker long-running service, not a single-process batch script — SQLite gives transactional writes and indexed month-to-date queries without a hand-rolled file lock. |

## Not ported (deliberately out of scope for v0.1)

- an internal module's `GeminiVertexLLM`'s retry/backoff-on-429 logic, thinking-token
  billing correction, and gcloud-CLI-credential fallback — GuardedGateway's Gemini
  client (`guardedgateway/providers.py::call_gemini`) is a minimal REST call; an
  operator running heavy Gemini traffic through this gateway should expect to
  extend it.
- The injection guard's homoglyph/zero-width/base64 hardening (see table above).

## Everything else

`guardedgateway/app.py`, `providers.py`, `tenants.py`, `crypto.py`, `billing.py`,
`config.py`, `cli.py`, and all templates/tests are new code written for this repo,
not ported from anywhere.
