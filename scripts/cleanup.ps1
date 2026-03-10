# cleanup.ps1 -- Windows CI / developer pre-test cleanup
# Usage: PowerShell -ExecutionPolicy Bypass -File cleanup.ps1
#
# Run this BEFORE opening VSCode or running pytest on Windows to avoid
# ghost Python handles that lock processed_index.db (WinError 32).

Write-Host "Stopping ghost Python processes..."
Get-Process -Name python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

$targets = @(
    "data\output\processed_index.db",
    "data\output\processed_index.db-wal",
    "data\output\processed_index.db-shm",
    "data\output\processed_index.db.lock",
    "data\output\checkpoint.json",
    "data\output\checkpoint.json.tmp",
    "data\output\metrics.json",
    "data\output\metrics.json.tmp",
    "data\output\success.jsonl",
    "data\output\tier2_fixed.jsonl",
    "data\output\manual_review.jsonl"
)

foreach ($t in $targets) {
    if (Test-Path $t) {
        Remove-Item -Force $t -ErrorAction SilentlyContinue
        Write-Host "  removed $t"
    }
}
Write-Host "Cleanup complete."
