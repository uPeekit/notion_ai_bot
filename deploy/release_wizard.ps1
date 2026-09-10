# Interactive release wizard. Double-click release.cmd in the repo root.
# Wraps release.py; remembers your answers in %USERPROFILE%\.notion_ai_bot\wizard.json
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Find-Uv {
  $c = Get-Command uv -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  $p = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
  if (Test-Path $p) { return $p }
  throw "uv not found. Install: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}

function Ask([string]$prompt, [string]$default) {
  $v = Read-Host "$prompt [$default]"
  if ([string]::IsNullOrWhiteSpace($v)) { return $default }
  return $v.Trim()
}

function AskYN([string]$prompt, [bool]$default) {
  $d = if ($default) { "Y/n" } else { "y/N" }
  $v = Read-Host "$prompt [$d]"
  if ([string]::IsNullOrWhiteSpace($v)) { return $default }
  return $v.Trim().ToLower().StartsWith("y")
}

function Load-State {
  $file = Join-Path $env:USERPROFILE ".notion_ai_bot\wizard.json"
  $state = @{ kind = "auto"; push = $true; update_prod = $true; prod_dir = "C:\apps\notion_ai_bot"; dist_dir = (Join-Path $root "dist") }
  if (Test-Path $file) {
    $saved = Get-Content $file -Raw | ConvertFrom-Json
    foreach ($k in @("kind", "push", "update_prod", "prod_dir")) {
      if ($null -ne $saved.$k) { $state[$k] = $saved.$k }
    }
  }
  return $state
}

function Save-State($state) {
  $dir = Join-Path $env:USERPROFILE ".notion_ai_bot"
  New-Item -ItemType Directory -Force $dir | Out-Null
  $state | ConvertTo-Json | Set-Content (Join-Path $dir "wizard.json") -Encoding ASCII
}

$uv = Find-Uv
$state = Load-State

Write-Host ""
Write-Host "=== notion_ai_bot release ==="
$pyproject = Get-Content (Join-Path $root "pyproject.toml") -Raw
$current = [regex]::Match($pyproject, '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
$lastTag = (git tag --list "v*" --sort=-v:refname | Select-Object -First 1)
if (-not $lastTag) { $lastTag = "(none)" }
Write-Host "current version : $current"
Write-Host "last tag        : $lastTag"
Write-Host "auto would do   : " -NoNewline
& $uv run python release.py --auto --dry-run 2>&1 | Select-Object -First 1 | ForEach-Object { Write-Host $_ }
$dirty = git status --porcelain
if ($dirty) {
  Write-Host ""
  Write-Host "WARNING: working tree has uncommitted changes. release.py refuses unless you commit first." -ForegroundColor Yellow
}
Write-Host ""

$kind = ""
while ($kind -notin @("auto", "patch", "full")) {
  $kind = (Ask "Release kind (auto = decide from changes, patch = 0.0.x, full = 0.x.0)" $state.kind).ToLower()
}
$dryRun = AskYN "Dry run only (show what would happen, change nothing)" $false
$skipTests = AskYN "Skip tests and lint" $false

$relArgs = @("run", "python", "release.py", "--$kind")
if ($dryRun) { $relArgs += "--dry-run" }
if ($skipTests) { $relArgs += "--skip-tests" }
Write-Host ""
Write-Host "> uv $($relArgs -join ' ')" -ForegroundColor Cyan
& $uv @relArgs
if ($LASTEXITCODE -ne 0) { Write-Host "release failed (exit $LASTEXITCODE)" -ForegroundColor Red; exit 1 }
$state.kind = $kind
Save-State $state
if ($dryRun) { exit 0 }

$last = Join-Path $root "dist\last_release.json"
if (-not (Test-Path $last)) { Write-Host "no dist\last_release.json found; nothing was built (no changes?)"; exit 0 }
$info = Get-Content $last -Raw | ConvertFrom-Json
$zip = Join-Path $root "dist\$($info.zip)"
Write-Host ""
Write-Host "built: $zip" -ForegroundColor Green

if (AskYN "Push commit and tag to origin" $state.push) {
  $state.push = $true
  git push
  git push --tags
} else { $state.push = $false }

if (AskYN "Update the production install now" $state.update_prod) {
  $state.update_prod = $true
  $prod = Ask "Production directory" $state.prod_dir
  $state.prod_dir = $prod
  Save-State $state
  if (Test-Path (Join-Path $prod "apply_update.py")) {
    Write-Host "> $prod\deploy\update.ps1 -Zip $zip" -ForegroundColor Cyan
    & (Join-Path $prod "deploy\update.ps1") -Zip $zip
    if ($LASTEXITCODE -ne 0) { Write-Host "update failed (exit $LASTEXITCODE). Roll back with: $prod\update.cmd" -ForegroundColor Red; exit 1 }
  } else {
    if (AskYN "No install found in $prod. Install a fresh copy there" $true) {
      Write-Host "> deploy\install.ps1 -Zip $zip -Dest $prod" -ForegroundColor Cyan
      & (Join-Path $root "deploy\install.ps1") -Zip $zip -Dest $prod
      if ($LASTEXITCODE -ne 0) { Write-Host "install failed (exit $LASTEXITCODE)" -ForegroundColor Red; exit 1 }
    }
  }
  Write-Host ""
  Write-Host "Start the bot with: $prod\deploy\run.ps1"
} else { $state.update_prod = $false }
Save-State $state
Write-Host ""
Write-Host "done." -ForegroundColor Green
exit 0
