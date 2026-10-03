<#
.SYNOPSIS
  Connect Reel Engine to your Instagram account (thin wrapper around `reel connect`).

.DESCRIPTION
  `reel connect` asks for your long-lived Instagram access token with hidden input,
  checks it against Instagram (/me), and stores it in Windows Credential Manager.
  The token is never shown, logged or written to a file.
  Where to get the token and what to do next: docs\SETUP.md.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\connect-instagram.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root  = Split-Path -Parent $PSScriptRoot
$Reel  = Join-Path $Root '.venv\Scripts\reel.exe'
$Setup = Join-Path $Root 'docs\SETUP.md'

if (-not (Test-Path -LiteralPath $Reel)) {
    Write-Host 'Reel Engine is not set up yet. Run this first:' -ForegroundColor Yellow
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\setup.ps1`""
    exit 1
}

Write-Host 'Connect Instagram' -ForegroundColor Cyan
Write-Host 'You will be asked to paste your long-lived Instagram access token (typing is hidden).'
Write-Host "Where to get it: $Setup"
Write-Host ''

$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    Push-Location $Root
    & $Reel connect
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    $ErrorActionPreference = $prev
}

Write-Host ''
if ($code -eq 0) {
    Write-Host 'Connected.' -ForegroundColor Green
    Write-Host "Next steps: $Setup"
    Write-Host "Check everything: `"$Reel`" doctor"
} else {
    Write-Host "reel connect did not finish (exit code $code). Nothing else was changed." -ForegroundColor Yellow
    Write-Host "Help: $Setup"
}
exit $code
