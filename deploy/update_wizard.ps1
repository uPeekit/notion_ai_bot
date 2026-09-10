# Interactive update wizard for a production install. Double-click update.cmd in the install root.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

function Ask([string]$prompt, [string]$default) {
  $v = Read-Host "$prompt [$default]"
  if ([string]::IsNullOrWhiteSpace($v)) { return $default }
  return $v.Trim().Trim('"')
}

function AskYN([string]$prompt, [bool]$default) {
  $d = if ($default) { "Y/n" } else { "y/N" }
  $v = Read-Host "$prompt [$d]"
  if ([string]::IsNullOrWhiteSpace($v)) { return $default }
  return $v.Trim().ToLower().StartsWith("y")
}

function Find-NewestZip {
  $dirs = @()
  $wiz = Join-Path $env:USERPROFILE ".notion_ai_bot\wizard.json"
  if (Test-Path $wiz) {
    $s = Get-Content $wiz -Raw | ConvertFrom-Json
    if ($s.dist_dir) { $dirs += $s.dist_dir }
  }
  $dirs += (Join-Path $env:USERPROFILE "Downloads")
  $dirs += $root
  $found = foreach ($d in $dirs) { if (Test-Path $d) { Get-ChildItem -Path $d -Filter "notion_ai_bot-*.zip" -File -ErrorAction SilentlyContinue } }
  $found | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

Write-Host ""
Write-Host "=== notion_ai_bot update ==="
$version = if (Test-Path "VERSION") { (Get-Content "VERSION" -Raw).Trim() } else { "(unknown)" }
Write-Host "installed version : $version"
Write-Host "install dir       : $root"
Write-Host "schema            : " -NoNewline
& $py -m tools.migrate --status --db "data\bot.sqlite" 2>&1 | Select-Object -First 1 | ForEach-Object { Write-Host $_ }
$pid_file = Join-Path $root "data\bot.pid"
if (Test-Path $pid_file) { Write-Host "note: data\bot.pid exists - stop the bot before updating" -ForegroundColor Yellow }
Write-Host ""
Write-Host "  1) Update this install"
Write-Host "  2) Dry run (show what an update would do)"
Write-Host "  3) Roll back to the previous version"
Write-Host "  4) Quit"
$choice = Ask "Choose" "1"

switch ($choice) {
  { $_ -in "1", "2" } {
    $newest = Find-NewestZip
    $default = if ($newest) { $newest.FullName } else { "" }
    $zip = Ask "Release zip" $default
    if (-not (Test-Path $zip)) { Write-Host "file not found: $zip" -ForegroundColor Red; exit 1 }
    Write-Host ""
    Write-Host "> apply_update.py $zip --root $root --dry-run" -ForegroundColor Cyan
    & $py (Join-Path $root "apply_update.py") $zip --root $root --dry-run
    if ($LASTEXITCODE -ne 0) { Write-Host "refused (exit $LASTEXITCODE)" -ForegroundColor Red; exit $LASTEXITCODE }
    if ($choice -eq "2") { exit 0 }
    if (-not (AskYN "Apply this update" $true)) { exit 0 }
    Write-Host "> deploy\update.ps1 -Zip $zip" -ForegroundColor Cyan
    & (Join-Path $root "deploy\update.ps1") -Zip $zip
    $code = $LASTEXITCODE
    if ($code -eq 0) { Write-Host "updated. Start the bot with deploy\run.ps1" -ForegroundColor Green }
    elseif ($code -eq 2) { Write-Host "update FAILED after files were copied. Run this wizard again and choose 3 (roll back)." -ForegroundColor Red }
    else { Write-Host "update refused (exit $code)" -ForegroundColor Red }
    exit $code
  }
  "3" {
    $backups = Get-ChildItem -Path (Join-Path $root ".backup") -Directory -ErrorAction SilentlyContinue | Sort-Object Name
    if (-not $backups) { Write-Host "no backups found"; exit 1 }
    Write-Host "available backups: $(($backups | ForEach-Object { $_.Name }) -join ', ')"
    if (-not (AskYN "Roll back app files to the newest backup (database is NOT restored; see data\*.pre-*.sqlite)" $false)) { exit 0 }
    Write-Host "> apply_update.py --rollback --root $root" -ForegroundColor Cyan
    & $py (Join-Path $root "apply_update.py") --rollback --root $root
    exit $LASTEXITCODE
  }
  default { exit 0 }
}
