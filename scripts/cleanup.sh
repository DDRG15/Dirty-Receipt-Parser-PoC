#!/usr/bin/env bash
# cleanup.sh -- Linux/macOS CI pre-test cleanup
# Run: bash cleanup.sh
set -euo pipefail
OUTPUT=data/output
for f in \
    "$OUTPUT/processed_index.db"      \
    "$OUTPUT/processed_index.db-wal"  \
    "$OUTPUT/processed_index.db-shm"  \
    "$OUTPUT/processed_index.db.lock" \
    "$OUTPUT/checkpoint.json"         \
    "$OUTPUT/checkpoint.json.tmp"     \
    "$OUTPUT/metrics.json"            \
    "$OUTPUT/metrics.json.tmp"        \
    "$OUTPUT/success.jsonl"           \
    "$OUTPUT/tier2_fixed.jsonl"       \
    "$OUTPUT/manual_review.jsonl"
do
    [ -f "$f" ] && rm -f "$f" && echo "  removed $f" || true
done
echo "Cleanup complete."
