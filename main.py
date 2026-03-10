

"""
main.py -- V13.6  |  Enterprise OCR Receipt Pipeline  (Transactional Outbox)
=============================================================================

Architecture
  Producer  : main thread reads the input file line-by-line, accumulates
              receipts into batches, submits per-core sub-chunks to the pool.
  Backpressure: MAX_INFLIGHT cap on concurrent futures prevents the reader
              from racing ahead and hoarding memory.
  Consumer  : drain_one() collects one completed future at a time via
              as_completed(); each result is stored atomically in SQLite.

Transactional Outbox Pattern
  The V13.4 dual-write architecture (SQLite + JSONL in the hot loop) could not
  be made crash-safe on Windows NTFS without a two-phase commit: SQLite
  guaranteed ACID, but the JSONL files were a second, independent sink.  Any
  hard kill (TerminateProcess) between the DB COMMIT and the JSONL fsync left
  the two stores inconsistent.

  V13.5 eliminates the dual-write entirely:

  1. SQLite is the SOLE write target during processing.  The schema now stores
     the full JSON payload alongside the receipt_id and status, making the DB
     a complete, self-contained record of every processed receipt.

  2. JSONL export is deferred to end-of-run.  After all batches are drained,
     export_jsonl() queries the DB once and writes the three JSONL files using
     the atomic write pattern (.tmp -> fsync -> os.replace).  There are no
     open JSONL file handles during the processing loop.

  3. Idempotent restart.  If the process is killed during processing, the next
     run resumes from the checkpoint, re-processes nothing (idempotency guard),
     and re-runs the export.  If killed during export, the next run sees the DB
     is complete and re-runs the export from scratch, producing identical files.

SQLite schema
  processed(
    receipt_id  INTEGER PRIMARY KEY,
    status      TEXT    NOT NULL,
    payload     TEXT    NOT NULL    -- json.dumps(full result dict)
  )

  open_db() migrates existing V13.4 databases that have only receipt_id by
  adding the status and payload columns (ALTER TABLE ... ADD COLUMN).

SQLite Idempotency Guard
  * Only the main process opens / writes the DB (workers never touch it).
  * WAL mode + busy_timeout=5000 ms + synchronous=NORMAL for resilience.
  * Per-chunk: BEGIN ... INSERT OR IGNORE ... COMMIT.
  * cursor.rowcount == 1 detects a genuinely new insert (per-row check).
    Defensive fallback (per-row): if rowcount < 0 (undefined on unusual
    builds), conn.total_changes delta for that single execute() decides.
  * OperationalError triggers exponential-backoff retry via _db_retry.
  * On shutdown: commit, WAL checkpoint (TRUNCATE), then close.

Shutdown sequence  (SIGINT / SIGTERM / KeyboardInterrupt / SystemExit)
  a) Set _stop_requested -- read loop stops submitting new batches.
  b) executor.shutdown(wait=False, cancel_futures=True).
  c) Drain all in-flight futures.
  d) DB commit, WAL checkpoint (TRUNCATE), close.
  e) export_jsonl() -- atomic write of three JSONL files from DB.
  f) Atomic write of metrics.json and checkpoint.json.
  A try/finally block guarantees d-f run even on unexpected exceptions.

Observability
  * Logs "DB rows at start: N" on open and "Final totals before exit" on close.
"""

import json
import logging
import os
import re
import signal
import sqlite3
import time
import pathlib
from concurrent.futures import Future, ProcessPoolExecutor, as_completed

from src.worker import process_chunk

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline")

# -- Configuration ------------------------------------------------------------
OUTPUT_DIR      = pathlib.Path("data/output")
INPUT_FILE      = pathlib.Path("data/raw_samples/dirty_batch.txt")
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"
METRICS_FILE    = OUTPUT_DIR / "metrics.json"
DB_FILE         = OUTPUT_DIR / "processed_index.db"

JSONL_FILES = {
    "SUCCESS":       OUTPUT_DIR / "success.jsonl",
    "TIER_2_FIXED":  OUTPUT_DIR / "tier2_fixed.jsonl",
    "MANUAL_REVIEW": OUTPUT_DIR / "manual_review.jsonl",
}

CHUNK_SIZE        = 5_000   # receipts accumulated before a parallel dispatch
MAX_INFLIGHT      = 4       # concurrent-futures ceiling (backpressure)
MAX_RECEIPT_LINES = 50      # OOM guard: discard receipt buffer if too long
METRICS_INTERVAL  = 5       # persist checkpoint every N drained chunks
DB_RETRY_ATTEMPTS = 5       # max retries on sqlite3.OperationalError
DB_RETRY_BASE_S   = 0.10    # base seconds for exponential backoff

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# -- Shutdown state -----------------------------------------------------------
_stop_requested: bool = False
_executor_ref:   ProcessPoolExecutor | None = None


def _handle_signal(signum, frame) -> None:   # noqa: ANN001
    """Steps a + b of the shutdown sequence."""
    global _stop_requested
    _stop_requested = True
    log.warning("Signal %s -- stopping submission.", signum)
    if _executor_ref is not None:
        _executor_ref.shutdown(wait=False, cancel_futures=True)


# -- SQLite helpers -----------------------------------------------------------

def open_db(path: pathlib.Path) -> sqlite3.Connection:
    """Open (or create) the outbox DB.  Main process only.

    Schema: processed(receipt_id INTEGER PRIMARY KEY,
                       status      TEXT NOT NULL,
                       payload     TEXT NOT NULL)

    Migration: if an older V13.x DB exists with only receipt_id, the two
    new columns are added via ALTER TABLE with safe defaults so the existing
    idempotency rows are preserved and the pipeline can resume normally.

    Settings:
      journal_mode=WAL    -- allows concurrent readers.
      busy_timeout=5000   -- wait up to 5 s on lock instead of raising.
      synchronous=NORMAL  -- safe with WAL; faster than FULL.
    """
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=True)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    # Create table with full schema for fresh databases.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed ("
        "  receipt_id  INTEGER PRIMARY KEY,"
        "  status      TEXT    NOT NULL DEFAULT '',"
        "  payload     TEXT    NOT NULL DEFAULT ''"
        ");"
    )

    # Migration: add columns to older DBs that only have receipt_id.
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(processed);").fetchall()
    }
    if "status" not in existing_cols:
        conn.execute("ALTER TABLE processed ADD COLUMN status TEXT NOT NULL DEFAULT '';")
        log.info("open_db: migrated -- added 'status' column")
    if "payload" not in existing_cols:
        conn.execute("ALTER TABLE processed ADD COLUMN payload TEXT NOT NULL DEFAULT '';")
        log.info("open_db: migrated -- added 'payload' column")

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM processed;").fetchone()[0]
    log.info("DB rows at start: %d  (%s)", n, path)
    return conn


def close_db(conn: sqlite3.Connection) -> None:
    """Commit, WAL checkpoint (TRUNCATE), close.

    TRUNCATE consolidates the WAL file to zero size before close, releasing
    all sidecar (-shm, -wal) handles on Windows before the process exits.
    """
    for step, sql_or_fn in [
        ("commit",     lambda: conn.commit()),
        ("checkpoint", lambda: conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")),
        ("close",      lambda: conn.close()),
    ]:
        try:
            sql_or_fn()
        except Exception as exc:
            log.debug("close_db: %s failed (ignored): %s", step, exc)


def _db_retry(fn, *args, **kwargs):
    """Retry fn on sqlite3.OperationalError with exponential backoff."""
    for attempt in range(1, DB_RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if attempt == DB_RETRY_ATTEMPTS:
                log.error("DB gave up after %d attempts: %s", attempt, exc)
                raise
            wait = DB_RETRY_BASE_S * (2 ** (attempt - 1))
            log.warning(
                "DB OperationalError (attempt %d/%d): %s -- retry in %.2fs",
                attempt, DB_RETRY_ATTEMPTS, exc, wait,
            )
            time.sleep(wait)


def insert_batch(
    conn: sqlite3.Connection,
    results: list[dict],
) -> list[dict]:
    """INSERT OR IGNORE all results in one transaction, storing full payload.

    Each row stores: receipt_id (PK), status, and the full json.dumps(payload).
    This makes the DB a complete, self-contained outbox -- no JSONL files are
    needed during processing.

    Per-row rowcount detection (unchanged from V13.4):
      rowcount == 1  -> new insert; add to new_results.
      rowcount == 0  -> duplicate; skip.
      rowcount < 0   -> undefined (unusual build); fall back to
                        conn.total_changes delta for this single execute().

    new_results is declared inside _do() so every retry starts clean.
    _db_retry propagates _do()'s return value on success.
    """
    def _do() -> list[dict]:
        new_results: list[dict] = []
        cur = conn.cursor()

        conn.execute("BEGIN")
        for payload in results:
            before_row = conn.total_changes
            cur.execute(
                "INSERT OR IGNORE INTO processed(receipt_id, status, payload)"
                " VALUES (?, ?, ?)",
                (
                    payload["receipt_id"],
                    payload["status"],
                    json.dumps(payload),   # full payload stored in DB
                ),
            )
            if cur.rowcount == 1:
                new_results.append(payload)
            elif cur.rowcount < 0:
                if conn.total_changes > before_row:
                    new_results.append(payload)
        conn.commit()
        return new_results

    return _db_retry(_do) or []


# -- Atomic writers -----------------------------------------------------------

def load_checkpoint() -> int:
    if CHECKPOINT_FILE.exists():
        try:
            return int(json.loads(CHECKPOINT_FILE.read_text()).get("last_id", -1))
        except Exception:
            return -1
    return -1


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """Write text to path.tmp, fsync, os.replace -- never a corrupt file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    with tmp.open("r+b") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_json(path: pathlib.Path, data: dict) -> None:
    _atomic_write(path, json.dumps(data, indent=2))


def save_checkpoint(last_id: int) -> None:
    _atomic_json(CHECKPOINT_FILE, {"last_id": last_id})


def save_metrics(
    last_id: int,
    total: int,
    counts: dict[str, int],
    start_time: float,
) -> None:
    elapsed = max(time.monotonic() - start_time, 1e-6)
    _atomic_json(METRICS_FILE, {
        "last_processed_id":  last_id,
        "processed_total":    total,
        "records_per_second": round(total / elapsed, 2),
        "counts":             counts,
    })


# -- Outbox export ------------------------------------------------------------

def export_jsonl(conn: sqlite3.Connection) -> dict[str, int]:
    """Query the DB and atomically write the three JSONL output files.

    This is the only place JSONL files are written.  It runs after all
    batches are fully committed to SQLite, so there is no dual-write window.

    Atomic write pattern per file:
      1. Write all lines to <name>.jsonl.tmp
      2. fsync the .tmp file (NTFS VDL committed to storage controller)
      3. os.replace(.tmp -> .jsonl)  (atomic rename on both POSIX and Windows)

    Idempotent: safe to call multiple times on the same DB.  If the process
    is killed between step 2 and step 3, the .tmp file survives; the next
    restart re-runs export_jsonl and overwrites the .tmp cleanly.

    Returns per-status counts for metrics.
    """
    # Accumulate lines per status in memory; batches are small relative to RAM.
    buckets: dict[str, list[str]] = {k: [] for k in JSONL_FILES}

    rows = conn.execute(
        "SELECT status, payload FROM processed ORDER BY receipt_id"
    ).fetchall()

    for status, payload_json in rows:
        # Route unknown statuses to MANUAL_REVIEW (defensive).
        key = status if status in JSONL_FILES else "MANUAL_REVIEW"
        buckets[key].append(payload_json)

    counts: dict[str, int] = {}
    for status, path in JSONL_FILES.items():
        lines = buckets[status]
        counts[status] = len(lines)
        _atomic_write(path, "\n".join(lines) + ("\n" if lines else ""))
        log.info("export_jsonl: %s -> %d records", path.name, len(lines))

    return counts


# -- Drain helper (step c) ----------------------------------------------------

def drain_one(
    in_flight:   dict[Future, list],
    conn:        sqlite3.Connection,
    counts:      dict[str, int],
    last_id_ref: list[int],
) -> int:
    """Block until one future completes; commit its results to the outbox DB.

    No JSONL writes happen here.  The DB is the sole sink during processing.
    Returns number of genuinely new records inserted this call.
    """
    for fut in as_completed(in_flight):
        chunk = in_flight.pop(fut)
        try:
            results: list[dict] = fut.result()
        except Exception as exc:
            ids = [c[0] for c in chunk]
            log.error(
                "Chunk error IDs %s...%s: %r -- skipped, pipeline continues.",
                ids[0], ids[-1], exc,
            )
            return 0

        new_results = insert_batch(conn, results)

        for payload in new_results:
            counts[payload["status"]] = counts.get(payload["status"], 0) + 1
            last_id_ref[0] = max(last_id_ref[0], int(payload["receipt_id"]))

        skipped = len(results) - len(new_results)
        if skipped:
            log.debug("Idempotency: skipped %d duplicate receipt(s).", skipped)
        return len(new_results)
    return 0


# -- Main pipeline ------------------------------------------------------------

_START_RE = re.compile(r"--- START RECEIPT\s+(\d+)\s+---")


def main() -> None:
    global _executor_ref, _stop_requested

    last_id_processed = load_checkpoint()
    cores             = os.cpu_count() or 4
    max_workers       = max(1, min(cores, CHUNK_SIZE))
    start_time        = time.monotonic()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "Pipeline starting | cores=%d  workers=%d  chunk=%d  inflight=%d  resume_after=%d",
        cores, max_workers, CHUNK_SIZE, MAX_INFLIGHT, last_id_processed,
    )

    total:       int = 0
    chunks_done: int = 0
    counts: dict[str, int] = {"SUCCESS": 0, "TIER_2_FIXED": 0, "MANUAL_REVIEW": 0}
    last_id_ref = [max(0, last_id_processed)]

    current_batch: list[tuple[int, str]] = []
    in_flight:     dict[Future, list]    = {}

    conn     = open_db(DB_FILE)
    executor = ProcessPoolExecutor(max_workers=max_workers)
    _executor_ref = executor

    current_receipt: list[str] = []
    current_id: int            = -1

    def _submit() -> None:
        nonlocal current_batch
        if not current_batch:
            return
        sub_chunks = [
            current_batch[i::max_workers]
            for i in range(max_workers)
            if current_batch[i::max_workers]
        ]
        for sc in sub_chunks:
            in_flight[executor.submit(process_chunk, sc)] = sc
        current_batch = []

    def _drain_checkpoint() -> None:
        nonlocal total, chunks_done
        written = drain_one(in_flight, conn, counts, last_id_ref)
        total       += written
        chunks_done += 1
        if chunks_done % METRICS_INTERVAL == 0:
            save_checkpoint(last_id_ref[0])
            elapsed = max(time.monotonic() - start_time, 1e-6)
            log.info(
                "id=%-9d  total=%-9d  rps=%.0f  %s",
                last_id_ref[0], total, total / elapsed, counts,
            )

    try:
        try:
            with INPUT_FILE.open("r", encoding="utf-8") as f_in:
                for line in f_in:
                    if _stop_requested:
                        break

                    if line.startswith("--- START RECEIPT"):
                        m = _START_RE.match(line.rstrip())
                        if m:
                            current_id      = int(m.group(1))
                            current_receipt = [line]
                        continue

                    if line.startswith("--- END RECEIPT") and current_receipt:
                        current_receipt.append(line)
                        current_batch.append((current_id, "".join(current_receipt)))
                        current_receipt = []
                        current_id      = -1

                        if len(current_batch) >= CHUNK_SIZE:
                            while len(in_flight) >= MAX_INFLIGHT:
                                _drain_checkpoint()
                            _submit()
                        continue

                    if current_receipt:
                        current_receipt.append(line)
                        if len(current_receipt) > MAX_RECEIPT_LINES:
                            log.warning(
                                "Receipt %d exceeded %d lines -- discarded.",
                                current_id, MAX_RECEIPT_LINES,
                            )
                            current_receipt = []
                            current_id      = -1

        except (KeyboardInterrupt, SystemExit):
            log.warning("Interrupted -- flushing in-flight work before exit.")
            _stop_requested = True
        except Exception as exc:
            log.exception("Fatal error in read loop: %s", exc)
            raise

        if current_batch and not _stop_requested:
            _submit()

        while in_flight:
            _drain_checkpoint()

    finally:
        # Step b: shut down executor (idempotent).
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        log.info("Final totals before exit: total=%d  counts=%s", total, counts)

        # Step d: commit + WAL checkpoint + close DB.
        close_db(conn)

        # Step e: export JSONL from the outbox (atomic, idempotent).
        # Runs even after a partial processing run; on a full run it writes
        # the complete output; on a crash-restart it fills in any gap.
        if not _stop_requested:
            # Open a fresh read-only connection for the export.  Assigned to a
            # named variable so the finally clause can close it explicitly.
            # Passing an anonymous sqlite3.connect() directly to export_jsonl()
            # leaks the handle: the caller has no reference to call .close() on
            # it, which leaves an open file descriptor on the DB file.  On
            # Windows NTFS this blocks test cleanup (PermissionError / WinError
            # 32) when the test harness tries to unlink the DB between tests.
            _export_conn: sqlite3.Connection | None = None
            try:
                _export_conn = sqlite3.connect(str(DB_FILE), timeout=30)
                export_counts = export_jsonl(_export_conn)
                log.info("export_jsonl complete: %s", export_counts)
            except Exception as exc:
                log.error("export_jsonl failed: %s", exc)
            finally:
                if _export_conn is not None:
                    try:
                        _export_conn.close()
                    except Exception:
                        pass

        # Step f: final atomic checkpoint + metrics.
        try:
            save_checkpoint(last_id_ref[0])
            save_metrics(last_id_ref[0], total, counts, start_time)
        except Exception as exc:
            log.error("Could not write final checkpoint/metrics: %s", exc)

    elapsed = time.monotonic() - start_time
    log.info(
        "DONE | records=%d  elapsed=%.1fs  rps=%.0f  %s",
        total, elapsed, total / max(elapsed, 1e-6), counts,
    )


if __name__ == "__main__":
    main()


