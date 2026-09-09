param([Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$Dest)
$ErrorActionPreference = "Stop"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found; install: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" }
New-Item -ItemType Directory -Force $Dest | Out-Null
Expand-Archive -Path $Zip -DestinationPath $Dest -Force
Push-Location $Dest
try {
  & $uv sync --frozen --no-dev
  if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "created .env - fill in tokens before running" }
  New-Item -ItemType Directory -Force "data","logs" | Out-Null
  & ".venv\Scripts\python.exe" -m tools.migrate --apply --db "data\bot.sqlite"
  Write-Host "installed $(Get-Content VERSION) into $Dest. Start with deploy\run.ps1"
} finally { Pop-Location }
