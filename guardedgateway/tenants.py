"""Tenant + API key store: SQLite-backed, alongside the ledger DB.

Not copied from another repo. Each API key belongs to a tenant, carries an
optional monthly cap (overriding the global GG_MONTHLY_CAP_USD default), a
`phi` flag (all traffic on this key is treated as PHI-flagged even without
the X-PHI header), and an optional encrypted BYOK provider key.

Shares one SQLite file with guardedgateway.ledger.Ledger (same path, from
ledger.db_path()), so it goes through the same GCS durability layer
(guardedgateway.durable): restore-on-boot before the first connection, and
snapshot-on-commit gated on total_changes so reads and no-op commits upload
nothing. This is the money path -- Stripe webhook fulfillment provisions API
keys through this store -- so a lost write here is a paying customer who
cannot use what they paid for, not just lost history.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from guardedgateway import crypto, durable, ledger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    name TEXT PRIMARY KEY,
    created_ts REAL NOT NULL,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    subscription_status TEXT,
    tier TEXT,
    plan_active INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_ts REAL NOT NULL
);
"""

_TENANT_MIGRATION_COLUMNS = (
    "stripe_customer_id TEXT",
    "stripe_subscription_id TEXT",
    "subscription_status TEXT",
    "tier TEXT",
    "plan_active INTEGER NOT NULL DEFAULT 0",
)


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
        # Same durability gate as Ledger, and safe to call unconditionally:
        # restore_once() is idempotent (a global _restored flag), so whichever
        # of TenantStore/Ledger is constructed first in a process is the one
        # that actually restores -- the other's call is a no-op. This makes
        # construction ORDER not matter, which is the whole point: before
        # this, a TenantStore built before any Ledger would open an empty
        # (unrestored) file.
        self._durable_active = str(self.path) != ":memory:" and durable.is_active()
        if self._durable_active:
            durable.restore_once(str(self.path))
        # `timeout` makes a writer wait for a competing write instead of
        # raising immediately, and WAL + busy_timeout let this store's writes
        # (the Stripe webhook fulfillment path) proceed alongside a concurrent
        # reader instead of raising `sqlite3.OperationalError: database is
        # locked` -> HTTP 500 -> Stripe retry. Mirrors ledger.py / IDRGateKit's
        # db.py::get_connection().
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        self._persisted_changes = 0
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 15000")
            self._conn.executescript(_SCHEMA)
            for column in _TENANT_MIGRATION_COLUMNS:
                try:
                    self._conn.execute(f"ALTER TABLE tenants ADD COLUMN {column}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            self._conn.commit()
            self._persisted_changes = self._conn.total_changes

    def _commit(self) -> None:
        """Commit the current transaction and, if it changed any rows and
        durability is active, snapshot to GCS. Every write method in this
        class MUST call this instead of `self._conn.commit()` directly --
        and MUST NOT use `with self._conn:` (sqlite3's own context manager
        commits in C without going through Python, so a snapshot gated here
        would silently never fire for that form; this file uses neither, by
        inspection, but keeping every write behind this one method is what
        keeps that true going forward).

        Caller must hold self._lock.
        """
        changed = self._conn.total_changes != self._persisted_changes
        self._conn.commit()
        if self._durable_active and changed:
            durable.persist(self._conn)
            self._persisted_changes = self._conn.total_changes

    def create_tenant(self, name: str) -> None:
        import time

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO tenants (name, created_ts) VALUES (?, ?)",
                (name, time.time()),
            )
            self._commit()

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
            self._commit()
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
            self._commit()
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
            self._commit()

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

    def set_stripe_customer(self, tenant: str, customer_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET stripe_customer_id = ? WHERE name = ?",
                (customer_id, tenant),
            )
            self._commit()

    def set_subscription_state(
        self, tenant: str, subscription_id: str | None, status: str | None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET stripe_subscription_id = ?, subscription_status = ? "
                "WHERE name = ?",
                (subscription_id, status, tenant),
            )
            self._commit()

    def get_tenant(self, name: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT name, stripe_customer_id, stripe_subscription_id, "
                "subscription_status, tier, plan_active FROM tenants WHERE name = ?",
                (name,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "name": row[0],
            "stripe_customer_id": row[1],
            "stripe_subscription_id": row[2],
            "subscription_status": row[3],
            "tier": row[4],
            "plan_active": bool(row[5]),
        }

    def resolve_tenant_by_customer_id(self, customer_id: str) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT name, stripe_customer_id, stripe_subscription_id, "
                "subscription_status, tier, plan_active FROM tenants WHERE stripe_customer_id = ?",
                (customer_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "name": row[0],
            "stripe_customer_id": row[1],
            "stripe_subscription_id": row[2],
            "subscription_status": row[3],
            "tier": row[4],
            "plan_active": bool(row[5]),
        }

    def set_tier(self, tenant: str, tier: str) -> None:
        """Record a validated tier for a tenant and mark the plan active.
        Callers (webhook fulfillment) must validate `tier` against
        `billing.TIER_PRICES_USD` BEFORE calling this -- this method does not
        default or guess a tier."""
        with self._lock:
            self._conn.execute(
                "UPDATE tenants SET tier = ?, plan_active = 1 WHERE name = ?",
                (tier, tenant),
            )
            self._commit()

    def get_tenant_cap(self, tenant: str) -> float | None:
        """Tier-derived monthly spend cap default for a tenant with no
        explicit per-key cap_usd override. None if the tenant has no
        recorded/active tier or the tier isn't in the price map."""
        from guardedgateway import billing  # noqa: PLC0415 (avoid import cycle)

        record = self.get_tenant(tenant)
        if not record or not record["plan_active"] or not record["tier"]:
            return None
        return billing.TIER_CAPS_USD.get(record["tier"])

    def revoke_tenant_keys(self, tenant: str) -> None:
        """Downgrade/revoke: zero every key's cap for this tenant so further
        spend is blocked (used on subscription cancellation or payment
        failure). Keys are not deleted -- reinstating simply raises the cap
        again once payment resumes."""
        with self._lock:
            self._conn.execute("UPDATE api_keys SET cap_usd = 0 WHERE tenant = ?", (tenant,))
            self._conn.execute("UPDATE tenants SET plan_active = 0 WHERE name = ?", (tenant,))
            self._commit()

    def mark_event_processed(self, event_id: str, event_type: str) -> bool:
        """Idempotency guard for Stripe webhook events. Returns True the
        first time an event id is seen, False on any repeat delivery."""
        import time

        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO stripe_events (event_id, event_type, processed_ts) "
                    "VALUES (?, ?, ?)",
                    (event_id, event_type, time.time()),
                )
                self._commit()
                return True
            except sqlite3.IntegrityError:
                return False

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
