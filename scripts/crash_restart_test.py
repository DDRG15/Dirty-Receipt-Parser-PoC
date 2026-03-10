

"""
scripts/crash_restart_test.py -- V13.5  |  Standalone Crash/Restart Acceptance Test
=====================================================================================
Run directly (no pytest required):
  python scripts/crash_restart_test.py

Transactional Outbox semantics
-------------------------------
JSONL files are NOT written during the processing loop.  After a hard kill,
JSONL absence is correct; the DB holds all committed rows.  After restart,
export_jsonl() writes the JSONL files atomically from the DB.

Algorithm
---------
1. Clean output state.
2. Generate SAMPLE_SIZE receipts.
3. Spawn main.py; kill after KILL_AFTER_S seconds.
4. Assert DB has no duplicates (JSONL absence is acceptable).
5. Restart main.py; let it complete.
6. Assert:
   a. Zero duplicate receipt_ids in final JSONL.
   b. DB row count == JSONL unique-id count.
   c. DB contains all processable receipts.
   d. metrics.json exists.
"""

import json
import logging
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crash_test")

SAMPLE_SIZE      = 2_000
GENERATOR_SEED   = 42
KILL_AFTER_S     = 1.5
PIPELINE_TIMEOUT = 90

_OOM_COUNT  = SAMPLE_SIZE // 1000
PROCESSABLE = SAMPLE_SIZE - _OOM_COUNT

OUTPUT_DIR   = pathlib.Path("data/output")
INPUT_FILE   = pathlib.Path("data/raw_samples/dirty_batch.txt")
CRASH_INPUT  = pathlib.Path("data/raw_samples/crash_test_batch.txt")
JSONL_FILES  = [
    OUTPUT_DIR / "success.jsonl",
    OUTPUT_DIR / "tier2_fixed.jsonl",
    OUTPUT_DIR / "manual_review.jsonl",
]
DB_FILE      = OUTPUT_DIR / "processed_index.db"
METRICS_FILE = OUTPUT_DIR / "metrics.json"


def _clean() -> None:
    targets = [
        *JSONL_FILES, DB_FILE, METRICS_FILE,
        OUTPUT_DIR / "checkpoint.json",
        OUTPUT_DIR / "checkpoint.json.tmp",
        OUTPUT_DIR / "metrics.json.tmp",
    ]
    for jf in JSONL_FILES:
        targets.append(jf.with_suffix(".jsonl.tmp"))
    for f in targets:
        if f.exists():
            f.unlink()
    log.info("Output state cleared.")


def _jsonl_ids() -> list[int]:
    ids: list[int] = []
    for path in JSONL_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    ids.append(int(json.loads(line)["receipt_id"]))
                except Exception:
                    pass
    return ids


def _db_count() -> int:
    if not DB_FILE.exists():
        return 0
    with sqlite3.connect(str(DB_FILE)) as conn:
        return conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]


def _db_has_no_duplicates() -> bool:
    if not DB_FILE.exists():
        return True
    with sqlite3.connect(str(DB_FILE)) as conn:
        dup = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT receipt_id FROM processed"
            "  GROUP BY receipt_id HAVING COUNT(*) > 1"
            ")"
        ).fetchone()[0]
    return dup == 0


def _kill(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
        log.info("PID %d terminated.", proc.pid)
    except Exception as exc:
        log.warning("Kill failed: %s -- forcing.", exc)
        try:
            proc.kill()
            proc.wait(timeout=4)
        except Exception:
            pass


def _spawn() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log.info("Started PID=%d.", proc.pid)
    return proc


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        log.error("FAIL: %s", msg)
        sys.exit(1)
    log.info("OK  -- %s", msg)


def main() -> None:
    log.info("=" * 60)
    log.info("CRASH/RESTART ACCEPTANCE TEST  --  V13.5 (Outbox)")
    log.info("=" * 60)

    _clean()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("STEP 1: Generating %d receipts (seed=%d) ...", SAMPLE_SIZE, GENERATOR_SEED)
    r = subprocess.run(
        [
            sys.executable, "-m", "src.generator",
            "--total",  str(SAMPLE_SIZE),
            "--seed",   str(GENERATOR_SEED),
            "--output", str(CRASH_INPUT),
        ],
        capture_output=True, text=True, timeout=60,
    )
    _assert(r.returncode == 0, f"Generator succeeded (rc={r.returncode})")

    import shutil
    backup = INPUT_FILE.with_suffix(".bak.txt")
    had_original = INPUT_FILE.exists()
    if had_original:
        INPUT_FILE.rename(backup)
    shutil.copy(str(CRASH_INPUT), str(INPUT_FILE))

    try:
        log.info("STEP 2: First run -- killing after %.1fs ...", KILL_AFTER_S)
        proc1 = _spawn()
        time.sleep(KILL_AFTER_S)
        _kill(proc1)
        time.sleep(0.1)

        # After a hard kill, JSONL is absent (outbox: no in-loop writes).
        # Assert DB integrity only.
        _assert(
            _db_has_no_duplicates(),
            "DB has no duplicate receipt_ids after crash"
        )
        db_after_crash = _db_count()
        log.info("DB rows after crash: %d", db_after_crash)

        log.info("STEP 3: Restarting pipeline ...")
        proc2 = _spawn()
        try:
            proc2.communicate(timeout=PIPELINE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(proc2)
            _assert(False, f"Pipeline completed within {PIPELINE_TIMEOUT}s")

        log.info("STEP 4: Verifying assertions ...")

        jsonl_ids = _jsonl_ids()
        dupes     = len(jsonl_ids) - len(set(jsonl_ids))
        _assert(dupes == 0,
                f"Zero duplicate receipt_ids in JSONL "
                f"({len(jsonl_ids)} total, {len(set(jsonl_ids))} unique)")

        db_count = _db_count()
        _assert(db_count == len(set(jsonl_ids)),
                f"DB rows ({db_count}) == JSONL unique ids ({len(set(jsonl_ids))})")

        _assert(METRICS_FILE.exists(), "metrics.json exists")

        _assert(db_count == PROCESSABLE,
                f"DB contains all {PROCESSABLE} processable receipts "
                f"(db_count={db_count}, OOM_discarded={_OOM_COUNT})")

        log.info("=" * 60)
        log.info("ALL ACCEPTANCE TESTS PASSED")
        log.info("=" * 60)

    finally:
        if had_original and backup.exists():
            INPUT_FILE.unlink(missing_ok=True)
            backup.rename(INPUT_FILE)


if __name__ == "__main__":
    main()


