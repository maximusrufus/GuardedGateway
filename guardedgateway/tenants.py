"""Tenant + API key store: SQLite-backed, alongside the ledger DB.

Not copied from another repo. Each API key belongs to a tenant, carries an
optional monthly cap (overriding the global GG_MONTHLY_CAP_USD default), a
`phi` flag (all traffic on this key is treated as PHI-flagged even without
the X-PHI header), and an optional encrypted BYOK provider key.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from guardedgateway import crypto, ledger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    name TEXT PRIMARY KEY,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    api_key TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    cap_usd REAL,
    phi INTEGER NOT NULL DEFAULT 0,
    inject_guard INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_keys (
    tenant TEXT NOT NULL,
    provider TEXT NOT NULL,
    encrypted_key TEXT NOT NULL,
    PRIMARY KEY (tenant, provider)
);
"""


@dataclass
class ApiKeyRecord:
    api_key: str
    tenant: str
    cap_usd: float | None
    phi: bool
    inject_guard: bool


class TenantStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else ledger.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def create_tenant(self, name: str) -> None:
        import time

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tenants (name, created_ts) VALUES (?, ?)",
                (name, time.time()),
            )
            self._conn.commit()

    def create_key(
        self,
        tenant: str,
        *,
        cap_usd: float | None = None,
        phi: bool = False,
        inject_guard: bool = False,
        api_key: str | None = None,
    ) -> str:
        import time

        self.create_tenant(tenant)
        api_key = api_key or f"gg-{secrets.token_urlsafe(24)}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO api_keys (api_key, tenant, cap_usd, phi, inject_guard, created_ts)
                VALUES (?,?,?,?,?,?)""",
                (api_key, tenant, cap_usd, int(phi), int(inject_guard), time.time()),
            )
            self._conn.commit()
        return api_key

    def get_key(self, api_key: str) -> ApiKeyRecord | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT api_key, tenant, cap_usd, phi, inject_guard FROM api_keys WHERE api_key = ?",
                (api_key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return ApiKeyRecord(
            api_key=row[0],
            tenant=row[1],
            cap_usd=row[2],
            phi=bool(row[3]),
            inject_guard=bool(row[4]),
        )

    def set_cap(self, api_key: str, cap_usd: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET cap_usd = ? WHERE api_key = ?", (cap_usd, api_key)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_provider_key(self, tenant: str, provider: str, plaintext_api_key: str) -> None:
        encrypted = crypto.encrypt(plaintext_api_key)
        with self._lock:
            self._conn.execute(
                """INSERT INTO provider_keys (tenant, provider, encrypted_key)
                VALUES (?,?,?)
                ON CONFLICT(tenant, provider) DO UPDATE SET encrypted_key=excluded.encrypted_key""",
                (tenant, provider, encrypted),
            )
            self._conn.commit()

    def get_provider_key(self, tenant: str, provider: str) -> str | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT encrypted_key FROM provider_keys WHERE tenant = ? AND provider = ?",
                (tenant, provider),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return crypto.decrypt(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_store_singleton: TenantStore | None = None
_singleton_lock = threading.Lock()


def get_tenant_store() -> TenantStore:
    global _store_singleton
    with _singleton_lock:
        if _store_singleton is None:
            _store_singleton = TenantStore()
        return _store_singleton


def reset_tenant_store_for_tests(path: str | Path) -> TenantStore:
    global _store_singleton
    with _singleton_lock:
        if _store_singleton is not None:
            _store_singleton.close()
        _store_singleton = TenantStore(path)
        return _store_singleton
