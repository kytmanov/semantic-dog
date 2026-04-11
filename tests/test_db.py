"""Stage 3 tests — SQLite state database."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from semanticdog.db import Database
from semanticdog.exceptions import DatabaseError, LockError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "state" / "state.db")


@pytest.fixture
def real_file(tmp_path):
    """A real file on disk (needed for import_json path existence check)."""
    p = tmp_path / "real.jpg"
    p.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    return str(p)


@pytest.fixture
def real_file2(tmp_path):
    p = tmp_path / "real2.cr2"
    p.write_bytes(b"\x00" * 50)
    return str(p)


# ---------------------------------------------------------------------------
# Schema / initialisation
# ---------------------------------------------------------------------------

class TestSchema:
    def test_creates_parent_dirs(self, tmp_path):
        db = Database(tmp_path / "deep" / "nested" / "state.db")
        assert (tmp_path / "deep" / "nested").is_dir()

    def test_wal_mode_set(self, db):
        conn = db._connect()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert row[0] == "wal"

    def test_schema_version_stored(self, db):
        assert db.get_meta("schema_version") == "1"

    def test_tables_exist(self, db):
        conn = db._connect()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert {"files", "scans", "scan_queue", "db_meta"} <= tables

    def test_indexes_exist(self, db):
        conn = db._connect()
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "idx_files_status" in indexes
        assert "idx_files_checked_at" in indexes

    def test_no_path_primary_key_index(self, db):
        """Ensure we don't have the redundant path index (path is already PK)."""
        conn = db._connect()
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "idx_files_path_prefix" not in indexes

    def test_reinitialise_is_idempotent(self, db):
        db._init_schema()  # second call — must not raise or lose data
        assert db.get_meta("schema_version") == "1"


# ---------------------------------------------------------------------------
# get_meta / set_meta
# ---------------------------------------------------------------------------

class TestMeta:
    def test_missing_key_returns_none(self, db):
        assert db.get_meta("nonexistent") is None

    def test_set_and_get(self, db):
        db.set_meta("foo", "bar")
        assert db.get_meta("foo") == "bar"

    def test_overwrite(self, db):
        db.set_meta("foo", "bar")
        db.set_meta("foo", "baz")
        assert db.get_meta("foo") == "baz"


# ---------------------------------------------------------------------------
# needs_check
# ---------------------------------------------------------------------------

class TestNeedsCheck:
    def test_never_seen_returns_true(self, db):
        assert db.needs_check("/photos/img.jpg", 1.0, 1024) is True

    def test_same_mtime_size_returns_false(self, db):
        db.record("/photos/img.jpg", 1.0, 1024, "ok")
        assert db.needs_check("/photos/img.jpg", 1.0, 1024) is False

    def test_mtime_changed_returns_true(self, db):
        db.record("/photos/img.jpg", 1.0, 1024, "ok")
        assert db.needs_check("/photos/img.jpg", 2.0, 1024) is True

    def test_size_changed_returns_true(self, db):
        db.record("/photos/img.jpg", 1.0, 1024, "ok")
        assert db.needs_check("/photos/img.jpg", 1.0, 2048) is True

    def test_force_recheck_not_triggered_yet(self, db):
        db.record("/photos/img.jpg", 1.0, 1024, "ok")
        assert db.needs_check("/photos/img.jpg", 1.0, 1024, force_recheck_days=90) is False

    def test_force_recheck_zero_disabled(self, db):
        # Manually insert an old checked_at
        conn = db._connect()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        conn.execute(
            "INSERT INTO files (path, mtime, size, status, checked_at) VALUES (?,?,?,?,?)",
            ("/old.jpg", 1.0, 100, "ok", old_ts),
        )
        conn.commit()
        conn.close()
        assert db.needs_check("/old.jpg", 1.0, 100, force_recheck_days=0) is False

    def test_force_recheck_triggers_after_days(self, db):
        conn = db._connect()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        conn.execute(
            "INSERT INTO files (path, mtime, size, status, checked_at) VALUES (?,?,?,?,?)",
            ("/old.jpg", 1.0, 100, "ok", old_ts),
        )
        conn.commit()
        conn.close()
        assert db.needs_check("/old.jpg", 1.0, 100, force_recheck_days=90) is True


# ---------------------------------------------------------------------------
# record + notified_at transitions
# ---------------------------------------------------------------------------

class TestRecord:
    def test_basic_insert(self, db):
        db.record("/img.jpg", 1.0, 1024, "ok")
        stats = db.get_stats()
        assert stats["total"] == 1
        assert stats["by_status"]["ok"] == 1

    def test_ok_to_corrupt_resets_notified_at(self, db):
        db.record("/img.jpg", 1.0, 1024, "ok")
        # Mark as notified
        db.mark_notified(["/img.jpg"])
        # Now record as corrupt
        db.record("/img.jpg", 1.0, 1024, "corrupt")
        new_corrupt = db.get_new_corrupt()
        paths = [r["path"] for r in new_corrupt]
        assert "/img.jpg" in paths

    def test_corrupt_to_corrupt_preserves_notified_at(self, db):
        db.record("/img.jpg", 1.0, 1024, "corrupt")
        db.mark_notified(["/img.jpg"])
        # Record corrupt again (same status)
        db.record("/img.jpg", 1.0, 1024, "corrupt")
        new_corrupt = db.get_new_corrupt()
        paths = [r["path"] for r in new_corrupt]
        assert "/img.jpg" not in paths

    def test_upsert_updates_status(self, db):
        db.record("/img.jpg", 1.0, 1024, "ok")
        db.record("/img.jpg", 1.0, 1024, "corrupt", error="truncated")
        stats = db.get_stats()
        assert stats["by_status"].get("ok", 0) == 0
        assert stats["by_status"]["corrupt"] == 1

    def test_error_and_suggested_action_stored(self, db):
        db.record(
            "/img.jpg", 1.0, 1024, "corrupt",
            error="Unexpected EOF",
            suggested_action="Re-download from source",
        )
        rows = db.get_corrupt_files()
        assert rows[0]["error"] == "Unexpected EOF"
        assert rows[0]["suggested_action"] == "Re-download from source"

    def test_scan_id_stored(self, db):
        scan_id = db.create_scan()
        db.record("/img.jpg", 1.0, 1024, "ok", scan_id=scan_id)
        conn = db._connect()
        row = conn.execute("SELECT scan_id FROM files WHERE path=?", ("/img.jpg",)).fetchone()
        conn.close()
        assert row["scan_id"] == scan_id


# ---------------------------------------------------------------------------
# mark_notified
# ---------------------------------------------------------------------------

class TestMarkNotified:
    def test_sets_notified_at(self, db):
        db.record("/img.jpg", 1.0, 1024, "corrupt")
        assert db.get_new_corrupt() != []
        db.mark_notified(["/img.jpg"])
        assert db.get_new_corrupt() == []

    def test_empty_list_no_op(self, db):
        db.mark_notified([])  # must not raise


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

class TestGetCorruptFiles:
    def _insert(self, db, path, status="corrupt", ext=None):
        db.record(path, 1.0, 1024, status)

    def test_returns_only_corrupt(self, db):
        db.record("/a.jpg", 1.0, 100, "corrupt")
        db.record("/b.jpg", 1.0, 100, "ok")
        rows = db.get_corrupt_files()
        assert len(rows) == 1
        assert rows[0]["path"] == "/a.jpg"

    def test_ext_filter(self, db):
        db.record("/a.jpg", 1.0, 100, "corrupt")
        db.record("/b.pdf", 1.0, 100, "corrupt")
        rows = db.get_corrupt_files(ext="jpg")
        assert all(r["path"].endswith(".jpg") for r in rows)
        assert len(rows) == 1

    def test_path_prefix_filter(self, db):
        db.record("/photos/img.jpg", 1.0, 100, "corrupt")
        db.record("/docs/report.pdf", 1.0, 100, "corrupt")
        rows = db.get_corrupt_files(path_prefix="/photos")
        assert len(rows) == 1
        assert rows[0]["path"] == "/photos/img.jpg"

    def test_limit(self, db):
        for i in range(10):
            db.record(f"/f{i}.jpg", float(i), 100, "corrupt")
        rows = db.get_corrupt_files(limit=3)
        assert len(rows) == 3


class TestGetStats:
    def test_empty_db(self, db):
        stats = db.get_stats()
        assert stats["total"] == 0
        assert stats["by_status"] == {}

    def test_multiple_statuses(self, db):
        db.record("/a.jpg", 1.0, 100, "ok")
        db.record("/b.jpg", 1.0, 100, "ok")
        db.record("/c.jpg", 1.0, 100, "corrupt")
        db.record("/d.jpg", 1.0, 100, "unreadable")
        stats = db.get_stats()
        assert stats["total"] == 4
        assert stats["by_status"]["ok"] == 2
        assert stats["by_status"]["corrupt"] == 1
        assert stats["by_status"]["unreadable"] == 1


class TestGetNewCorruptUnreadable:
    def test_get_new_corrupt_excludes_notified(self, db):
        db.record("/a.jpg", 1.0, 100, "corrupt")
        db.record("/b.jpg", 1.0, 100, "corrupt")
        db.mark_notified(["/a.jpg"])
        result = db.get_new_corrupt()
        assert len(result) == 1
        assert result[0]["path"] == "/b.jpg"

    def test_get_new_unreadable(self, db):
        db.record("/mnt/disk.jpg", 1.0, 100, "unreadable")
        result = db.get_new_unreadable()
        assert result[0]["path"] == "/mnt/disk.jpg"

    def test_ok_not_in_new_corrupt(self, db):
        db.record("/ok.jpg", 1.0, 100, "ok")
        assert db.get_new_corrupt() == []


# ---------------------------------------------------------------------------
# Scan lifecycle
# ---------------------------------------------------------------------------

class TestScanLifecycle:
    def test_create_scan_returns_uuid(self, db):
        sid = db.create_scan()
        assert len(sid) == 36  # UUID4 string

    def test_create_with_scope(self, db):
        sid = db.create_scan(scope="/photos")
        scans = db.list_scans()
        assert scans[0]["scope"] == "/photos"

    def test_finish_scan_stores_stats(self, db):
        sid = db.create_scan()
        db.finish_scan(sid, total=100, corrupt=3, unreadable=1, files_per_sec=12.5)
        scans = db.list_scans()
        s = scans[0]
        assert s["total"] == 100
        assert s["corrupt"] == 3
        assert s["unreadable"] == 1
        assert abs(s["files_per_sec"] - 12.5) < 0.01
        assert s["finished_at"] is not None

    def test_list_scans_order(self, db):
        sid1 = db.create_scan()
        sid2 = db.create_scan()
        scans = db.list_scans()
        # Most recent first
        assert scans[0]["id"] == sid2

    def test_list_scans_limit(self, db):
        for _ in range(25):
            db.create_scan()
        assert len(db.list_scans(limit=5)) == 5

    def test_get_last_files_per_sec_none_when_empty(self, db):
        assert db.get_last_files_per_sec() is None

    def test_get_last_files_per_sec(self, db):
        sid = db.create_scan()
        db.finish_scan(sid, 50, 0, 0, 8.0)
        assert db.get_last_files_per_sec() == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# scan_queue
# ---------------------------------------------------------------------------

class TestScanQueue:
    def test_queue_and_retrieve(self, db):
        sid = db.create_scan()
        db.queue_paths(sid, ["/a.jpg", "/b.jpg", "/c.jpg"])
        pending = db.get_pending_paths(sid)
        assert set(pending) == {"/a.jpg", "/b.jpg", "/c.jpg"}

    def test_mark_done_removes_from_pending(self, db):
        sid = db.create_scan()
        db.queue_paths(sid, ["/a.jpg", "/b.jpg"])
        db.mark_queue_done(sid, ["/a.jpg"])
        pending = db.get_pending_paths(sid)
        assert pending == ["/b.jpg"]

    def test_batch_size_respected(self, db):
        """Large batch should insert without error (chunked at 10K)."""
        sid = db.create_scan()
        paths = [f"/file{i}.jpg" for i in range(12_000)]
        db.queue_paths(sid, paths)
        conn = db._connect()
        count = conn.execute(
            "SELECT COUNT(*) FROM scan_queue WHERE scan_id=? AND done=0", (sid,)
        ).fetchone()[0]
        conn.close()
        assert count == 12_000

    def test_cleanup_scan_queue(self, db):
        sid = db.create_scan()
        db.queue_paths(sid, ["/a.jpg", "/b.jpg"])
        db.mark_queue_done(sid, ["/a.jpg", "/b.jpg"])
        db.cleanup_scan_queue(sid)
        assert db.get_pending_paths(sid) == []
        conn = db._connect()
        count = conn.execute(
            "SELECT COUNT(*) FROM scan_queue WHERE scan_id=?", (sid,)
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_ignore_duplicate_queue_insert(self, db):
        sid = db.create_scan()
        db.queue_paths(sid, ["/a.jpg"])
        db.queue_paths(sid, ["/a.jpg"])  # duplicate — INSERT OR IGNORE
        pending = db.get_pending_paths(sid)
        assert pending.count("/a.jpg") == 1

    def test_cleanup_stale_queues(self, db):
        sid = db.create_scan()
        # Manually backdate the scan's started_at
        conn = db._connect()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        conn.execute("UPDATE scans SET started_at=? WHERE id=?", (old_ts, sid))
        conn.commit()
        conn.close()
        db.queue_paths(sid, ["/stale.jpg"])
        removed = db.cleanup_stale_queues(max_age_days=7)
        assert removed >= 1


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_all(self, db):
        db.record("/a.jpg", 1.0, 100, "ok")
        db.record("/b.pdf", 1.0, 100, "corrupt")
        count = db.reset()
        assert count == 2
        assert db.get_stats()["total"] == 0

    def test_reset_path_prefix(self, db):
        db.record("/photos/a.jpg", 1.0, 100, "ok")
        db.record("/docs/b.pdf", 1.0, 100, "ok")
        count = db.reset(path_prefix="/photos")
        assert count == 1
        stats = db.get_stats()
        assert stats["total"] == 1
        assert stats["by_status"]["ok"] == 1

    def test_reset_empty_db(self, db):
        assert db.reset() == 0


# ---------------------------------------------------------------------------
# Vacuum
# ---------------------------------------------------------------------------

class TestVacuum:
    def test_incremental_vacuum_no_error(self, db):
        db.incremental_vacuum(pages=100)

    def test_vacuum_sets_meta(self, db):
        db.incremental_vacuum()
        assert db.get_meta("vacuum_last_run") is not None

    def test_should_vacuum_true_when_no_meta(self, db):
        assert db.should_vacuum() is True

    def test_should_vacuum_false_after_recent_run(self, db):
        db.incremental_vacuum()
        assert db.should_vacuum(min_days=7) is False

    def test_should_vacuum_true_after_old_run(self, db):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        db.set_meta("vacuum_last_run", old)
        assert db.should_vacuum(min_days=7) is True


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

class TestExportImport:
    def test_export_empty(self, db):
        assert db.export_json() == []

    def test_export_round_trip(self, db, real_file):
        db.record(real_file, 1.0, 100, "ok")
        exported = db.export_json()
        assert len(exported) == 1
        assert exported[0]["path"] == real_file

    def test_import_skips_missing_paths(self, db):
        records = [{"path": "/nonexistent/ghost.jpg", "mtime": 1.0, "size": 100, "status": "ok", "checked_at": "2024-01-01"}]
        inserted, skipped = db.import_json(records)
        assert inserted == 0
        assert skipped == 1

    def test_import_inserts_existing_path(self, db, real_file):
        records = [{
            "path": real_file, "mtime": 1.0, "size": 100,
            "status": "ok", "checked_at": "2024-01-01T00:00:00+00:00",
        }]
        inserted, skipped = db.import_json(records)
        assert inserted == 1
        assert skipped == 0

    def test_import_no_force_skips_existing(self, db, real_file):
        db.record(real_file, 1.0, 100, "ok")
        records = [{
            "path": real_file, "mtime": 2.0, "size": 200,
            "status": "corrupt", "checked_at": "2024-01-01T00:00:00+00:00",
        }]
        inserted, skipped = db.import_json(records, force=False)
        assert inserted == 0
        assert skipped == 1
        # Original record preserved
        assert db.get_stats()["by_status"]["ok"] == 1

    def test_import_force_overwrites(self, db, real_file):
        db.record(real_file, 1.0, 100, "ok")
        records = [{
            "path": real_file, "mtime": 2.0, "size": 200,
            "status": "corrupt", "checked_at": "2024-01-01T00:00:00+00:00",
        }]
        inserted, skipped = db.import_json(records, force=True)
        assert inserted == 1
        assert db.get_stats()["by_status"].get("ok", 0) == 0
        assert db.get_stats()["by_status"]["corrupt"] == 1

    def test_import_path_map(self, db, real_file):
        old_prefix = "/old/mount"
        new_prefix = str(Path(real_file).parent)
        old_path = old_prefix + "/" + Path(real_file).name
        records = [{
            "path": old_path, "mtime": 1.0, "size": 100,
            "status": "ok", "checked_at": "2024-01-01T00:00:00+00:00",
        }]
        inserted, skipped = db.import_json(records, path_map={old_prefix: new_prefix})
        assert inserted == 1

    def test_import_skips_empty_path(self, db):
        records = [{"path": "", "mtime": 1.0, "size": 0, "status": "ok", "checked_at": ""}]
        inserted, skipped = db.import_json(records)
        assert skipped == 1


# ---------------------------------------------------------------------------
# Instance lock
# ---------------------------------------------------------------------------

class TestInstanceLock:
    def test_acquire_release_cycle(self, db):
        uuid = "test-boot-uuid"
        db.acquire_lock(uuid)
        assert db.get_meta("lock") is not None
        db.release_lock()
        assert db.get_meta("lock") is None

    def test_stale_lock_overwritten(self, db):
        # Write a lock with a dead PID
        db.set_meta("lock", json.dumps({"pid": 999999999, "boot_uuid": "old-uuid"}))
        db.acquire_lock("new-uuid")  # must not raise
        info = json.loads(db.get_meta("lock"))
        assert info["pid"] == os.getpid()

    def test_live_same_uuid_raises(self, db):
        uuid = "same-uuid"
        db.acquire_lock(uuid)
        with pytest.raises(LockError, match="Another sdog instance"):
            db.acquire_lock(uuid)
        db.release_lock()

    def test_live_different_uuid_does_not_raise(self, db):
        """Same PID but different boot_uuid = stale from prior run, overwrite."""
        db.set_meta("lock", json.dumps({"pid": os.getpid(), "boot_uuid": "different-uuid"}))
        db.acquire_lock("new-uuid")  # must not raise
        db.release_lock()

    def test_malformed_lock_overwritten(self, db):
        db.set_meta("lock", "not-valid-json{{")
        db.acquire_lock("any-uuid")  # must not raise
        db.release_lock()

    def test_release_without_lock_no_error(self, db):
        db.release_lock()  # must not raise
