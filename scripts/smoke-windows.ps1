# Runs physical-input tests against controlled windows. Moves the real cursor; do not touch mouse/keyboard while it runs.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python.exe" }
& $py scripts/smoke_windows.py
exit $LASTEXITCODE
