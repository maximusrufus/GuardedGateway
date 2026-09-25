"""Durable, append-only, month-to-date spend ledger backed by SQLite.

Ported from the *concept* of ReviewHouse's `reviewhouse/spend_ledger.py`
(cross-process durable total so N processes cannot each get their own $5),
but re-implemented against SQLite instead of a JSON file because GuardedGateway
is a long-running multi-worker service, not a batch script — SQLite gives us
transactional writes + indexed month-to-date queries for free.

Every row is one completed (or refused) call. Refused calls are recorded too
(cost=0, refused=1) so the audit trail shows what was blocked, not just what
was billed.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB_PATH = "data/guardedgateway.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    period TEXT NOT NULL,
    tenant TEXT NOT NULL,
    api_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    phi_flagged INTEGER NOT NULL DEFAULT 0,
    redaction_count INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    refused INTEGER NOT NULL DEFAULT 0,
    refusal_reason TEXT,
    request_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_period ON ledger(period);
CREATE INDEX IF NOT EXISTS idx_ledger_key ON ledger(api_key, period);
CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON ledger(tenant, period);
"""


def current_period(now: float | None = None) -> str:
    """'YYYY-MM' — the billing period a call belongs to."""
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m")


def db_path() -> Path:
    override = os.environ.get("GG_DB_PATH")
    return Path(override) if override else Path(DEFAULT_DB_PATH)


@dataclass
class LedgerEntry:
    tenant: str
    api_key: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    phi_flagged: bool = False
    redaction_count: int = 0
    cache_hit: bool = False
    refused: bool = False
    refusal_reason: str | None = None
    request_hash: str | None = None


class Ledger:
    """Thread-safe wrapper around one SQLite connection. One instance is
    shared per process (see `get_ledger()`); SQLite itself serializes writes
    via its own file lock, and `_lock` prevents interleaved python-level
    read-modify-write races between threads in this process.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def record(self, entry: LedgerEntry, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        period = current_period(ts)
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO ledger
                (ts, period, tenant, api_key, provider, model, prompt_tokens,
                 completion_tokens, cost_usd, phi_flagged, redaction_count,
                 cache_hit, refused, refusal_reason, request_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts,
                    period,
                    entry.tenant,
                    entry.api_key,
                    entry.provider,
                    entry.model,
                    entry.prompt_tokens,
                    entry.completion_tokens,
                    entry.cost_usd,
                    int(entry.phi_flagged),
                    entry.redaction_count,
                    int(entry.cache_hit),
                    int(entry.refused),
                    entry.refusal_reason,
                    entry.request_hash,
                ),
            )
            return cur.lastrowid

    def spent_usd(
        self,
        *,
        api_key: str | None = None,
        tenant: str | None = None,
        model: str | None = None,
        period: str | None = None,
        now: float | None = None,
    ) -> float:
        """Month-to-date spend, optionally scoped to a key/tenant/model.
        Only billed (non-refused) rows count — a refused call cost $0."""
        period = period or current_period(now)
        clauses = ["period = ?", "refused = 0"]
        params: list = [period]
        if api_key is not None:
            clauses.append("api_key = ?")
            params.append(api_key)
        if tenant is not None:
            clauses.append("tenant = ?")
            params.append(tenant)
        if model is not None:
            clauses.append("model = ?")
            params.append(model)
        sql = f"SELECT COALESCE(SUM(cost_usd), 0.0) FROM ledger WHERE {' AND '.join(clauses)}"
        with self._cursor() as cur:
            cur.execute(sql, params)
            (total,) = cur.fetchone()
        return float(total)

    def audit_rows(self, ts_from: float, ts_to: float) -> list[dict]:
        """Rows for the audit endpoint — never includes prompt/response text
        because that text is never written to this table in the first place."""
        with self._cursor() as cur:
            cur.execute(
                """SELECT ts, period, tenant, api_key, provider, model,
                          prompt_tokens, completion_tokens, cost_usd,
                          phi_flagged, redaction_count, cache_hit, refused,
                          refusal_reason, request_hash
                   FROM ledger WHERE ts >= ? AND ts <= ? ORDER BY ts ASC""",
                (ts_from, ts_to),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_ledger_singleton: Ledger | None = None
_singleton_lock = threading.Lock()


def get_ledger() -> Ledger:
    global _ledger_singleton
    with _singleton_lock:
        if _ledger_singleton is None:
            _ledger_singleton = Ledger()
        return _ledger_singleton


def reset_ledger_for_tests(path: str | Path) -> Ledger:
    """Test helper: point the process-wide singleton at an isolated tmp_path
    DB so tests never write to data/guardedgateway.db."""
    global _ledger_singleton
    with _singleton_lock:
        if _ledger_singleton is not None:
            _ledger_singleton.close()
        _ledger_singleton = Ledger(path)
        return _ledger_singleton
