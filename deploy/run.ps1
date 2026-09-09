$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& ".venv\Scripts\python.exe" -m app.main
exit $LASTEXITCODE
