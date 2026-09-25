"""Optional per-key prompt-injection guard.

Ported (trimmed) from ReviewHouse's `reviewhouse/injection_guard.py`
(InstructionPatternFilter): a small, explicit, case-insensitive regex
catalog for instruction-shaped text ("ignore previous instructions",
role-reassignment, chat-control tokens, etc). Recall-oriented: a false
positive redacts a harmless span, a false negative lets an injection attempt
through — the asymmetry the source module documents.

Not applied by default (it's aimed at RAG/tool-context use cases GuardedGateway
doesn't itself have); exposed as `scan`/`redact` for a caller/key config to
opt into (`inject_guard: true` in a key's config) before content is sent
upstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REDACTION_PLACEHOLDER = "(instruction-like text removed)"

_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "ignore_prior_instructions",
        re.compile(
            r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
            r"(?:instructions|rules|guidance)",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_instructions",
        re.compile(r"disregard\s+.{0,40}?(?:instructions|rules|methodology)", re.IGNORECASE),
    ),
    ("system_prompt_mention", re.compile(r"system\s+prompt", re.IGNORECASE)),
    ("role_reassignment", re.compile(r"you\s+are\s+(?:now\s+)?(?:a|an|the)\s+", re.IGNORECASE)),
    ("as_an_ai", re.compile(r"as\s+an\s+ai\b", re.IGNORECASE)),
    ("chat_control_token_im", re.compile(r"<\|im_start\|>", re.IGNORECASE)),
    ("chat_control_token_system", re.compile(r"<\|system\|>", re.IGNORECASE)),
    ("inst_bracket_token", re.compile(r"\[INST\]", re.IGNORECASE)),
    ("hash_instruction_header", re.compile(r"###\s*(?:instruction|system)", re.IGNORECASE)),
]


@dataclass
class InjectionFinding:
    pattern_id: str
    start: int
    end: int


def scan(text: str) -> list[InjectionFinding]:
    findings: list[InjectionFinding] = []
    for pattern_id, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            findings.append(InjectionFinding(pattern_id, m.start(), m.end()))
    findings.sort(key=lambda f: (f.start, -(f.end - f.start)))
    out: list[InjectionFinding] = []
    last_end = -1
    for f in findings:
        if f.start < last_end:
            continue
        out.append(f)
        last_end = f.end
    return out


def redact(text: str) -> tuple[str, list[InjectionFinding]]:
    findings = scan(text)
    out = text
    for f in sorted(findings, key=lambda f: f.start, reverse=True):
        out = out[: f.start] + _REDACTION_PLACEHOLDER + out[f.end :]
    return out, findings
