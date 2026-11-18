# Installs the nextgen-api skill for the current user (available in every project).
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
# Re-run it to update: it replaces the installed copy.

$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "nextgen-api"
$dst = Join-Path $env:USERPROFILE ".claude\skills\nextgen-api"

if (-not (Test-Path (Join-Path $src "SKILL.md"))) { throw "SKILL.md not found in $src" }
New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Copy-Item -Recurse $src $dst

Write-Host "Installed in $dst"
python (Join-Path $dst "scripts\ngcat.py") stats
Write-Host ""
Write-Host "Restart Claude Code and ask, e.g.: 'which endpoint gives me encounter insurances?'"
Write-Host "Optional PHI guard hook: see README.md, section 'Optional: PHI guard hook'."
