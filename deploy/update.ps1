param([Parameter(Mandatory)][string]$Zip, [switch]$Force, [switch]$DryRun)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$updateArgs = @((Resolve-Path $Zip).Path, "--root", $root)
if ($Force) { $updateArgs += "--force" }
if ($DryRun) { $updateArgs += "--dry-run" }
& $py (Join-Path $root "apply_update.py") @updateArgs
exit $LASTEXITCODE
