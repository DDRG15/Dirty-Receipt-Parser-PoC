






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







