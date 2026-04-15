"""SQLite state DB — WAL mode, incremental vacuum, schema migrations."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Iterator

from .exceptions import DatabaseError, LockError

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA auto_vacuum=INCREMENTAL;

CREATE TABLE IF NOT EXISTS db_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path             TEXT PRIMARY KEY,
    mtime            REAL NOT NULL,
    size             INTEGER NOT NULL,
    hash             TEXT,
    partial_hash     TEXT,
    status           TEXT NOT NULL,
    error            TEXT,
    suggested_action TEXT,
    checked_at       TEXT NOT NULL,
    scan_id          TEXT,
    notified_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_status     ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_checked_at ON files(checked_at);

CREATE TABLE IF NOT EXISTS scans (
    id            TEXT PRIMARY KEY,
    started_at    TEXT,
    finished_at   TEXT,
    total         INTEGER DEFAULT 0,
    corrupt       INTEGER DEFAULT 0,
    unreadable    INTEGER DEFAULT 0,
    scope         TEXT,
    files_per_sec REAL
);

CREATE TABLE IF NOT EXISTS scan_queue (
    scan_id  TEXT NOT NULL,
    path     TEXT NOT NULL,
    done     INTEGER DEFAULT 0,
    PRIMARY KEY (scan_id, path)
);
"""

_QUEUE_BATCH_SIZE = 10_000


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    """
    SQLite state database.

    Each public method opens its own connection — safe for the main process
    (sole writer) pattern used by the scanner. Workers never call this class.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.create_function("reverse", 1, lambda s: s[::-1] if s else s)
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            # Set schema version if not present
            conn.execute(
                "INSERT OR IGNORE INTO db_meta VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM db_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO db_meta (key, value) VALUES (?,?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # needs_check
    # ------------------------------------------------------------------

    def needs_check(
        self,
        path: str,
        mtime: float,
        size: int,
        force_recheck_days: int = 0,
    ) -> bool:
        """Return True if the file should be validated."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT mtime, size, checked_at FROM files WHERE path=?", (path,)
            ).fetchone()
            if row is None:
                return True
            if row["mtime"] != mtime or row["size"] != size:
                return True
            if force_recheck_days > 0 and row["checked_at"]:
                try:
                    checked = datetime.fromisoformat(row["checked_at"])
                    now = datetime.now(timezone.utc)
                    if checked.tzinfo is None:
                        checked = checked.replace(tzinfo=timezone.utc)
                    delta_days = (now - checked).days
                    if delta_days >= force_recheck_days:
                        return True
                except ValueError:
                    return True
            return False
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Record a validation result
    # ------------------------------------------------------------------

    def record(
        self,
        path: str,
        mtime: float,
        size: int,
        status: str,
        scan_id: str | None = None,
        error: str | None = None,
        suggested_action: str | None = None,
        hash_: str | None = None,
        partial_hash: str | None = None,
    ) -> None:
        """Write a validation result. Resets notified_at on ok→corrupt transition."""
        conn = self._connect()
        try:
            now = datetime.now(timezone.utc).isoformat()

            # Determine whether to reset notified_at
            existing = conn.execute(
                "SELECT status, notified_at FROM files WHERE path=?", (path,)
            ).fetchone()
            notified_at: str | None = existing["notified_at"] if existing else None
            if status == "corrupt" and existing and existing["status"] == "ok":
                notified_at = None  # re-corruption after fix → re-notify

            conn.execute(
                """
                INSERT OR REPLACE INTO files
                  (path, mtime, size, hash, partial_hash, status, error,
                   suggested_action, checked_at, scan_id, notified_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    path, mtime, size, hash_, partial_hash, status,
                    error, suggested_action, now, scan_id, notified_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_notified(self, paths: list[str]) -> None:
        """Set notified_at for a batch of paths."""
        if not paths:
            return
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.executemany(
                "UPDATE files SET notified_at=? WHERE path=?",
                [(now, p) for p in paths],
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_corrupt_files(
        self,
        since: str | None = None,
        ext: str | None = None,
        path_prefix: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            q = "SELECT * FROM files WHERE status='corrupt'"
            params: list[Any] = []
            if since:
                q += " AND checked_at >= ?"
                params.append(since)
            if ext:
                q += " AND path LIKE ?"
                params.append(f"%.{ext.lstrip('.')}")
            if path_prefix:
                q += " AND path LIKE ?"
                params.append(f"{path_prefix}%")
            q += " ORDER BY checked_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            totals = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM files GROUP BY status"
            ).fetchall()
            total_all = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            total_size = conn.execute("SELECT SUM(size) FROM files").fetchone()[0] or 0
            return {
                "total": total_all,
                "total_size_bytes": total_size,
                "by_status": {r["status"]: r["cnt"] for r in totals},
            }
        finally:
            conn.close()

    def get_format_counts(self) -> list[tuple[str, int]]:
        """Return file counts grouped by extension, sorted by count descending."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN path LIKE '%.%'
                            THEN lower(substr(path, length(path) - instr(reverse(path), '.') + 1))
                        ELSE '(no ext)'
                    END AS ext,
                    COUNT(*) AS cnt
                FROM files
                GROUP BY ext
                ORDER BY cnt DESC
                """
            ).fetchall()
            return [(r["ext"], r["cnt"]) for r in rows]
        finally:
            conn.close()

    def get_stale_count(self, days: int) -> int:
        """Return count of files not checked in the last `days` days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM files WHERE checked_at < ?", (cutoff,)
            ).fetchone()[0]
        finally:
            conn.close()

    def get_top_errors(self, limit: int = 5) -> list[tuple[str, int]]:
        """Return most frequent error strings with counts."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT error, COUNT(*) as cnt FROM files"
                " WHERE error IS NOT NULL AND error != ''"
                " GROUP BY error ORDER BY cnt DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r["error"], r["cnt"]) for r in rows]
        finally:
            conn.close()

    def get_new_corrupt(self) -> list[dict[str, Any]]:
        """Return corrupt files not yet notified."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM files WHERE status='corrupt' AND notified_at IS NULL"
                " ORDER BY checked_at DESC LIMIT 1000"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_new_unreadable(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM files WHERE status='unreadable' AND notified_at IS NULL"
                " ORDER BY checked_at DESC LIMIT 1000"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Scan lifecycle
    # ------------------------------------------------------------------

    def create_scan(self, scope: str | None = None) -> str:
        scan_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO scans (id, started_at, scope) VALUES (?,?,?)",
                (scan_id, now, scope),
            )
            conn.commit()
        finally:
            conn.close()
        return scan_id

    def finish_scan(
        self,
        scan_id: str,
        total: int,
        corrupt: int,
        unreadable: int,
        files_per_sec: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE scans SET finished_at=?, total=?, corrupt=?,
                   unreadable=?, files_per_sec=? WHERE id=?""",
                (now, total, corrupt, unreadable, files_per_sec, scan_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        """Return a single scan record by ID, or None if not found."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_scan_file_counts(self, scan_id: str) -> dict[str, int]:
        """Return per-status counts from the files table for a given scan_id.

        Used on resume to restore ScanStats from actually-processed files,
        since the scans table counters are only written by finish_scan().
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM files WHERE scan_id=? GROUP BY status",
                (scan_id,),
            ).fetchall()
        finally:
            conn.close()
        by_status = {r["status"]: r["cnt"] for r in rows}
        return {
            "total":       sum(by_status.values()),
            "ok":          by_status.get("ok", 0),
            "corrupt":     by_status.get("corrupt", 0),
            "unreadable":  by_status.get("unreadable", 0),
            "unsupported": by_status.get("unsupported", 0),
            "error":       by_status.get("error", 0),
        }

    def list_scans(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_last_files_per_sec(self) -> float | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT files_per_sec FROM scans WHERE finished_at IS NOT NULL"
                " ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            return float(row["files_per_sec"]) if row and row["files_per_sec"] else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # scan_queue
    # ------------------------------------------------------------------

    def queue_paths(self, scan_id: str, paths: list[str]) -> None:
        """Batch-insert paths into scan_queue. Only inserts in chunks."""
        conn = self._connect()
        try:
            for i in range(0, len(paths), _QUEUE_BATCH_SIZE):
                batch = paths[i : i + _QUEUE_BATCH_SIZE]
                conn.execute("BEGIN")
                conn.executemany(
                    "INSERT OR IGNORE INTO scan_queue (scan_id, path) VALUES (?,?)",
                    [(scan_id, p) for p in batch],
                )
                conn.execute("COMMIT")
        finally:
            conn.close()

    def get_pending_paths(self, scan_id: str, batch: int = 500) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path FROM scan_queue WHERE scan_id=? AND done=0 LIMIT ?",
                (scan_id, batch),
            ).fetchall()
            return [r["path"] for r in rows]
        finally:
            conn.close()

    def get_all_pending_paths(self, scan_id: str) -> list[str]:
        """Return all pending (done=0) paths for a scan — no LIMIT."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path FROM scan_queue WHERE scan_id=? AND done=0",
                (scan_id,),
            ).fetchall()
            return [r["path"] for r in rows]
        finally:
            conn.close()

    def mark_queue_done(self, scan_id: str, paths: list[str]) -> None:
        conn = self._connect()
        try:
            conn.executemany(
                "UPDATE scan_queue SET done=1 WHERE scan_id=? AND path=?",
                [(scan_id, p) for p in paths],
            )
            conn.commit()
        finally:
            conn.close()

    def cleanup_scan_queue(self, scan_id: str) -> None:
        """Remove completed entries immediately after scan finishes."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM scan_queue WHERE scan_id=? AND done=1", (scan_id,))
            conn.commit()
        finally:
            conn.close()

    def cleanup_stale_queues(self, max_age_days: int = 7) -> int:
        """Remove incomplete queues older than max_age_days. Returns count removed."""
        conn = self._connect()
        try:
            cutoff = datetime.now(timezone.utc).isoformat()[:10]
            rows = conn.execute(
                "SELECT DISTINCT sq.scan_id FROM scan_queue sq"
                " JOIN scans s ON sq.scan_id = s.id"
                " WHERE s.finished_at IS NULL"
                " AND date(s.started_at) <= date(?, ?)",
                (cutoff, f"-{max_age_days} days"),
            ).fetchall()
            stale_ids = [r["scan_id"] for r in rows]
            for sid in stale_ids:
                conn.execute("DELETE FROM scan_queue WHERE scan_id=?", (sid,))
            conn.commit()
            return len(stale_ids)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, path_prefix: str | None = None) -> int:
        """Delete file records (optionally filtered by path prefix). Returns count."""
        conn = self._connect()
        try:
            if path_prefix:
                cur = conn.execute(
                    "DELETE FROM files WHERE path LIKE ?", (f"{path_prefix}%",)
                )
            else:
                cur = conn.execute("DELETE FROM files")
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Vacuum
    # ------------------------------------------------------------------

    def incremental_vacuum(self, pages: int = 1000) -> None:
        """Non-blocking incremental vacuum."""
        conn = self._connect()
        try:
            conn.execute(f"PRAGMA incremental_vacuum({pages})")
            conn.commit()
            self.set_meta("vacuum_last_run", datetime.now(timezone.utc).isoformat())
        finally:
            conn.close()

    def should_vacuum(self, min_days: int = 7) -> bool:
        last = self.get_meta("vacuum_last_run")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(last)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).days >= min_days
        except ValueError:
            return True

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    def export_json(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM files").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def import_json(
        self,
        records: list[dict[str, Any]],
        force: bool = False,
        path_map: dict[str, str] | None = None,
    ) -> tuple[int, int]:
        """Import records. Returns (inserted, skipped)."""
        inserted = skipped = 0
        conn = self._connect()
        try:
            for rec in records:
                path = rec.get("path", "")
                if not path:
                    skipped += 1
                    continue

                # Apply path remapping
                if path_map:
                    for old, new in path_map.items():
                        # Ensure boundary: prefix must end at a path separator
                        old_norm = old.rstrip("/")
                        rest = path[len(old_norm):]
                        if path.startswith(old_norm) and (rest == "" or rest.startswith("/")):
                            path = new.rstrip("/") + rest
                            break

                # Skip paths that don't exist on this filesystem
                if not Path(path).exists():
                    skipped += 1
                    continue

                if force:
                    conn.execute(
                        "INSERT OR REPLACE INTO files (path,mtime,size,hash,partial_hash,"
                        "status,error,suggested_action,checked_at,scan_id,notified_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            path, rec.get("mtime", 0), rec.get("size", 0),
                            rec.get("hash"), rec.get("partial_hash"),
                            rec.get("status", "error"), rec.get("error"),
                            rec.get("suggested_action"), rec.get("checked_at", ""),
                            rec.get("scan_id"), rec.get("notified_at"),
                        ),
                    )
                    inserted += 1
                else:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO files (path,mtime,size,hash,partial_hash,"
                        "status,error,suggested_action,checked_at,scan_id,notified_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            path, rec.get("mtime", 0), rec.get("size", 0),
                            rec.get("hash"), rec.get("partial_hash"),
                            rec.get("status", "error"), rec.get("error"),
                            rec.get("suggested_action"), rec.get("checked_at", ""),
                            rec.get("scan_id"), rec.get("notified_at"),
                        ),
                    )
                    if cur.rowcount:
                        inserted += 1
                    else:
                        skipped += 1
            conn.commit()
        finally:
            conn.close()
        return inserted, skipped

    # ------------------------------------------------------------------
    # Instance lock
    # ------------------------------------------------------------------

    def acquire_lock(self, boot_uuid: str) -> None:
        """
        Write lock to db_meta. Raises LockError if another live instance holds it.
        """
        existing = self.get_meta("lock")
        if existing:
            try:
                info = json.loads(existing)
                pid = info.get("pid", 0)
                stored_uuid = info.get("boot_uuid", "")
                # Check if the PID is still alive
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True  # process exists, no permission to signal
                if alive and pid != os.getpid() and stored_uuid != boot_uuid:
                    raise LockError(
                        f"Another sdog instance is running (PID {pid}). "
                        "Use 'sdog status' to check."
                    )
                # Stale lock (dead PID, PID reuse, or same-process re-acquire) — clean up
            except (json.JSONDecodeError, KeyError):
                pass  # malformed lock — overwrite

        self.set_meta("lock", json.dumps({"pid": os.getpid(), "boot_uuid": boot_uuid}))

    def release_lock(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM db_meta WHERE key='lock'")
            conn.commit()
        finally:
            conn.close()
