






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







