"""Stage 9 tests — scanner engine."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from semanticdog.config import Config
from semanticdog.db import Database
from semanticdog.scanner import Scanner, ScanStats, walk_paths, is_excluded, _validate_file
from tests.fixtures.generators import make_minimal_jpeg, make_minimal_png, make_not_an_image, make_truncated_jpeg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "state.db")


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=[str(tmp_path)],
        workers=1,
        raw_workers=1,
        validation_timeout_s=30,
        force_recheck_days=0,
    )


@pytest.fixture
def scanner(cfg, db):
    return Scanner(cfg, db)


# ---------------------------------------------------------------------------
# is_excluded
# ---------------------------------------------------------------------------

class TestIsExcluded:
    def test_no_patterns_never_excluded(self):
        assert is_excluded("/photos/img.jpg", []) is False

    def test_ds_store_excluded(self):
        assert is_excluded("/photos/.DS_Store", ["**/.DS_Store"]) is True

    def test_eadir_excluded(self):
        assert is_excluded("/photos/@eaDir/thumb.jpg", ["**/@eaDir/**"]) is True

    def test_non_matching_not_excluded(self):
        assert is_excluded("/photos/img.jpg", ["**/@eaDir/**"]) is False

    def test_multiple_patterns(self):
        patterns = ["**/.DS_Store", "**/*.lrprev", "**/@eaDir/**"]
        assert is_excluded("/photos/img.lrprev", patterns) is True
        assert is_excluded("/photos/img.jpg", patterns) is False

    def test_wildcard_extension(self):
        assert is_excluded("/tmp/cache/file.tmp", ["**/cache/**"]) is True


# ---------------------------------------------------------------------------
# ScanStats
# ---------------------------------------------------------------------------

class TestScanStats:
    def test_initial_zeroes(self):
        s = ScanStats()
        assert s.total == 0
        assert s.ok == 0

    def test_record_ok(self):
        s = ScanStats()
        s.record("ok")
        assert s.total == 1
        assert s.ok == 1

    def test_record_corrupt(self):
        s = ScanStats()
        s.record("corrupt")
        assert s.corrupt == 1

    def test_record_unreadable(self):
        s = ScanStats()
        s.record("unreadable")
        assert s.unreadable == 1

    def test_record_error_counts_as_error(self):
        s = ScanStats()
        s.record("error")
        assert s.error == 1

    def test_files_per_sec_positive(self):
        s = ScanStats()
        s.total = 100
        s.start_time = time.monotonic() - 10
        assert s.files_per_sec() == pytest.approx(10.0, abs=1.0)

    def test_files_per_sec_zero_when_no_time(self):
        s = ScanStats()
        s.start_time = time.monotonic()
        assert s.files_per_sec() == 0.0


# ---------------------------------------------------------------------------
# walk_paths
# ---------------------------------------------------------------------------

class TestWalkPaths:
    def test_finds_registered_files(self, tmp_path):
        make_minimal_jpeg(tmp_path / "a.jpg")
        make_minimal_png(tmp_path / "b.png")
        results = walk_paths([str(tmp_path)])
        paths = {r[0] for r in results}
        assert str(tmp_path / "a.jpg") in paths
        assert str(tmp_path / "b.png") in paths

    def test_ignores_unregistered_extensions(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        results = walk_paths([str(tmp_path)])
        assert all(not r[0].endswith(".txt") for r in results)

    def test_excludes_matching_patterns(self, tmp_path):
        sub = tmp_path / "@eaDir"
        sub.mkdir()
        make_minimal_jpeg(sub / "thumb.jpg")
        results = walk_paths([str(tmp_path)], exclude=["**/@eaDir/**"])
        paths = {r[0] for r in results}
        assert str(sub / "thumb.jpg") not in paths

    def test_does_not_follow_symlinks_by_default(self, tmp_path):
        target = tmp_path / "real_dir"
        target.mkdir()
        make_minimal_jpeg(target / "img.jpg")
        link = tmp_path / "linked"
        link.symlink_to(target)
        results = walk_paths([str(tmp_path)], follow_symlinks=False)
        # symlinked files inside linked/ should not appear
        paths = {r[0] for r in results}
        assert not any("linked" in p for p in paths)

    def test_returns_mtime_size(self, tmp_path):
        p = make_minimal_jpeg(tmp_path / "img.jpg")
        results = walk_paths([str(tmp_path)])
        assert len(results) == 1
        fpath, mtime, size = results[0]
        assert mtime == pytest.approx(p.stat().st_mtime, abs=1.0)
        assert size == p.stat().st_size

    def test_empty_directory(self, tmp_path):
        assert walk_paths([str(tmp_path)]) == []

    def test_multiple_roots(self, tmp_path):
        dir1 = tmp_path / "d1"
        dir2 = tmp_path / "d2"
        dir1.mkdir(); dir2.mkdir()
        make_minimal_jpeg(dir1 / "a.jpg")
        make_minimal_png(dir2 / "b.png")
        results = walk_paths([str(dir1), str(dir2)])
        assert len(results) == 2


# ---------------------------------------------------------------------------
# _validate_file (module-level worker function)
# ---------------------------------------------------------------------------

class TestValidateFileWorker:
    def test_known_extension_returns_result(self, tmp_path):
        p = make_minimal_jpeg(tmp_path / "img.jpg")
        r = _validate_file(str(p))
        assert r.status in ("ok", "corrupt", "unsupported", "unreadable", "error")

    def test_unknown_extension_returns_unsupported(self, tmp_path):
        p = tmp_path / "file.xyz_unknown"
        p.write_bytes(b"\x00" * 10)
        r = _validate_file(str(p))
        assert r.status == "unsupported"

    def test_never_raises(self, tmp_path):
        p = make_not_an_image(tmp_path / "garbage.jpg")
        try:
            r = _validate_file(str(p))
            assert r.status in ("ok", "corrupt", "unsupported", "unreadable", "error")
        except Exception as exc:
            pytest.fail(f"_validate_file raised: {exc}")


# ---------------------------------------------------------------------------
# Scanner.estimate
# ---------------------------------------------------------------------------

class TestScannerEstimate:
    def test_estimate_counts_new_files(self, scanner, tmp_path):
        make_minimal_jpeg(tmp_path / "a.jpg")
        make_minimal_jpeg(tmp_path / "b.jpg")
        counts = scanner.estimate([str(tmp_path)])
        assert counts.get(".jpg", 0) >= 2

    def test_estimate_skips_already_checked(self, scanner, db, tmp_path):
        p = make_minimal_jpeg(tmp_path / "checked.jpg")
        st = p.stat()
        db.record(str(p), st.st_mtime, st.st_size, "ok")
        counts = scanner.estimate([str(tmp_path)])
        assert counts.get(".jpg", 0) == 0

    def test_estimate_no_db_writes(self, scanner, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        scanner.estimate([str(tmp_path)])
        assert db.get_stats()["total"] == 0


# ---------------------------------------------------------------------------
# Scanner.scan (small directory, real execution)
# ---------------------------------------------------------------------------

class TestScannerScan:
    def test_scan_records_valid_jpeg(self, scanner, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        stats = scanner.scan([str(tmp_path)])
        assert stats.total >= 1
        db_stats = db.get_stats()
        assert db_stats["total"] >= 1

    def test_scan_creates_and_finishes_scan_record(self, scanner, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        scanner.scan([str(tmp_path)])
        scans = db.list_scans()
        assert len(scans) >= 1
        assert scans[0]["finished_at"] is not None

    def test_scan_skips_already_checked_files(self, scanner, db, tmp_path):
        p = make_minimal_jpeg(tmp_path / "img.jpg")
        st = p.stat()
        db.record(str(p), st.st_mtime, st.st_size, "ok")
        stats = scanner.scan([str(tmp_path)])
        assert stats.skipped >= 1
        assert stats.total == 0

    def test_scan_acquires_and_releases_lock(self, scanner, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        scanner.scan([str(tmp_path)])
        # Lock released after scan — should be None
        assert db.get_meta("lock") is None

    def test_scan_releases_lock_on_exception(self, db, tmp_path, cfg):
        """Lock released even if scan errors mid-flight."""
        scanner = Scanner(cfg, db)
        with patch.object(scanner, "_run_pool", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                scanner.scan([str(tmp_path)])
        assert db.get_meta("lock") is None

    def test_scan_excludes_patterns(self, db, tmp_path):
        sub = tmp_path / "@eaDir"
        sub.mkdir()
        make_minimal_jpeg(sub / "thumb.jpg")
        cfg = Config(
            paths=[str(tmp_path)],
            exclude=["**/@eaDir/**"],
            workers=1, raw_workers=1,
            validation_timeout_s=30,
        )
        scanner = Scanner(cfg, db)
        stats = scanner.scan([str(tmp_path)])
        assert stats.total == 0

    def test_scan_stats_files_per_sec(self, scanner, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        stats = scanner.scan([str(tmp_path)])
        # fps may be 0 if no files processed (all skipped), otherwise positive
        assert stats.files_per_sec() >= 0.0


# ---------------------------------------------------------------------------
# TOCTOU: file changes during scan → discard
# ---------------------------------------------------------------------------

class TestTOCTOU:
    def test_changed_file_discarded(self, cfg, db, tmp_path):
        p = make_minimal_jpeg(tmp_path / "changing.jpg")
        scanner = Scanner(cfg, db)

        original_run_pool = scanner._run_pool

        def _patched_run_pool(file_list, scan_id, stats, workers, context, total_files, is_tty):
            # Mutate the file between submit and TOCTOU check by patching os.stat
            original_stat = os.stat

            call_count = [0]
            def _fake_stat(path, *args, **kwargs):
                st = original_stat(path, *args, **kwargs)
                if str(path) == str(p):
                    call_count[0] += 1
                    if call_count[0] > 0:
                        # Simulate changed file: return different size
                        class FakeStat:
                            st_mtime = st.st_mtime
                            st_size = st.st_size + 999
                        return FakeStat()
                return st

            with patch("semanticdog.scanner.os.stat", side_effect=_fake_stat):
                original_run_pool(file_list, scan_id, stats, workers, context, total_files, is_tty)

        with patch.object(scanner, "_run_pool", side_effect=_patched_run_pool):
            stats = scanner.scan([str(tmp_path)])

        # File discarded due to TOCTOU, nothing in DB
        assert stats.toctou_discards >= 1 or db.get_stats()["total"] == 0


# ---------------------------------------------------------------------------
# Shutdown / SIGTERM
# ---------------------------------------------------------------------------

class TestScannerShutdown:
    def test_shutdown_flag_stops_submission(self, cfg, db, tmp_path):
        """After shutdown set, new files are not submitted."""
        # Create many files
        for i in range(5):
            make_minimal_jpeg(tmp_path / f"img{i}.jpg")

        scanner = Scanner(cfg, db)
        scanner._shutdown.set()  # pre-set shutdown

        stats = scanner.scan([str(tmp_path)])
        # No futures submitted — all skipped or zero processed
        assert stats.total == 0


# ---------------------------------------------------------------------------
# Inline stats: ok/corrupt/unreadable updated during scan (not only at end)
# ---------------------------------------------------------------------------

class TestScannerInlineStats:
    """stats.ok / stats.corrupt / files_per_sec populated while scan runs."""

    def test_ok_count_matches_db_after_scan(self, cfg, db, tmp_path):
        for i in range(3):
            make_minimal_jpeg(tmp_path / f"img{i}.jpg")
        stats = Scanner(cfg, db).scan([str(tmp_path)])
        assert stats.ok >= 1
        assert stats.ok == db.get_stats()["by_status"].get("ok", 0)

    def test_corrupt_count_matches_db_after_scan(self, cfg, db, tmp_path):
        make_truncated_jpeg(tmp_path / "bad.jpg")
        stats = Scanner(cfg, db).scan([str(tmp_path)])
        assert stats.corrupt >= 1
        assert stats.corrupt == db.get_stats()["by_status"].get("corrupt", 0)

    def test_files_per_sec_nonzero_when_files_validated(self, cfg, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        stats = Scanner(cfg, db).scan([str(tmp_path)])
        if stats.total > 0:
            assert stats.files_per_sec() > 0.0


# ---------------------------------------------------------------------------
# Resume: cancel/resume scan preserves position across multiple interrupts
# ---------------------------------------------------------------------------

class TestScannerResume:
    def test_scan_id_populated_on_stats(self, scanner, tmp_path):
        stats = scanner.scan([str(tmp_path)])
        assert stats.scan_id != ""

    def test_resume_nonexistent_id_raises(self, scanner):
        from semanticdog.exceptions import ScanError
        with pytest.raises(ScanError, match="not found"):
            scanner.scan(resume_scan_id="nonexistent-id")

    def test_resume_completed_scan_raises(self, cfg, db, tmp_path):
        from semanticdog.exceptions import ScanError
        make_minimal_jpeg(tmp_path / "img.jpg")
        stats = Scanner(cfg, db).scan([str(tmp_path)])
        with pytest.raises(ScanError, match="already completed"):
            Scanner(cfg, db).scan(resume_scan_id=stats.scan_id)

    def test_interrupted_scan_has_null_finished_at(self, cfg, db, tmp_path):
        """Pre-set shutdown → scan record stays incomplete (finished_at IS NULL)."""
        make_minimal_jpeg(tmp_path / "img.jpg")
        scanner = Scanner(cfg, db)
        scanner._shutdown.set()
        scanner.scan([str(tmp_path)])
        scans = db.list_scans()
        assert len(scans) >= 1
        assert scans[0]["finished_at"] is None

    def test_completed_scan_has_nonnull_finished_at(self, cfg, db, tmp_path):
        make_minimal_jpeg(tmp_path / "img.jpg")
        Scanner(cfg, db).scan([str(tmp_path)])
        scans = db.list_scans()
        assert scans[0]["finished_at"] is not None

    def test_resume_skipped_files_marked_done_in_queue(self, cfg, db, tmp_path):
        """
        Regression: files already in DB that are skipped on resume must be
        marked done=1 in scan_queue. Without the fix, get_all_pending_paths
        returns the same stale entries on every subsequent resume.
        """
        p1 = make_minimal_jpeg(tmp_path / "a.jpg")
        p2 = make_minimal_jpeg(tmp_path / "b.jpg")
        # Initial scan: puts both files into the files table
        Scanner(cfg, db).scan([str(tmp_path)])

        # Simulate interrupted scan: both files still listed as pending
        interrupted_id = db.create_scan(scope=str(tmp_path))
        db.queue_paths(interrupted_id, [str(p1), str(p2)])
        assert len(db.get_all_pending_paths(interrupted_id)) == 2

        # Resume: files are already in DB → skipped → must be marked done
        Scanner(cfg, db).scan(resume_scan_id=interrupted_id)

        assert db.get_all_pending_paths(interrupted_id) == []

    def test_double_resume_pending_count_shrinks(self, cfg, db, tmp_path):
        """
        Second resume sees fewer pending files than first, not the same count.
        Verifies the fix holds across multiple interrupt/resume cycles.
        """
        files = [make_minimal_jpeg(tmp_path / f"f{i}.jpg") for i in range(4)]
        Scanner(cfg, db).scan([str(tmp_path)])  # all files in DB

        # First interrupted scan: all 4 pending
        sid1 = db.create_scan(scope=str(tmp_path))
        db.queue_paths(sid1, [str(f) for f in files])

        Scanner(cfg, db).scan(resume_scan_id=sid1)
        assert db.get_all_pending_paths(sid1) == []

        # Second interrupted scan: mark 2 as already done, 2 still pending
        sid2 = db.create_scan(scope=str(tmp_path))
        db.queue_paths(sid2, [str(f) for f in files])
        db.mark_queue_done(sid2, [str(files[0]), str(files[1])])
        assert len(db.get_all_pending_paths(sid2)) == 2

        Scanner(cfg, db).scan(resume_scan_id=sid2)
        assert db.get_all_pending_paths(sid2) == []
