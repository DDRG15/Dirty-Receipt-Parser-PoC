

"""
tests/test_resilience.py -- V13.6  |  Crash/Restart Acceptance Tests
=====================================================================
Verifies the zero-duplicate guarantee after a mid-run crash and restart
under the Transactional Outbox Pattern.

V13.5 semantics change from V13.4
----------------------------------
JSONL files are no longer written during the processing loop.  They are
exported atomically from SQLite at end-of-run.  After a mid-run crash:
  * SQLite contains all committed rows.
  * JSONL files are absent (never written) or hold a prior export (resume).
  * The next restart processes remaining receipts, then exports JSONL.

Assertions after crash (mid-run):
  * DB has no duplicates (idempotency guard).
  * JSONL is absent or empty -- this is CORRECT, not a failure.

Assertions after restart (full run):
  a. Zero duplicate receipt_ids in JSONL files.
  b. DB row count == JSONL unique-id count.
  c. DB contains all processable receipts (SAMPLE_SIZE - OOM_count).

Run:
  pytest tests/test_resilience.py -v -s
  (or standalone: python scripts/crash_restart_test.py)
"""

import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

# -- constants ----------------------------------------------------------------
SAMPLE_SIZE       = 2_000
GENERATOR_SEED    = 42
KILL_AFTER_S      = 1.5
PIPELINE_TIMEOUT  = 90
OUTPUT_DIR        = pathlib.Path("data/output")
INPUT_FILE        = pathlib.Path("data/raw_samples/dirty_batch.txt")
GENERATOR_INPUT   = pathlib.Path("data/raw_samples/crash_test_batch.txt")
JSONL_FILES       = [
    OUTPUT_DIR / "success.jsonl",
    OUTPUT_DIR / "tier2_fixed.jsonl",
    OUTPUT_DIR / "manual_review.jsonl",
]
DB_FILE      = OUTPUT_DIR / "processed_index.db"
METRICS_FILE = OUTPUT_DIR / "metrics.json"
CKPT_FILE    = OUTPUT_DIR / "checkpoint.json"

_OOM_COUNT  = SAMPLE_SIZE // 1000
PROCESSABLE = SAMPLE_SIZE - _OOM_COUNT

_UNLINK_RETRIES = 10
_UNLINK_DELAY_S = 0.25


# -- helpers ------------------------------------------------------------------

def _safe_remove(path: pathlib.Path) -> bool:
    for _ in range(_UNLINK_RETRIES):
        if not path.exists():
            return True
        try:
            path.unlink()
            return True
        except PermissionError:
            time.sleep(_UNLINK_DELAY_S)
        except Exception:
            break
    return not path.exists()


def _clean() -> None:
    targets = [
        *JSONL_FILES,
        DB_FILE,
        DB_FILE.with_suffix(".db-wal"),
        DB_FILE.with_suffix(".db-shm"),
        METRICS_FILE,
        CKPT_FILE,
        OUTPUT_DIR / "checkpoint.json.tmp",
        OUTPUT_DIR / "metrics.json.tmp",
    ]
    # Also remove any .tmp export files left from a killed export phase.
    for jf in JSONL_FILES:
        targets.append(jf.with_suffix(".jsonl.tmp"))
    for f in targets:
        if not _safe_remove(f):
            import warnings
            warnings.warn(
                f"_clean: could not remove {f} after {_UNLINK_RETRIES} retries"
            )


def _assert_clean_state() -> None:
    still_present = [str(p) for p in [DB_FILE, CKPT_FILE] if p.exists()]
    if still_present:
        time.sleep(_UNLINK_DELAY_S * 2)
        _clean()
        still_present = [str(p) for p in [DB_FILE, CKPT_FILE] if p.exists()]
    if still_present:
        try:
            if sys.platform == "win32":
                proc_list = subprocess.check_output(
                    ["tasklist", "/FI", "IMAGENAME eq python.exe"],
                    text=True, timeout=5,
                )
            else:
                proc_list = subprocess.check_output(
                    ["pgrep", "-la", "python"], text=True, timeout=5,
                )
        except Exception as exc:
            proc_list = "(process list unavailable: " + str(exc) + ")"
        raise RuntimeError(
            "Safe Start failed: files still present after cleanup: "
            + str(still_present)
            + "\nRunning Python processes:\n" + proc_list
        )


def _safe_spawn() -> subprocess.Popen:
    """Assert clean state, then spawn main.py."""
    _assert_clean_state()
    return subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _spawn() -> subprocess.Popen:
    """Spawn without the pre-spawn guard (DB may exist from prior run)."""
    return subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _jsonl_ids() -> list[int]:
    ids: list[int] = []
    for path in JSONL_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    ids.append(int(json.loads(line)["receipt_id"]))
                except Exception:
                    pass
    return ids


def _db_ids() -> list[int]:
    """Return all receipt_ids from the DB, ordered.

    Uses an explicit conn.close() rather than `with sqlite3.connect(...) as
    conn` because Python's sqlite3 context manager commits/rolls back the
    transaction but does NOT close the connection.  On Windows NTFS an
    unclosed connection keeps an open file handle on the DB, which blocks
    _clean() from deleting the file and causes _assert_clean_state() to
    raise RuntimeError at the start of the next test.
    """
    if not DB_FILE.exists():
        return []
    conn = sqlite3.connect(str(DB_FILE))
    try:
        return [r[0] for r in conn.execute(
            "SELECT receipt_id FROM processed ORDER BY receipt_id"
        ).fetchall()]
    finally:
        conn.close()


def _kill(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=4)
        except Exception:
            pass


# -- fixtures -----------------------------------------------------------------

@pytest.fixture(scope="module")
def generated_input():
    result = subprocess.run(
        [
            sys.executable, "-m", "src.generator",
            "--total", str(SAMPLE_SIZE),
            "--seed",  str(GENERATOR_SEED),
            "--output", str(GENERATOR_INPUT),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"Generator failed:\n{result.stderr}"
    return GENERATOR_INPUT


@pytest.fixture(autouse=True)
def use_crash_input(generated_input):
    import shutil
    backup = INPUT_FILE.with_suffix(".bak.txt")
    had_original = INPUT_FILE.exists()
    if had_original:
        INPUT_FILE.rename(backup)
    shutil.copy(str(generated_input), str(INPUT_FILE))

    _clean()
    yield

    _clean()
    if had_original and backup.exists():
        INPUT_FILE.unlink(missing_ok=True)
        backup.rename(INPUT_FILE)
    if not generated_input.exists() and INPUT_FILE.exists():
        shutil.copy(str(INPUT_FILE), str(generated_input))


# =============================================================================
# Tests
# =============================================================================

class TestCrashRestart:

    def test_no_duplicates_after_crash_and_restart(self) -> None:
        """
        Full crash/restart cycle under the Transactional Outbox Pattern.

        Phase 1 -- hard kill mid-processing:
          JSONL files are NOT written during the processing loop (by design).
          After the kill, JSONL may be absent.  This is correct.
          The DB must have no duplicates.

        Phase 2 -- restart to completion:
          The pipeline resumes from the checkpoint, skips already-committed
          rows (idempotency), and at end-of-run exports JSONL atomically.
          After completion:
            a. Zero duplicate receipt_ids in JSONL.
            b. DB row count == JSONL unique-id count.
            c. DB holds all processable receipts.
        """
        # Phase 1: kill mid-batch.
        proc1 = _safe_spawn()
        time.sleep(KILL_AFTER_S)
        _kill(proc1)
        time.sleep(0.1)   # let OS release file handles

        # After crash: DB must have no duplicate receipt_ids.
        db_ids_after_crash = _db_ids()
        assert len(db_ids_after_crash) == len(set(db_ids_after_crash)), (
            "DB duplicates found after crash -- idempotency guard broken"
        )
        # JSONL absence is EXPECTED in the outbox pattern; do not assert
        # JSONL completeness here.

        # Phase 2: restart (DB preserved intentionally).
        proc2 = _spawn()
        try:
            proc2.communicate(timeout=PIPELINE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(proc2)
            pytest.fail(f"Pipeline timed out after {PIPELINE_TIMEOUT}s on restart")

        # Assertion a: zero duplicates in exported JSONL.
        jsonl_ids = _jsonl_ids()
        dupes = len(jsonl_ids) - len(set(jsonl_ids))
        assert dupes == 0, (
            f"Found {dupes} duplicate receipt_ids in JSONL after restart"
        )

        # Assertion b: DB rows == JSONL unique ids.
        db_ids = _db_ids()
        assert len(db_ids) == len(set(jsonl_ids)), (
            f"DB has {len(db_ids)} rows but JSONL has {len(set(jsonl_ids))} unique ids"
        )

        # Assertion c: all processable receipts present.
        assert len(db_ids) == PROCESSABLE, (
            f"DB should contain {PROCESSABLE} processable receipts; "
            f"got {len(db_ids)}  (OOM_discarded={_OOM_COUNT})"
        )

    def test_db_stores_full_payload(self) -> None:
        """The outbox DB must store status and the full JSON payload per row."""
        from main import open_db, insert_batch, close_db

        conn = open_db(DB_FILE)
        payload = {
            "receipt_id": 99999,
            "status": "SUCCESS",
            "vendor": "METRO",
            "date": "01/01/2024",
            "total": {"currency": "PEN", "amount": "10.00"},
            "routing_reasons": [],
        }
        first  = insert_batch(conn, [payload])
        second = insert_batch(conn, [payload])

        row = conn.execute(
            "SELECT status, payload FROM processed WHERE receipt_id = 99999"
        ).fetchone()
        close_db(conn)

        assert len(first)  == 1, "First insert must succeed"
        assert len(second) == 0, "Second insert must be silently ignored"
        assert row is not None, "Row must exist in DB"
        assert row[0] == "SUCCESS", f"Expected status=SUCCESS, got {row[0]!r}"
        stored = json.loads(row[1])
        assert stored["receipt_id"] == 99999
        assert stored["vendor"] == "METRO"

    def test_export_jsonl_is_idempotent(self) -> None:
        """Calling export_jsonl() twice on the same DB must produce identical files."""
        from main import open_db, insert_batch, close_db, export_jsonl

        conn = open_db(DB_FILE)
        payloads = [
            {"receipt_id": i, "status": "SUCCESS",
             "vendor": "METRO", "date": "01/01/2024",
             "total": None, "routing_reasons": []}
            for i in range(1, 6)
        ]
        insert_batch(conn, payloads)
        close_db(conn)

        conn2 = sqlite3.connect(str(DB_FILE), timeout=30)
        counts1 = export_jsonl(conn2)
        conn2.close()

        snap1 = {p: p.read_text() for p in JSONL_FILES if p.exists()}

        conn3 = sqlite3.connect(str(DB_FILE), timeout=30)
        counts2 = export_jsonl(conn3)
        conn3.close()

        snap2 = {p: p.read_text() for p in JSONL_FILES if p.exists()}

        assert counts1 == counts2, "counts must match on re-export"
        assert snap1 == snap2, "file contents must be identical on re-export"

    def test_metrics_json_written_on_clean_run(self) -> None:
        """A clean run must produce valid metrics.json with correct totals."""
        proc = _safe_spawn()
        try:
            proc.communicate(timeout=PIPELINE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(proc)
            pytest.fail("Pipeline timed out")

        assert METRICS_FILE.exists()
        m = json.loads(METRICS_FILE.read_text())
        assert "last_processed_id"  in m
        assert "processed_total"    in m
        assert "records_per_second" in m
        assert "counts"             in m
        assert m["processed_total"] == PROCESSABLE, (
            f"expected processed_total={PROCESSABLE}, got {m['processed_total']}"
        )


