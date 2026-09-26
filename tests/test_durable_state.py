"""Tests for guardedgateway.durable -- snapshot-on-commit / restore-on-boot.

All GCS access is faked in-process; nothing here touches the network. The
fake blob/bucket holds raw bytes plus an integer generation and raises
google.api_core.exceptions.PreconditionFailed when if_generation_match
doesn't match the current generation, mirroring real GCS compare-and-swap.

google-cloud-storage/google-api-core are optional dependencies (only needed
to exercise the fake-GCS path here); `test_inactive_when_bucket_unset` below
runs with no such import at all, matching a minimal production install.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

pytest.importorskip("google.api_core", reason="google-cloud-storage is an optional dev dependency")

from google.api_core.exceptions import NotFound, PreconditionFailed  # noqa: E402

from guardedgateway import durable  # noqa: E402
from guardedgateway.ledger import Ledger, LedgerEntry  # noqa: E402


class FakeBlob:
    def __init__(self, store: dict):
        self._store = store
        self.generation = 0

    def reload(self) -> None:
        if "data" not in self._store:
            raise NotFound("no such object")
        self.generation = self._store["generation"]

    def download_to_filename(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(self._store["data"])

    def upload_from_string(self, data: bytes, if_generation_match=None, content_type=None) -> None:
        current = self._store.get("generation", 0)
        if if_generation_match is not None and if_generation_match != current:
            raise PreconditionFailed("generation mismatch")
        new_generation = current + 1
        self._store["data"] = data
        self._store["generation"] = new_generation
        self.generation = new_generation


@pytest.fixture()
def fake_gcs(monkeypatch):
    """Install a fake blob backed by a shared dict, and activate durability."""
    store: dict = {}
    monkeypatch.setenv("GUARDEDGATEWAY_GCS_BUCKET", "fake-bucket")
    durable._reset_client_cache()
    durable._generation = 0
    durable._restored = False

    def fake_get_blob():
        return FakeBlob(store)

    monkeypatch.setattr(durable, "_get_blob", fake_get_blob)
    yield store
    monkeypatch.delenv("GUARDEDGATEWAY_GCS_BUCKET", raising=False)
    durable._reset_client_cache()
    durable._generation = 0
    durable._restored = False


def _upload_count(monkeypatch, store: dict) -> list[int]:
    """Wrap FakeBlob.upload_from_string to count real invocations."""
    calls: list[int] = []
    original = FakeBlob.upload_from_string

    def counted(self, data, if_generation_match=None, content_type=None):
        calls.append(1)
        return original(
            self, data, if_generation_match=if_generation_match, content_type=content_type
        )

    monkeypatch.setattr(FakeBlob, "upload_from_string", counted)
    return calls


def _entry(**overrides) -> LedgerEntry:
    fields = dict(
        tenant="t1",
        api_key="k1",
        provider="openai",
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=1.0,
    )
    fields.update(overrides)
    return LedgerEntry(**fields)


def test_commit_with_changes_uploads_once(tmp_path, fake_gcs, monkeypatch):
    calls = _upload_count(monkeypatch, fake_gcs)
    ledger = Ledger(tmp_path / "a.db")
    calls.clear()  # schema-creation commit may or may not fire; isolate the write under test
    ledger.record(_entry())
    ledger.close()
    assert len(calls) == 1


def test_read_only_uploads_nothing(tmp_path, fake_gcs, monkeypatch):
    calls = _upload_count(monkeypatch, fake_gcs)
    ledger = Ledger(tmp_path / "b.db")
    calls.clear()
    ledger.spent_usd(api_key="nobody")
    ledger.audit_rows(0.0, 9999999999.0)
    ledger.close()
    assert len(calls) == 0


def test_fresh_process_restores_previous_state(tmp_path, fake_gcs, monkeypatch):
    ledger1 = Ledger(tmp_path / "e.db")
    ledger1.record(_entry(api_key="k-persisted"))
    ledger1.close()

    # Simulate a brand-new process/instance: reset restored flag, new db path.
    durable._restored = False
    ledger2 = Ledger(tmp_path / "e2.db")
    spent = ledger2.spent_usd(api_key="k-persisted")
    ledger2.close()
    assert spent == 1.0


def test_stale_generation_raises_and_resyncs(tmp_path, fake_gcs, monkeypatch):
    db_path = tmp_path / "f.db"
    ledger = Ledger(db_path)
    ledger.record(_entry(api_key="o1"))  # generation now 1 in the fake store

    # Another writer wins the race: its snapshot (only "winner") lands in the
    # bucket with a bumped generation, behind our back.
    winner_conn = sqlite3.connect(str(tmp_path / "winner.db"))
    winner_conn.execute(
        "CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "period TEXT NOT NULL, tenant TEXT NOT NULL, api_key TEXT NOT NULL, "
        "provider TEXT NOT NULL, model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, "
        "completion_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, "
        "phi_flagged INTEGER NOT NULL DEFAULT 0, redaction_count INTEGER NOT NULL DEFAULT 0, "
        "cache_hit INTEGER NOT NULL DEFAULT 0, refused INTEGER NOT NULL DEFAULT 0, "
        "refusal_reason TEXT, request_hash TEXT)"
    )
    winner_conn.execute(
        "INSERT INTO ledger (ts, period, tenant, api_key, provider, model, prompt_tokens, "
        "completion_tokens, cost_usd) VALUES (0, '2026-01', 'winner-tenant', 'winner', "
        "'openai', 'm', 1, 1, 9.0)"
    )
    winner_conn.commit()
    winner_snap = sqlite3.connect(":memory:")
    winner_conn.backup(winner_snap)
    fake_gcs["data"] = bytes(winner_snap.serialize())
    fake_gcs["generation"] = fake_gcs["generation"] + 1
    winner_snap.close()
    winner_conn.close()

    with pytest.raises(durable.StaleStateError):
        ledger.record(_entry(api_key="o2"))
    ledger.close()  # releases db_path; the deferred resync can now swap the file in

    # A fresh connection (the caller's / Stripe's retry) sees the winner's
    # state, not the locally-committed-but-discarded "o2" row.
    fresh = Ledger(db_path)
    keys = {row["api_key"] for row in fresh.audit_rows(0.0, 9999999999.0)}
    fresh.close()
    assert "winner" in keys
    assert "o2" not in keys


def test_stale_wal_files_deleted_before_restore(tmp_path, fake_gcs, monkeypatch):
    # Seed the fake bucket with a real sqlite snapshot.
    seed_path = str(tmp_path / "seed.db")
    seed_conn = sqlite3.connect(seed_path)
    seed_conn.execute("CREATE TABLE t (x INTEGER)")
    seed_conn.commit()
    snap = sqlite3.connect(":memory:")
    seed_conn.backup(snap)
    fake_gcs["data"] = bytes(snap.serialize())
    fake_gcs["generation"] = 1
    snap.close()
    seed_conn.close()

    db_path = str(tmp_path / "g.db")
    wal_path = db_path + "-wal"
    shm_path = db_path + "-shm"
    with open(wal_path, "wb") as fh:
        fh.write(b"stale-wal-from-dead-instance")
    with open(shm_path, "wb") as fh:
        fh.write(b"stale-shm-from-dead-instance")

    durable._restored = False
    durable.restore_once(db_path)

    assert not os.path.exists(wal_path)
    assert not os.path.exists(shm_path)


def test_inactive_when_bucket_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDEDGATEWAY_GCS_BUCKET", raising=False)
    ledger = Ledger(tmp_path / "h.db")
    assert ledger._durable_active is False
    ledger.record(_entry())
    ledger.close()
