param([Parameter(Mandatory)][string]$Zip, [switch]$Force, [switch]$DryRun)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$args = @((Resolve-Path $Zip).Path, "--root", $root)
if ($Force) { $args += "--force" }
if ($DryRun) { $args += "--dry-run" }
& $py (Join-Path $root "apply_update.py") @args
exit $LASTEXITCODE
