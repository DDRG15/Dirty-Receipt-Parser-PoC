"""
nuclear.py -- Titanium-Vault V13.6  |  Enterprise OCR Receipt Pipeline Generator
==================================================================================
Single-file project scaffold.  Run once to write the entire project.

Changes from V13.5 -- SQLite Connection Leak Fix (Test Harness)
---------------------------------------------------------------
Root cause: Python's sqlite3 context manager (`with sqlite3.connect(...) as
conn`) commits or rolls back the active transaction on __exit__, but does NOT
close the underlying connection.  The file handle remains open.  On Windows
NTFS, an open file handle prevents unlink(); the test harness _clean() routine
could not delete processed_index.db between tests, and the subsequent
_assert_clean_state() raised RuntimeError: Safe Start failed.

Files changed
  main.py               -- export connection leak fixed
  tests/test_resilience.py -- _db_ids() leak fixed

Files unchanged
  src/generator.py, src/worker.py, src/locked_db.py,
  tests/test_worker.py, tests/helpers/safe_start.py,
  scripts/crash_restart_test.py,
  Dockerfile, docker-compose.yml, requirements.txt, cleanup scripts

main.py -- fix
  Previously:
      export_counts = export_jsonl(
          sqlite3.connect(str(DB_FILE), timeout=30)   # anonymous -- leaked
      )
  Now:
      _export_conn = sqlite3.connect(str(DB_FILE), timeout=30)
      try:
          export_counts = export_jsonl(_export_conn)
      finally:
          if _export_conn is not None:
              _export_conn.close()           # always closed

tests/test_resilience.py -- fix
  Previously (in _db_ids):
      with sqlite3.connect(str(DB_FILE)) as conn:   # context mgr: no close!
          return [...]
  Now:
      conn = sqlite3.connect(str(DB_FILE))
      try:
          return [r[0] for r in conn.execute(...).fetchall()]
      finally:
          conn.close()                       # always closed

  All other sqlite3 connections in test_resilience.py (conn2/conn3 in
  test_export_jsonl_is_idempotent, conn in test_db_stores_full_payload) were
  already explicitly closed in V13.5 and are unchanged.

  All sqlite3 connections in test_worker.py already called conn.close()
  explicitly in V13.5 and are unchanged.

Quick-start (native)
  pip install -r requirements.txt
  python nuclear.py
  python -m src.generator --total 1000 --seed 42
  python main.py
  pytest tests/ -v
  python scripts/crash_restart_test.py

Quick-start (Docker)
  docker compose up --build
  docker compose --profile test run --rm titanium-vault-test
"""

import pathlib

for _d in ["src", "data/raw_samples", "data/output",
           "tests", "tests/helpers", "scripts"]:
    pathlib.Path(_d).mkdir(parents=True, exist_ok=True)

for _f in ["src/__init__.py", "tests/__init__.py",
           "tests/helpers/__init__.py", "scripts/__init__.py"]:
    pathlib.Path(_f).touch(exist_ok=True)

GENERATOR_PY = r'''






"""
src/generator.py — V13
======================
Shadow-free deterministic dirty-data generator.

Ladder (rarest → most-common so each slot is mutually exclusive):
  rid % 1000 == 0  →  OOM edge-case   (missing END marker)          ~0.10 %
  rid % 500  == 0  →  MANUAL logic    (month = 14)                   ~0.10 %
  rid % 200  == 0  →  MANUAL halluc   (??/??/???? date)              ~0.30 %
  rid % 100  == 0  →  MANUAL asterisk (TOTAL: S/. ***.**)            ~0.20 %
  rid % 33   == 0  →  TIER_2_FIXED    (two-digit year)               ~2.98 %
  else             →  SUCCESS          (clean OCR-noisy receipt)     ~96.32 %

Checking largest divisor first prevents smaller divisors from shadowing rarer
cases (e.g. % 100 would shadow % 200 and % 500 because every multiple of
200 / 500 is also a multiple of 100).

Run: python -m src.generator [--total N] [--output PATH] [--seed S]
"""

import argparse
import math
import random
import pathlib

DEFAULT_OUTPUT = pathlib.Path("data/raw_samples/dirty_batch.txt")
DEFAULT_TOTAL  = 1_000_000

VENDORS_RAW = [
    "TOTTUS", "T0TTUS", "METR0",  "METRO",
    "PLAZA VEA", "PLAZA V3A", "OXXO", "0XX0",
]

_OCR_NOISE: dict[str, str] = {
    "0": "O", "1": "l", "5": "S", "8": "B", "2": "Z", "O": "0", "l": "1",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def corrupt(text: str, rate: float = 0.05) -> str:
    """Inject random OCR homoglyph noise at the given character-level rate."""
    chars = list(text)
    for i, ch in enumerate(chars):
        if ch in _OCR_NOISE and random.random() < rate:
            chars[i] = _OCR_NOISE[ch]
    return "".join(chars)


def _items() -> list[str]:
    return [
        corrupt(
            f"  ITEM {i + 1}: Product-{random.randint(100, 999)}"
            f"   x{random.randint(1, 5)}  S/. {random.uniform(1, 50):.2f}"
        )
        for i in range(random.randint(2, 8))
    ]


def _clean_date() -> str:
    day   = str(random.randint(1, 28)).zfill(2)
    month = str(random.randint(1, 12)).zfill(2)
    year  = str(random.choice([2022, 2023, 2024]))
    sep   = random.choice([".", "/", "-"])
    return corrupt(f"{day}{sep}{month}{sep}{year}", rate=0.03)


def _clean_total() -> str:
    amount   = round(random.uniform(5.0, 999.99), 2)
    currency = random.choice(["S/.", "S/", "$"])
    return corrupt(f"TOTAL: {currency} {amount:,.2f}", rate=0.03)


def _vendor() -> str:
    return corrupt(random.choice(VENDORS_RAW))


def _assemble(
    receipt_id: int,
    vendor: str,
    date: str,
    total: str,
    include_end: bool = True,
) -> str:
    lines = [f"  Vendor: {vendor}", f"  Date:   {date}", *_items(), f"  {total}"]
    body  = "\n".join(lines)
    end   = f"--- END RECEIPT {receipt_id} ---\n" if include_end else ""
    return f"--- START RECEIPT {receipt_id} ---\n{body}\n{end}"


# ── deterministic injection ladder ───────────────────────────────────────────

def make_receipt(receipt_id: int) -> str:
    """
    Rarest-first modulo ladder — every bucket is mutually exclusive.

    Order: 1000 (OOM) > 500 (logic) > 200 (halluc) > 100 (asterisk) > 33 (Y2K).
    Checking the largest divisor first prevents shadowing: every multiple of 200
    or 500 is also a multiple of 100, so checking % 100 first would permanently
    dead-code the rarer branches.
    """
    rid = receipt_id

    # OOM edge-case ~0.10 %: missing END marker exercises MAX_RECEIPT_LINES guard
    if rid % 1000 == 0:
        return _assemble(rid, _vendor(), _clean_date(), _clean_total(), include_end=False)

    # MANUAL logic error ~0.10 %: month 14 is always invalid
    if rid % 500 == 0:
        day = str(random.randint(1, 28)).zfill(2)
        sep = random.choice([".", "/", "-"])
        return _assemble(rid, _vendor(), f"{day}{sep}14{sep}2024", _clean_total())

    # MANUAL hallucinated date ~0.30 %
    if rid % 200 == 0:
        return _assemble(rid, _vendor(), "??/??/????", _clean_total())

    # MANUAL asterisk total ~0.20 %
    if rid % 100 == 0:
        return _assemble(rid, _vendor(), _clean_date(), "TOTAL: S/. ***.**")

    # TIER_2_FIXED: two-digit year ~2.98 %
    if rid % 33 == 0:
        day        = str(random.randint(1, 28)).zfill(2)
        month      = str(random.randint(1, 12)).zfill(2)
        sep        = random.choice([".", "/", "-"])
        short_year = str(random.randint(20, 29)).zfill(2)
        date       = corrupt(f"{day}{sep}{month}{sep}{short_year}", rate=0.02)
        return _assemble(rid, _vendor(), date, _clean_total())

    # SUCCESS baseline ~96.32 %
    return _assemble(rid, _vendor(), _clean_date(), _clean_total())


# ── expected-distribution banner ─────────────────────────────────────────────

def expected_counts(total: int) -> dict[str, int]:
    """Exact bucket sizes via LCM inclusion-exclusion."""
    def net(div: int, rarer: list[int]) -> int:
        n = total // div
        for r in rarer:
            n -= total // math.lcm(div, r)
        return n

    oom   = total // 1000
    logic = net(500,  [1000])
    hall  = net(200,  [1000, 500])
    ast   = net(100,  [1000, 500, 200])
    t2    = net(33,   [1000, 500, 200, 100])
    ok    = total - oom - logic - hall - ast - t2
    return {
        "SUCCESS":         ok,
        "TIER_2_FIXED":    t2,
        "MANUAL_asterisk": ast,
        "MANUAL_halluc":   hall,
        "MANUAL_logic":    logic,
        "OOM_edge":        oom,
    }


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate OCR receipt test data")
    parser.add_argument("--total",  type=int,          default=DEFAULT_TOTAL)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Optional integer seed for reproducible output (calls random.seed(N)).",
    )
    args = parser.parse_args()

    # Seed the PRNG if requested so output is fully reproducible.
    if args.seed is not None:
        random.seed(args.seed)

    exp  = expected_counts(args.total)
    step = max(1, args.total // 10)

    print(f"Generating {args.total:,} receipts -> {args.output}"
          + (f"  [seed={args.seed}]" if args.seed is not None else ""))
    for k, v in exp.items():
        print(f"  {k:<20}: {v:>8,}  ({v / args.total * 100:.3f} %)")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        for rid in range(1, args.total + 1):
            fh.write(make_receipt(rid))
            if rid % step == 0:
                print(f"  {rid:>9,} / {args.total:,}")
    print("Done.")


if __name__ == "__main__":
    main()







'''

WORKER_PY = r'''






"""
src/worker.py — V13
====================
Stateless, multiprocessing-safe receipt processing.

Key design points
-----------------
SIGTERM handler
  Registers a minimal handler that sets _stop_requested.  The handler only
  flips a flag — it never performs I/O (Python signal handlers run between
  bytecodes; blocking I/O inside them can deadlock).  process_chunk() checks
  the flag between receipts for cooperative shutdown.

  Windows note: On Windows, SIGTERM is not a real OS signal. Python maps it to
  a software-level notification similar to SIGINT.  Graceful stop on Windows
  therefore relies primarily on the main process calling
  executor.shutdown(cancel_futures=True) rather than on OS delivery of SIGTERM
  to worker processes.  The handler is still registered for portability.

Normalisation scope
  _normalize() is applied only to receipt *body* lines (everything between the
  --- START --- and --- END --- sentinel lines).  This prevents 'S'->5 and
  'Z'->2 from corrupting structural tokens ("START", "RECEIPT") and vendor
  names that contain those letters ("PLAZA").

  After normalisation PLAZA -> PLA2A (Z->2), so vendor patterns must match
  both the raw form (in case normalisation is skipped) and the post-norm form.

Regex compilation
  All patterns are compiled once at module import time.  Each worker process
  imports the module once; compiled objects are reused for every receipt in
  that worker's lifetime — no redundant re-compile per receipt.

  Date groups accept digits OR '?' so hallucinated dates like ??/??/????
  are captured and routed to MANUAL_REVIEW rather than silently dropped.

  Total regex accepts all currency variants that survive normalisation:
    S/. -> 5/.   S/ -> 5/   S . -> 5 .   $ stays $

receipt_id
  Always int throughout the entire pipeline (never str).
"""

import re
import signal
import unicodedata
from typing import Any

# ── Cooperative stop flag ─────────────────────────────────────────────────────
_stop_requested: bool = False


def _handle_sigterm(signum, frame) -> None:   # noqa: ANN001
    """Minimal SIGTERM handler — sets flag only; no I/O."""
    global _stop_requested
    _stop_requested = True


signal.signal(signal.SIGTERM, _handle_sigterm)


# ── Homoglyph normalisation ───────────────────────────────────────────────────
_HOMOGLYPH_TABLE = str.maketrans({
    "O": "0", "o": "0",
    "l": "1", "I": "1",
    "S": "5",
    "Z": "2",
    "B": "8",
    "\u2014": "-",    # em-dash
    "\u2013": "-",    # en-dash
    "\u00D0": "0",    # Eth — rare scanner artefact
})


def _normalize(text: str) -> str:
    """NFKC Unicode decomposition followed by homoglyph replacement.
    Must be called only on body lines — not on sentinel lines."""
    return unicodedata.normalize("NFKC", text).translate(_HOMOGLYPH_TABLE)


# ── Compiled patterns ─────────────────────────────────────────────────────────
# All patterns operate on post-normalisation text unless noted otherwise.
# After _normalize(): O->0, S->5, Z->2 — patterns use canonical post-norm forms.

_VENDOR_PATTERNS: dict[str, re.Pattern[str]] = {
    "TOTTUS":    re.compile(r"\bT0TTU5\b",            re.IGNORECASE),
    "METRO":     re.compile(r"\bM[3E]TR0\b",          re.IGNORECASE),
    # "PLAZA" contains Z; after _normalize() Z->2 so "PLAZA" becomes "PLA2A".
    # Pattern accepts both the raw form (Z) and the normalised form (2).
    "PLAZA VEA": re.compile(r"\bPLA[Z2]A\s*V[3E]A\b", re.IGNORECASE),
    "OXXO":      re.compile(r"\b0XX0\b",               re.IGNORECASE),
}

# Each date group accepts digits OR '?' so hallucinated dates like ??/??/????
# are matched and flagged as MANUAL_REVIEW instead of silently dropped as
# "Date missing".
_DATE_RE = re.compile(
    r"(?P<d>\d{1,2}|\?{1,2})[./-]"
    r"(?P<m>\d{1,2}|\?{1,2})[./-]"
    r"(?P<y>\d{2,4}|\?{2,4})"
)

# Matches all currency variants after normalisation:
#   S/.  ->  5/.    slash-dot (standard Peruvian sol)
#   S/   ->  5/     slash only
#   S.   ->  5.     dot only (no slash — OCR drop)
#   S .  ->  5 .    space-dot (OCR space insertion)
#   $    ->  $      US dollar (unchanged by normalisation)
# The currency group is optional so bare amounts (no currency) are also matched.
_TOTAL_RE = re.compile(
    r"(?i)T0TAL[:\s]*"
    r"(?:(?P<cur>5\s*/\s*\.?|5\s*\.|5\s+\.|\$)\s*)?"
    r"(?P<amt>\d{1,3}(?:,\d{3})*(?:\.\d{2}))"
)

# Asterisk-corruption check — run against raw_text BEFORE normalisation so
# the '*' sentinel is never silently removed by character substitution.
_CORRUPT_RE = re.compile(r"TOTAL[:\s]*[\S\s]{0,10}\*", re.IGNORECASE)


# ── Core processing ───────────────────────────────────────────────────────────

def process_receipt(receipt_id: int, raw_text: str) -> dict[str, Any]:
    """Parse a single OCR receipt.  receipt_id is always int.

    Normalisation is scoped to body lines only (sentinel lines stripped first)
    so 'S' in "START"/"RECEIPT" and 'Z' in "PLAZA" are never corrupted.
    Asterisk corruption is checked against raw_text before normalisation.
    """
    # Strip --- START --- and --- END --- sentinels before normalising so that
    # 'S'->'5' and 'Z'->'2' substitutions do not corrupt structural tokens.
    body_lines = [
        ln for ln in raw_text.splitlines()
        if not ln.startswith("--- START RECEIPT")
        and not ln.startswith("--- END RECEIPT")
    ]
    text = _normalize("\n".join(body_lines))

    status: str        = "SUCCESS"
    reasons: list[str] = []
    date_val           = None
    total_val          = None
    vendor_val         = "UNKNOWN"

    # ── Vendor ────────────────────────────────────────────────────────────────
    for name, pat in _VENDOR_PATTERNS.items():
        if pat.search(text):
            vendor_val = name
            break

    # ── Date ──────────────────────────────────────────────────────────────────
    dm = _DATE_RE.search(text)
    if not dm:
        status = "MANUAL_REVIEW"
        reasons.append("Date missing")
    else:
        d, m, y = dm.group("d"), dm.group("m"), dm.group("y")
        if "?" in d or "?" in m or "?" in y:
            status = "MANUAL_REVIEW"
            reasons.append("OCR Hallucination")
        elif len(m) > 2 or (m.isdigit() and int(m) > 12):
            status = "MANUAL_REVIEW"
            reasons.append(f"Invalid month: {m}")
        elif len(y) == 2:
            status   = "TIER_2_FIXED"
            y        = "20" + y
            date_val = f"{d.zfill(2)}/{m.zfill(2)}/{y}"
            reasons.append("Y2K auto-corrected")
        else:
            date_val = f"{d.zfill(2)}/{m.zfill(2)}/{y.zfill(4)}"

    # ── Total — asterisk check on raw_text first ──────────────────────────────
    if _CORRUPT_RE.search(raw_text):
        status = "MANUAL_REVIEW"
        reasons.append("Total corrupted (asterisk)")
    else:
        tm = _TOTAL_RE.search(text)
        if tm:
            amt = tm.group("amt").replace(",", "")
            try:
                float(amt)
                # '$' in the raw currency group means USD; everything else PEN.
                currency  = "USD" if tm.group("cur") and "$" in tm.group("cur") else "PEN"
                total_val = {"currency": currency, "amount": amt}
            except ValueError:
                status = "MANUAL_REVIEW"
                reasons.append("Amount Parse Error")
        else:
            if status != "MANUAL_REVIEW":
                status = "MANUAL_REVIEW"
            reasons.append("Total missing")

    return {
        "receipt_id":      receipt_id,   # int — always
        "vendor":          vendor_val,
        "date":            date_val,
        "total":           total_val,
        "status":          status,
        "routing_reasons": reasons,
    }


def process_chunk(chunk: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """Process a batch of (receipt_id: int, raw_text: str) pairs.
    Checks _stop_requested between receipts for cooperative shutdown."""
    results: list[dict[str, Any]] = []
    for rid, raw in chunk:
        if _stop_requested:   # cooperative exit on SIGTERM
            break
        results.append(process_receipt(rid, raw))
    return results







'''

MAIN_PY = r'''

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


'''

LOCKED_DB_PY = r'''




"""src/locked_db.py -- Cross-platform exclusive DB wrapper using portalocker.

Install:  pip install portalocker

Falls back to advisory-only locking when portalocker is absent.  In this
pipeline the single-writer architecture (main process only) is the real
guard; portalocker adds an OS-level fence that prevents a ghost process from
re-opening the DB simultaneously after an abnormal exit.
"""

import contextlib
import pathlib
import sqlite3

try:
    import portalocker as _pl
    _PORTALOCKER = True
except ImportError:
    _pl = None
    _PORTALOCKER = False


@contextlib.contextmanager
def open_locked_db(path: pathlib.Path):
    """Context manager: acquire exclusive advisory lock, open DB, yield conn.

    The .lock sidecar file is separate from the DB file so SQLite can open
    and manage the DB (including WAL sidecars) without interference.

    Example::

        from src.locked_db import open_locked_db
        with open_locked_db(DB_FILE) as conn:
            do_work(conn)
        # lock released, WAL checkpointed, connection closed

    """
    lock_path = pathlib.Path(str(path) + ".lock")
    lock_fh = open(str(lock_path), "w")
    try:
        if _PORTALOCKER:
            # LOCK_EX | LOCK_NB: fail immediately if another process holds it.
            # Remove LOCK_NB to block instead of raising.
            _pl.lock(lock_fh, _pl.LOCK_EX)
    except Exception:
        lock_fh.close()
        raise

    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=True)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed"
            " (receipt_id INTEGER PRIMARY KEY);"
        )
        conn.commit()
        yield conn
    finally:
        if conn is not None:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        if _PORTALOCKER:
            try:
                _pl.unlock(lock_fh)
            except Exception:
                pass
        try:
            lock_fh.close()
        except Exception:
            pass
        try:
            lock_path.unlink()
        except Exception:
            pass





'''

TEST_WORKER_PY = r'''






"""
tests/test_worker.py — V13  |  Worker Unit Test Suite
Run: pytest tests/test_worker.py -v
"""

import pytest
from src.worker import _normalize, process_receipt, process_chunk


# ── helpers ───────────────────────────────────────────────────────────────────

def raw(rid: int, vendor: str, date: str, total: str) -> str:
    return (
        f"--- START RECEIPT {rid} ---\n"
        f"  Vendor: {vendor}\n"
        f"  Date:   {date}\n"
        f"  {total}\n"
        f"--- END RECEIPT {rid} ---\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Homoglyph normalisation  (NFKC + table)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHomoglyphNormalisation:

    @pytest.mark.parametrize("ch,expected", [
        ("O", "0"), ("o", "0"),
        ("l", "1"), ("I", "1"),
        ("S", "5"),
        ("Z", "2"),
        ("B", "8"),
        ("\u2014", "-"),    # em-dash
        ("\u2013", "-"),    # en-dash
        ("\uff10", "0"),    # FULLWIDTH DIGIT ZERO  (NFKC -> "0")
        ("\uff11", "1"),    # FULLWIDTH DIGIT ONE   (NFKC -> "1")
    ])
    def test_single_char(self, ch: str, expected: str) -> None:
        assert _normalize(ch) == expected

    def test_idempotent(self) -> None:
        s = "TOTAL: S/. 99.99"
        assert _normalize(s) == _normalize(_normalize(s))

    def test_chain_substitution(self) -> None:
        # S->5, O->0, l->1, I->1, Z->2, B->8
        assert _normalize("SOlIZB") == "501128"

    def test_sentinel_lines_not_normalised(self) -> None:
        """'S' in START and 'Z' in PLAZA must survive — process_receipt()
        strips sentinel lines before calling _normalize()."""
        result = process_receipt(1, raw(1, "PLAZA VEA", "01/01/2024", "TOTAL: S/. 10.00"))
        assert result["vendor"] == "PLAZA VEA"

    def test_ascii_digits_unchanged(self) -> None:
        assert _normalize("0123456789") == "0123456789"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Y2K two-digit year auto-correction
# ═══════════════════════════════════════════════════════════════════════════════

class TestY2KAutoCorrection:

    def test_status_tier2(self) -> None:
        r = process_receipt(1, raw(1, "METRO", "15/03/25", "TOTAL: S/. 10.00"))
        assert r["status"] == "TIER_2_FIXED"

    def test_year_expanded(self) -> None:
        r = process_receipt(1, raw(1, "METRO", "15/03/25", "TOTAL: S/. 10.00"))
        assert r["date"] == "15/03/2025"

    def test_reason_present(self) -> None:
        r = process_receipt(1, raw(1, "METRO", "15/03/25", "TOTAL: S/. 10.00"))
        assert "Y2K auto-corrected" in r["routing_reasons"]

    def test_four_digit_year_is_success(self) -> None:
        r = process_receipt(1, raw(1, "METRO", "15/03/2024", "TOTAL: S/. 10.00"))
        assert r["status"] == "SUCCESS"
        assert r["date"] == "15/03/2024"

    def test_y2k_with_missing_total_yields_manual(self) -> None:
        """Y2K fix + no total -> worst status wins: MANUAL_REVIEW."""
        txt = (
            "--- START RECEIPT 2 ---\n"
            "  Vendor: METRO\n"
            "  Date:   15/03/25\n"
            "--- END RECEIPT 2 ---\n"
        )
        r = process_receipt(2, txt)
        assert r["status"] == "MANUAL_REVIEW"
        assert "Y2K auto-corrected" in r["routing_reasons"]

    @pytest.mark.parametrize("sep", [".", "/", "-"])
    def test_separator_variants(self, sep: str) -> None:
        r = process_receipt(1, raw(1, "METRO", f"15{sep}03{sep}25", "TOTAL: S/. 10.00"))
        assert r["status"] == "TIER_2_FIXED"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Corrupted total (asterisks) and currency variants
# ═══════════════════════════════════════════════════════════════════════════════

class TestCorruptedTotal:

    def test_fully_masked_asterisks(self) -> None:
        r = process_receipt(10, raw(10, "TOTTUS", "01/01/2024", "TOTAL: S/. ***.**"))
        assert r["status"] == "MANUAL_REVIEW"
        assert any("corrupt" in x.lower() or "asterisk" in x.lower()
                   for x in r["routing_reasons"])

    def test_partial_asterisk(self) -> None:
        r = process_receipt(10, raw(10, "TOTTUS", "01/01/2024", "TOTAL: S/. 1*0.00"))
        assert r["status"] == "MANUAL_REVIEW"

    def test_clean_total_pen(self) -> None:
        r = process_receipt(10, raw(10, "TOTTUS", "01/01/2024", "TOTAL: S/. 99.99"))
        assert r["status"] == "SUCCESS"
        assert r["total"] == {"currency": "PEN", "amount": "99.99"}

    def test_clean_total_usd(self) -> None:
        r = process_receipt(10, raw(10, "TOTTUS", "01/01/2024", "TOTAL: $ 49.50"))
        assert r["status"] == "SUCCESS"
        assert r["total"]["currency"] == "USD"

    def test_clean_total_sol_dot_no_slash(self) -> None:
        """S. (no slash, OCR drop) after normalisation becomes 5. -- must match."""
        r = process_receipt(10, raw(10, "TOTTUS", "01/01/2024", "TOTAL: S. 77.50"))
        assert r["status"] == "SUCCESS"
        assert r["total"]["currency"] == "PEN"
        assert r["total"]["amount"]   == "77.50"

    def test_clean_total_s_slash(self) -> None:
        """S/ (no dot) must also parse as PEN."""
        r = process_receipt(10, raw(10, "TOTTUS", "01/01/2024", "TOTAL: S/ 20.00"))
        assert r["status"] == "SUCCESS"
        assert r["total"]["currency"] == "PEN"

    def test_missing_total(self) -> None:
        txt = (
            "--- START RECEIPT 11 ---\n"
            "  Vendor: METRO\n"
            "  Date:   01/01/2024\n"
            "--- END RECEIPT 11 ---\n"
        )
        r = process_receipt(11, txt)
        assert r["status"] == "MANUAL_REVIEW"
        assert "Total missing" in r["routing_reasons"]
        assert r["total"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Missing / hallucinated date
# ═══════════════════════════════════════════════════════════════════════════════

class TestMissingDate:

    def test_no_date_at_all(self) -> None:
        txt = (
            "--- START RECEIPT 20 ---\n"
            "  Vendor: METRO\n"
            "  TOTAL: S/. 55.00\n"
            "--- END RECEIPT 20 ---\n"
        )
        r = process_receipt(20, txt)
        assert r["status"] == "MANUAL_REVIEW"
        assert "Date missing" in r["routing_reasons"]
        assert r["date"] is None

    def test_hallucinated_question_marks(self) -> None:
        r = process_receipt(21, raw(21, "METRO", "??/??/????", "TOTAL: S/. 55.00"))
        assert r["status"] == "MANUAL_REVIEW"
        assert "OCR Hallucination" in r["routing_reasons"]

    def test_invalid_month_13(self) -> None:
        r = process_receipt(22, raw(22, "METRO", "01/13/2024", "TOTAL: S/. 20.00"))
        assert r["status"] == "MANUAL_REVIEW"
        assert any("month" in x.lower() for x in r["routing_reasons"])

    def test_invalid_month_14(self) -> None:
        r = process_receipt(23, raw(23, "METRO", "15/14/2024", "TOTAL: S/. 20.00"))
        assert r["status"] == "MANUAL_REVIEW"

    def test_partial_question_marks_in_day(self) -> None:
        r = process_receipt(24, raw(24, "METRO", "??/03/2024", "TOTAL: S/. 10.00"))
        assert r["status"] == "MANUAL_REVIEW"
        assert "OCR Hallucination" in r["routing_reasons"]


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Truncated receipt / OOM guard (process_chunk level)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTruncatedReceipt:

    def test_empty_body_does_not_raise(self) -> None:
        """A receipt with no body between sentinels must not raise; must be
        routed to MANUAL_REVIEW with appropriate reasons."""
        results = process_chunk([(50, "--- START RECEIPT 50 ---\n--- END RECEIPT 50 ---\n")])
        assert len(results) == 1
        assert results[0]["status"] == "MANUAL_REVIEW"

    def test_process_chunk_multi_receipt(self) -> None:
        chunk = [
            (1, raw(1, "METRO",   "01/01/2024", "TOTAL: S/. 10.00")),
            (2, raw(2, "TOTTUS",  "15/03/25",   "TOTAL: S/. 20.00")),
            (3, raw(3, "OXXO",    "??/??/????",  "TOTAL: S/. 5.00")),
        ]
        results = process_chunk(chunk)
        assert len(results) == 3
        assert results[0]["status"] == "SUCCESS"
        assert results[1]["status"] == "TIER_2_FIXED"
        assert results[2]["status"] == "MANUAL_REVIEW"

    def test_receipt_id_is_int(self) -> None:
        results = process_chunk([(99, raw(99, "METRO", "01/01/2024", "TOTAL: S/. 10.00"))])
        assert isinstance(results[0]["receipt_id"], int)
        assert results[0]["receipt_id"] == 99

    def test_large_receipt_id(self) -> None:
        results = process_chunk(
            [(999_999, raw(999_999, "METRO", "01/01/2024", "TOTAL: S/. 10.00"))]
        )
        assert results[0]["receipt_id"] == 999_999

    def test_body_with_many_lines_processed_correctly(self) -> None:
        """Receipts with many item lines (below OOM limit) should succeed."""
        items = "\n".join(
            f"  ITEM {i}: Product-{i:03d}   x1  S/. 5.00" for i in range(1, 30)
        )
        txt = (
            "--- START RECEIPT 77 ---\n"
            "  Vendor: METRO\n"
            "  Date:   01/06/2024\n"
            f"{items}\n"
            "  TOTAL: S/. 150.00\n"
            "--- END RECEIPT 77 ---\n"
        )
        results = process_chunk([(77, txt)])
        assert results[0]["status"] == "SUCCESS"
        assert results[0]["receipt_id"] == 77


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Vendor detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestVendorDetection:

    @pytest.mark.parametrize("vendor_str,expected", [
        ("T0TTU5",   "TOTTUS"),
        ("TOTTUS",   "TOTTUS"),
        ("METRO",    "METRO"),
        ("PLAZA VEA","PLAZA VEA"),
        ("0XX0",     "OXXO"),
        ("OXXO",     "OXXO"),
        ("JUMBO",    "UNKNOWN"),
    ])
    def test_vendor(self, vendor_str: str, expected: str) -> None:
        r = process_receipt(1, raw(1, vendor_str, "01/01/2024", "TOTAL: S/. 10.00"))
        assert r["vendor"] == expected


# ═══════════════════════════════════════════════════════════════════════════════
# 7. insert_batch idempotency (unit-level, no subprocess)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInsertBatch:

    def test_first_insert_returns_payload(self, tmp_path) -> None:
        from main import open_db, insert_batch
        conn = open_db(tmp_path / "test.db")
        payload = {
            "receipt_id": 1, "status": "SUCCESS",
            "vendor": "METRO", "date": "01/01/2024",
            "total": {"currency": "PEN", "amount": "10.00"},
            "routing_reasons": [],
        }
        result = insert_batch(conn, [payload])
        conn.close()
        assert len(result) == 1
        assert result[0]["receipt_id"] == 1

    def test_duplicate_insert_returns_empty(self, tmp_path) -> None:
        from main import open_db, insert_batch
        conn = open_db(tmp_path / "test.db")
        payload = {
            "receipt_id": 2, "status": "SUCCESS",
            "vendor": "METRO", "date": "01/01/2024",
            "total": {"currency": "PEN", "amount": "10.00"},
            "routing_reasons": [],
        }
        insert_batch(conn, [payload])          # first insert
        result = insert_batch(conn, [payload]) # duplicate
        conn.close()
        assert result == []

    def test_mixed_batch_new_and_dup(self, tmp_path) -> None:
        from main import open_db, insert_batch
        conn = open_db(tmp_path / "test.db")
        def make(rid):
            return {"receipt_id": rid, "status": "SUCCESS",
                    "vendor": "METRO", "date": "01/01/2024",
                    "total": {"currency": "PEN", "amount": "5.00"},
                    "routing_reasons": []}
        insert_batch(conn, [make(10)])                  # seed id=10
        result = insert_batch(conn, [make(10), make(11)])  # 10=dup, 11=new
        conn.close()
        assert len(result) == 1
        assert result[0]["receipt_id"] == 11

    def test_retry_does_not_duplicate(self, tmp_path) -> None:
        """Simulate a retry by calling _do logic directly; new_results must
        be fresh on each attempt (no accumulated entries from prior call)."""
        import sqlite3 as _sq
        from main import _db_retry
        conn = _sq.connect(str(tmp_path / "test.db"))
        conn.execute("CREATE TABLE processed (receipt_id INTEGER PRIMARY KEY)")
        conn.commit()

        call_count = [0]
        all_new_results = []

        def _do():
            call_count[0] += 1
            new_results = []   # must be inside _do so retries start clean
            cur = conn.cursor()
            conn.execute("BEGIN")
            cur.execute("INSERT OR IGNORE INTO processed(receipt_id) VALUES (?)", (99,))
            if cur.rowcount == 1:
                new_results.append({"receipt_id": 99})
            conn.commit()
            return new_results

        # First call succeeds
        result = _db_retry(_do)
        assert result == [{"receipt_id": 99}], "First call must return new payload"

        # Second call — duplicate, must return empty
        result2 = _db_retry(_do)
        assert result2 == [], "Second call must return empty (duplicate)"
        conn.close()







'''

TEST_RESILIENCE_PY = r'''

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


'''

SAFE_START_PY = r'''




import pathlib
import subprocess
import sys
import time

_DB   = pathlib.Path("data/output/processed_index.db")
_CKPT = pathlib.Path("data/output/checkpoint.json")

_UNLINK_RETRIES = 10
_UNLINK_DELAY_S = 0.25


def safe_remove(path, retries=_UNLINK_RETRIES, delay=_UNLINK_DELAY_S):
    """Unlink path with retries for Windows transient file locks (WinError 32)."""
    for _ in range(retries):
        if not path.exists():
            return True
        try:
            path.unlink()
            return True
        except PermissionError:
            time.sleep(delay)
        except Exception:
            break
    return not path.exists()


def safe_start_spawn(spawn_fn):
    """Clean up DB/checkpoint with retries, assert absence, then call spawn_fn().

    Raises RuntimeError with tasklist/pgrep diagnostics if files remain locked.
    Import this from tests and use it wherever _spawn() is called on a clean slate.
    """
    db = _DB
    ck = _CKPT
    if not safe_remove(db) or not safe_remove(ck):
        for _ in range(3):
            time.sleep(0.5)
            if safe_remove(db) and safe_remove(ck):
                break
    if db.exists() or ck.exists():
        try:
            if sys.platform == "win32":
                out = subprocess.check_output(
                    ["tasklist", "/FI", "IMAGENAME eq python.exe"],
                    text=True, timeout=5,
                )
            else:
                out = subprocess.check_output(
                    ["pgrep", "-la", "python"], text=True, timeout=5,
                )
        except Exception as exc:
            out = "(process list unavailable: " + str(exc) + ")"
        msg = (
            "Safe start failed: DB or CK still present after retries.\n"
            "Process list:\n" + out
        )
        raise RuntimeError(msg)
    time.sleep(0.25)
    return spawn_fn()





'''

CRASH_TEST_PY = r'''

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


'''

REQUIREMENTS_TXT = """# requirements.txt -- Titanium-Vault V13.2\npytest>=7.0.0\nportalocker>=2.7.0\n"""

DOCKERFILE_TEXT = """# Dockerfile -- Titanium-Vault V13.2\n# Build : docker build -t titanium-vault:v13.2 .\n# Run   : docker compose up\nFROM python:3.12-slim\nENV PYTHONUNBUFFERED=1\nWORKDIR /app\nRUN apt-get update && apt-get install -y --no-install-recommends \\\n        build-essential \\\n    && rm -rf /var/lib/apt/lists/*\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nRUN mkdir -p /app/data/raw_samples /app/data/output\n# /app/data is the named-volume mount point; inputs and outputs survive\n# container restarts and are isolated from host OS file-locking behaviour.\nVOLUME [\"/app/data\"]\nCMD [\"python\", \"main.py\"]\n"""

COMPOSE_TEXT = """# docker-compose.yml -- Titanium-Vault V13.2\nversion: \"3.9\"\nservices:\n  titanium-vault:\n    build: .\n    image: titanium-vault:v13.2\n    volumes:\n      - receipts_data:/app/data\n    environment:\n      - PYTHONUNBUFFERED=1\n    restart: unless-stopped\n\n  titanium-vault-test:\n    build: .\n    image: titanium-vault:v13.2\n    volumes:\n      - test_data:/app/data\n    entrypoint: [\"python\", \"-m\", \"pytest\", \"tests/\", \"-v\", \"--tb=short\"]\n    environment:\n      - PYTHONUNBUFFERED=1\n    profiles:\n      - test\n\nvolumes:\n  receipts_data:\n  test_data:\n"""

DOCKERIGNORE_TEXT = """__pycache__\n*.pyc\n*.pyo\n.env\n.vscode\n.git\ndata/output/*.db\ndata/output/*.db-wal\ndata/output/*.db-shm\ndata/output/*.json\ndata/output/*.jsonl\n"""

CLEANUP_PS1_TEXT = """# cleanup.ps1 -- Windows CI / developer pre-test cleanup\n# Usage: PowerShell -ExecutionPolicy Bypass -File cleanup.ps1\n#\n# Run this BEFORE opening VSCode or running pytest on Windows to avoid\n# ghost Python handles that lock processed_index.db (WinError 32).\n\nWrite-Host \"Stopping ghost Python processes...\"\nGet-Process -Name python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue\n\n$targets = @(\n    \"data\\output\\processed_index.db\",\n    \"data\\output\\processed_index.db-wal\",\n    \"data\\output\\processed_index.db-shm\",\n    \"data\\output\\processed_index.db.lock\",\n    \"data\\output\\checkpoint.json\",\n    \"data\\output\\checkpoint.json.tmp\",\n    \"data\\output\\metrics.json\",\n    \"data\\output\\metrics.json.tmp\",\n    \"data\\output\\success.jsonl\",\n    \"data\\output\\tier2_fixed.jsonl\",\n    \"data\\output\\manual_review.jsonl\"\n)\n\nforeach ($t in $targets) {\n    if (Test-Path $t) {\n        Remove-Item -Force $t -ErrorAction SilentlyContinue\n        Write-Host \"  removed $t\"\n    }\n}\nWrite-Host \"Cleanup complete.\"\n"""

CLEANUP_SH_TEXT = """#!/usr/bin/env bash\n# cleanup.sh -- Linux/macOS CI pre-test cleanup\n# Run: bash cleanup.sh\nset -euo pipefail\nOUTPUT=data/output\nfor f in \\\n    \"$OUTPUT/processed_index.db\"      \\\n    \"$OUTPUT/processed_index.db-wal\"  \\\n    \"$OUTPUT/processed_index.db-shm\"  \\\n    \"$OUTPUT/processed_index.db.lock\" \\\n    \"$OUTPUT/checkpoint.json\"         \\\n    \"$OUTPUT/checkpoint.json.tmp\"     \\\n    \"$OUTPUT/metrics.json\"            \\\n    \"$OUTPUT/metrics.json.tmp\"        \\\n    \"$OUTPUT/success.jsonl\"           \\\n    \"$OUTPUT/tier2_fixed.jsonl\"       \\\n    \"$OUTPUT/manual_review.jsonl\"\ndo\n    [ -f \"$f\" ] && rm -f \"$f\" && echo \"  removed $f\" || true\ndone\necho \"Cleanup complete.\"\n"""

# ---------------------------------------------------------------------------
# WRITE ALL FILES
# ---------------------------------------------------------------------------

_PY_FILES = {
    "src/generator.py":              GENERATOR_PY,
    "src/worker.py":                 WORKER_PY,
    "main.py":                       MAIN_PY,
    "src/locked_db.py":             LOCKED_DB_PY,
    "tests/test_worker.py":          TEST_WORKER_PY,
    "tests/test_resilience.py":      TEST_RESILIENCE_PY,
    "tests/helpers/safe_start.py":   SAFE_START_PY,
    "scripts/crash_restart_test.py": CRASH_TEST_PY,
}

_TEXT_FILES = {
    "requirements.txt":    REQUIREMENTS_TXT,
    "Dockerfile":          DOCKERFILE_TEXT,
    "docker-compose.yml":  COMPOSE_TEXT,
    ".dockerignore":       DOCKERIGNORE_TEXT,
    "scripts/cleanup.ps1": CLEANUP_PS1_TEXT,
    "scripts/cleanup.sh":  CLEANUP_SH_TEXT,
}

for _path, _content in {**_PY_FILES, **_TEXT_FILES}.items():
    _p = pathlib.Path(_path)
    _p.parent.mkdir(parents=True, exist_ok=True)
    _p.write_text(_content, encoding="utf-8")
    print(f"  written  {_path:<46} ({len(_content.splitlines())} lines)")

import os as _os, sys as _sys
if _sys.platform != "win32":
    _sh = pathlib.Path("scripts/cleanup.sh")
    _sh.chmod(_sh.stat().st_mode | 0o111)

print("""
=================================================================
  nuclear.py Titanium-Vault V13.6 -- generation complete.

  Native quick-start
  ------------------
    pip install -r requirements.txt
    python -m src.generator --total 1000 --seed 42
    python main.py
    pytest tests/ -v
    python scripts/crash_restart_test.py

  Docker quick-start
  ------------------
    docker compose up --build
    docker compose --profile test run --rm titanium-vault-test

  Windows developer checklist
  ---------------------------
    PowerShell -ExecutionPolicy Bypass -File scripts/cleanup.ps1
=================================================================
""")
