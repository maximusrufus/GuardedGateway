"""PHI detection and redaction.

Merged/ported from two sources (see PROVENANCE.md for exact provenance):
- ReviewHouse's `reviewhouse/determination.py` (lines ~505-660): the
  `_PHI_PATTERNS` catalog, `scan_phi`/`redact_phi` span-scan-and-splice
  approach (longest-match-wins overlap removal, splice from the end so
  earlier offsets stay valid).
- HealthShield's `Backend/core/security_hardening.py`: `scrub_phi` /
  `scrub_dict` — the recursive dict-scrubbing helper for structured logs.

Recall-oriented by design (same rationale as both sources): a false positive
costs a little redacted context; a false negative is PHI reaching a
third-party model or a log file. GuardedGateway never reasons about whether
a specific match is "really" PHI — every match becomes a placeholder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PHI_PLACEHOLDER = "[REDACTED-PHI]"

_PHI_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ssn", re.compile(r"\b\d{3}[\s.\-/]\d{2}[\s.\-/]\d{4}\b")),
    ("ssn", re.compile(r"\bssn\b\W{0,10}\d{3}\D{0,3}\d{2}\D{0,3}\d{4}\b", re.IGNORECASE)),
    ("ssn", re.compile(r"(?<![\w-])\d{9}(?![\d-])")),
    (
        "insurance_id",
        re.compile(
            r"\b(?:member|subscriber|policy|group|insured|enrollee|beneficiary|health\s+plan)"
            r"(?:[\s_-]*(?:id|ids|no|nos|num|number|#)\s*[:#-]?\s*[A-Za-z0-9][A-Za-z0-9-]{2,}"
            r"|\s*[:#-]?\s*(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{4,})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "mrn",
        re.compile(
            r"\b(?:mrn|medical\s+record(?:\s+number|\s+no\.?|\s+#)?|med\s+rec(?:\s+no\.?)?)"
            r"\s*[:#-]?\s*[A-Za-z0-9-]{3,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dob",
        re.compile(
            r"\b(?:dob|date\s+of\s+birth|birth\s*date|born(?:\s+on)?)\b\W{0,12}"
            r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{2,4})",
            re.IGNORECASE,
        ),
    ),
    ("dob", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("phone", re.compile(r"(?:\+?1[\s.-])?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b")),
    (
        "address",
        re.compile(
            r"\b\d{1,6}\s+[A-Z][a-zA-Z']*(?:\s+[A-Z][a-zA-Z']*){0,2}\s+"
            r"(?:St|Street|Ave|Avenue|Rd|Road|Ct|Court|Dr|Drive|Ln|Lane|Blvd|Way|Pl|Place|"
            r"Ter|Terrace|Cir|Circle|Hwy|Highway)\b(?:,?\s+[A-Z][a-zA-Z]*(?:,?\s+[A-Z]{2})?)?"
        ),
    ),
    (
        "address",
        re.compile(
            r"\b(?:lives?|resid(?:es|ing)|located|domicil\w*)\s+at\s+\d[^.\n]{0,80}"
            r"|\b(?:home|mailing|street|residential)\s+address\b\W{0,4}[^.\n]{0,80}",
            re.IGNORECASE,
        ),
    ),
    ("name", re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Miss)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")),
    (
        "name",
        re.compile(
            r"\b(?:patient|member|enrollee|claimant|beneficiary|insured|subscriber)"
            r"(?:'s)?(?:\s+name)?(?:\s+(?:is|was|named))?[\s,:]+"
            r"[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+\b"
        ),
    ),
]


@dataclass
class PhiFinding:
    label: str
    start: int
    end: int


def scan_phi(text: str) -> list[PhiFinding]:
    """Return non-overlapping PHI-shaped spans (longest match wins on overlap)."""
    hits: list[PhiFinding] = []
    for label, pattern in _PHI_PATTERNS:
        for m in pattern.finditer(text):
            if m.start() != m.end():
                hits.append(PhiFinding(label, m.start(), m.end()))
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    out: list[PhiFinding] = []
    last_end = -1
    for h in hits:
        if h.start < last_end:
            continue
        out.append(h)
        last_end = h.end
    return out


def redact_phi(text: str) -> tuple[str, int, dict[str, int]]:
    """Returns (redacted_text, total_redaction_count, counts_by_label).
    Never returns the original spans — only counts, so callers can log
    'redacted 2 ssn, 1 dob' without ever writing raw PHI to a log."""
    findings = scan_phi(text)
    out = text
    counts: dict[str, int] = {}
    for f in sorted(findings, key=lambda h: h.start, reverse=True):
        out = out[: f.start] + PHI_PLACEHOLDER + out[f.end :]
        counts[f.label] = counts.get(f.label, 0) + 1
    return out, len(findings), counts


def scrub_dict(d: dict, sensitive_keys: set[str] | None = None) -> dict:
    """Recursively redact string values in a dict — used before anything is
    logged. Keys named like an identifier (ssn, mrn, dob, ...) are replaced
    outright regardless of pattern match, matching HealthShield's
    `scrub_dict` convention."""
    sensitive_keys = sensitive_keys or {
        "ssn",
        "mrn",
        "dob",
        "date_of_birth",
        "member_id",
        "policy_id",
        "phone",
        "email",
        "address",
        "name",
        "patient_name",
    }
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, str):
            if k.lower() in sensitive_keys:
                out[k] = PHI_PLACEHOLDER
            else:
                redacted, _count, _counts = redact_phi(v)
                out[k] = redacted
        elif isinstance(v, dict):
            out[k] = scrub_dict(v, sensitive_keys)
        elif isinstance(v, list):
            out[k] = [
                scrub_dict(item, sensitive_keys)
                if isinstance(item, dict)
                else (redact_phi(item)[0] if isinstance(item, str) else item)
                for item in v
            ]
        else:
            out[k] = v
    return out
