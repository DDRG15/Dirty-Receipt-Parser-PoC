






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







