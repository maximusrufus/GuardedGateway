"""Durable state for the SQLite ledger via GCS snapshot-on-commit / restore-on-boot.

Cloud Run's filesystem is in-memory and ephemeral: every revision deploy,
scale-to-zero, or crash destroys `guardedgateway.db` -- along with the spend
ledger, tenant/entitlement rows, and Stripe webhook idempotency rows it holds.
This module snapshots the whole database into Google Cloud Storage on every
commit that changed rows, and restores it before the first connection on
boot -- using the object `generation` as a compare-and-swap guard so an
overlapping old/new revision during a rollout can never silently clobber the
other's writes.

Ported from idrgatekit/durable.py (working, tested, deployed: an API key
created before a revision deploy still authenticated after it). Same
properties, renamed env vars and defaults for this repo.

Inert unless GUARDEDGATEWAY_GCS_BUCKET is set: no import-time GCS client
construction, no behavior change for local dev or the existing test suite,
and no `google.cloud.storage` import is even attempted -- this is an
open-source distribution and must install and run with no GCS libraries
present.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger("guardedgateway.durable")

_MAX_QUIET_BYTES = 20 * 1024 * 1024  # 20 MB -- log a warning above this, not a failure.

_lock = threading.RLock()  # RLock: persist() re-enters restore_once() on a stale write
_client = None  # google.cloud.storage.Client, cached
_blob = None  # google.cloud.storage.Blob, cached
_generation = 0  # last known object generation; 0 == "object does not exist yet"
_restored = False


class StaleStateError(Exception):
    """Raised when a commit's GCS upload lost a compare-and-swap race.

    This can only happen during the seconds-long overlap of a Cloud Run
    rollout where an old and a new revision are both briefly live. The local
    write that lost is intentionally discarded here; the caller (or Stripe's
    own webhook retry) replays the request against the winner's state.
    """


def gcs_bucket_name() -> str | None:
    return os.environ.get("GUARDEDGATEWAY_GCS_BUCKET") or None


def gcs_object_name() -> str:
    return os.environ.get("GUARDEDGATEWAY_GCS_OBJECT", "guardedgateway.db")


def is_active() -> bool:
    return bool(gcs_bucket_name())


def _get_blob():
    """Return the cached (client, blob) pair, constructing it on first use."""
    global _client, _blob
    if _blob is None:
        from google.cloud import storage  # imported lazily so it's optional locally

        _client = storage.Client()
        bucket = _client.bucket(gcs_bucket_name())
        _blob = bucket.blob(gcs_object_name())
    return _blob


def _reset_client_cache() -> None:
    """Test hook: drop the cached client/blob so a fake can be installed."""
    global _client, _blob
    _client = None
    _blob = None


def restore_once(db_path: str) -> None:
    """Restore db_path from GCS if this process hasn't already done so.

    Must be called before the first sqlite3.connect() for db_path. Deletes
    stale -wal/-shm siblings first -- they belong to whatever instance died
    (or scaled to zero) before this one started, and replaying them against a
    freshly-restored file would corrupt it.
    """
    global _generation, _restored
    with _lock:
        if _restored:
            return
        for suffix in ("-wal", "-shm"):
            stale = Path(str(db_path) + suffix)
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Still held open -- e.g. the same process re-restoring right
                # after a StaleStateError, with its own connection's -wal
                # still live (observed on Windows, which locks open files).
                # A genuinely dead instance's handle is gone and this branch
                # is never hit for it; here it's just not ours to delete.
                logger.warning("durable state: could not remove %s: %s", stale, exc)

        blob = _get_blob()
        try:
            blob.reload()
        except Exception as exc:
            from google.api_core.exceptions import NotFound

            if isinstance(exc, NotFound):
                _generation = 0
                _restored = True
                logger.info("durable state: no existing object, starting fresh")
                return
            logger.error("durable state: restore failed: %s", exc)
            raise

        # Download to a sibling temp file, then atomically replace db_path.
        # google-cloud-storage's download_to_filename does a plain
        # open(filename, "wb") in place -- fine on a cold boot where nothing
        # has db_path open yet, but restore_once is also called mid-process
        # to resync after a StaleStateError, while THIS process's own
        # sqlite3 connection still has db_path open with a live WAL. An
        # in-place overwrite would share that connection's inode, so its
        # eventual checkpoint-on-close would write stale WAL frames back
        # over the just-downloaded winner. os.replace() swaps the directory
        # entry to a new inode instead: new opens see the fresh download,
        # the old connection's fd stays pointed at the old (now-unlinked,
        # harmless) inode.
        tmp_path = f"{db_path}.download.tmp"
        try:
            blob.download_to_filename(tmp_path)
        except Exception as exc:
            logger.error("durable state: download failed: %s", exc)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

        try:
            os.replace(tmp_path, db_path)
        except OSError as exc:
            # On Windows, replacing a file that another handle in this same
            # process still has open (the live connection that just lost the
            # compare-and-swap race in persist()) can raise a sharing
            # violation -- there is no live connection with db_path open on a
            # cold boot, so this only happens on the mid-process resync path.
            # Leave `_restored` False so the NEXT restore_once call (for the
            # next connection opened against db_path, once this one closes)
            # retries the swap; nothing is silently skipped, it's deferred.
            logger.warning(
                "durable state: could not swap in downloaded file (will retry on next connection): %s",
                exc,
            )
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return

        _generation = blob.generation
        _restored = True
        logger.info("durable state: restored %s (generation=%s)", db_path, _generation)


def persist(conn: sqlite3.Connection) -> None:
    """Upload a consistent snapshot of conn's database to GCS.

    Uses the SQLite backup API into an in-memory database and serializes
    that, rather than reading the on-disk file directly -- in WAL mode the
    file alone is not a consistent point-in-time image.
    """
    global _generation

    snap = sqlite3.connect(":memory:")
    try:
        conn.backup(snap)
        data = snap.serialize()
    finally:
        snap.close()

    size = len(data)
    if size > _MAX_QUIET_BYTES:
        logger.warning("durable state: snapshot is %d bytes (>20MB) -- revisit the design", size)

    from google.api_core.exceptions import PreconditionFailed

    with _lock:
        blob = _get_blob()
        try:
            blob.upload_from_string(
                bytes(data),
                if_generation_match=_generation,
                content_type="application/x-sqlite3",
            )
        except PreconditionFailed as exc:
            logger.error(
                "durable state: stale generation on upload (expected %s): %s",
                _generation,
                exc,
            )
            # Another writer won the race. Discard this write, force a fresh
            # restore so the local file matches the winner, and let the
            # caller's retry (Stripe's webhook retry, or the HTTP client)
            # replay against that winner's state.
            global _restored
            _restored = False
            # Row is a plain tuple here (callers may not set row_factory =
            # sqlite3.Row), so index positionally: PRAGMA database_list
            # yields (seq, name, file).
            db_path = conn.execute("PRAGMA database_list").fetchone()[2]
            restore_once(db_path)
            raise StaleStateError(str(exc)) from exc

        _generation = blob.generation
        logger.info(
            "durable state: uploaded snapshot (%d bytes, generation=%s)",
            size,
            _generation,
        )
