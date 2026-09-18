# Starts the bot in this window. Double-click start.cmd in the install root.
# Stop it with Ctrl+C (clean) or by closing the window. Output also goes to the log file.
# Exit codes come from app/main.py; tests/test_deploy_scripts.py keeps the two in step.

# The bot logs to stderr. Under "Stop", PowerShell 5.1 would treat the first stderr line from a
# native command as a terminating error and kill this script mid-run.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$envFile = Join-Path $root ".env"

function Open-Env {
  $v = Read-Host "Open .env in Notepad now? [Y/n]"
  if ([string]::IsNullOrWhiteSpace($v) -or $v.Trim().ToLower().StartsWith("y")) {
    Start-Process notepad.exe $envFile
  }
}

function Read-EnvValue([string]$key, [string]$default) {
  $m = Select-String -Path $envFile -Pattern "^\s*$key\s*=\s*(.*?)\s*$" | Select-Object -First 1
  if ($m) { return $m.Matches[0].Groups[1].Value.Trim('"') }
  return $default
}

if (-not (Test-Path $py)) {
  Write-Host "This folder is not an installed copy of the bot (there is no .venv here)." -ForegroundColor Red
  Write-Host "Install it with release.cmd in the source repo - it offers a fresh install."
  exit 1
}
if (-not (Test-Path $envFile)) {
  Copy-Item (Join-Path $root ".env.example") $envFile
  Write-Host "Created .env - fill in TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS and NOTION_TOKEN." -ForegroundColor Yellow
  Open-Env
  exit 2
}

$version = if (Test-Path "VERSION") { (Get-Content "VERSION" -Raw).Trim() } else { "(dev)" }
$port = Read-EnvValue "ADMIN_UI_PORT" "8787"
$logFile = Read-EnvValue "LOG_FILE" "logs/bot.log"

Write-Host ""
Write-Host "=== notion_ai_bot $version ===" -ForegroundColor Cyan
if ($logFile) { Write-Host "log file   : $(Join-Path $root $logFile)" } else { Write-Host "log file   : (disabled)" }
if ($port -ne "0") { Write-Host "admin page : http://127.0.0.1:$port" }
Write-Host "stop       : Ctrl+C (then Y), or close this window"
Write-Host ""

& $py -m app.main
$code = $LASTEXITCODE

Write-Host ""
switch ($code) {
  0 { Write-Host "Bot stopped." }
  2 {
    Write-Host "Configuration problem - the message above says what to fix in .env." -ForegroundColor Red
    Open-Env
  }
  3 {
    Write-Host "Notion rejected NOTION_TOKEN. Check it in .env (see documentation\NOTION_SETUP.md)." -ForegroundColor Red
    Open-Env
  }
  4 {
    # Never applied behind the user's back (documentation/ARCHITECTURE.md section 15): only on an
    # explicit yes, through the same tools.migrate the installer uses, which backs up the db first.
    Write-Host "The database needs a migration before the bot can start." -ForegroundColor Red
    $v = Read-Host "Apply it now (a backup of the database is taken first)? [Y/n]"
    if ([string]::IsNullOrWhiteSpace($v) -or $v.Trim().ToLower().StartsWith("y")) {
      $db = Read-EnvValue "DB_PATH" "data/bot.sqlite"
      & $py -m tools.migrate --apply --db $db
      if ($LASTEXITCODE -eq 0) { Write-Host "Migrated. Start the bot again with start.cmd." -ForegroundColor Green }
      else { Write-Host "Migration failed (exit $LASTEXITCODE) - see the message above." -ForegroundColor Red }
    }
  }
  5 { Write-Host "The bot is already running - look for its other window." -ForegroundColor Yellow }
  # A crash before logging starts (e.g. at import) never reaches the log file - the traceback
  # printed above is the only record of it.
  default { Write-Host "The bot stopped unexpectedly (exit $code). The error is printed above; later failures are also in the log file." -ForegroundColor Red }
}
exit $code
