<#
.SYNOPSIS
  Store one Reel Engine secret in Windows Credential Manager, typed hidden.

.DESCRIPTION
  Asks for the value with hidden input, then hands it to `reel secret set <Name> --stdin`
  on STDIN only. The value never appears on a command line, in a file, a log or on screen.
  After storing, it reads back a SHA-256 fingerprint of what keyring holds (in memory only)
  and prints "stored" only if it matches. Otherwise it prints the error.

.PARAMETER Name
  ig_app_secret | telegram_bot_token
  (The Instagram token is NOT stored here: use scripts\connect-instagram.ps1, which
  verifies it with Instagram and records its expiry.)

.PARAMETER TestService
  Test mode: stores into keyring service "ReelEngine-Test" through a stand-in Python
  one-liner, never into the real "ReelEngine" service.

.PARAMETER SecureValue
  For automated tests only: a SecureString to store instead of prompting.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set-secret.ps1 -Name ig_app_secret
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('ig_app_secret', 'telegram_bot_token')]
    [string]$Name,

    [switch]$TestService,

    [securestring]$SecureValue
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$Reel   = Join-Path $Root '.venv\Scripts\reel.exe'

if ($TestService) {
    $Service  = 'ReelEngine-Test'
    $StoreExe = $VenvPy
    $StoreArgs = '-c "import sys,keyring; keyring.set_password(''ReelEngine-Test'', sys.argv[1], sys.stdin.read().strip())" ' + $Name
} else {
    $Service  = if ($env:REELENGINE_KEYRING_SERVICE) { $env:REELENGINE_KEYRING_SERVICE } else { 'ReelEngine' }
    $StoreExe = $Reel
    $StoreArgs = "secret set $Name --stdin"
}
$VerifyArgs = '-c "import sys,hashlib,keyring; v=keyring.get_password(sys.argv[1], sys.argv[2]) or chr(0); print(hashlib.sha256(v.encode(''utf-8'')).hexdigest())" ' +
              "`"$Service`" $Name"

# Runs a program with bytes on STDIN (never on the command line). Returns code/stdout/stderr.
function Invoke-WithStdin {
    param([string]$Exe, [string]$Arguments, [byte[]]$InputBytes)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = $Arguments
    $psi.WorkingDirectory = $Root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $psi.EnvironmentVariables['REELENGINE_KEYRING_SERVICE'] = $Service
    $p = [System.Diagnostics.Process]::Start($psi)
    try {
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if ($InputBytes -and $InputBytes.Length -gt 0) {
            $p.StandardInput.BaseStream.Write($InputBytes, 0, $InputBytes.Length)
            $p.StandardInput.BaseStream.Flush()
        }
        $p.StandardInput.Close()
        if (-not $p.WaitForExit(60000)) { $p.Kill(); throw 'timed out after 60 seconds' }
        $p.WaitForExit()
        return @{ Code = $p.ExitCode; Out = $outTask.Result; Err = $errTask.Result }
    } finally {
        $p.Dispose()
    }
}

# Last meaningful line of an error, with anything token-shaped masked (belt and braces).
function Format-Error([string]$Text) {
    $lines = @($Text -split "`r?`n" | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) { return 'unknown error (no message)' }
    return ($lines[-1].Trim() -replace '\b(?=[A-Za-z0-9_\-\.]*\d)(?=[A-Za-z0-9_\-\.]*[A-Za-z])[A-Za-z0-9_\-\.]{24,}\b', '[REDACTED]')
}

if (-not (Test-Path -LiteralPath $StoreExe)) {
    Write-Host "error: $StoreExe was not found. Run scripts\setup.ps1 first."
    exit 1
}

$secure = $SecureValue
if (-not $secure) {
    $secure = Read-Host -AsSecureString -Prompt "Paste $Name (typing is hidden), then press Enter"
}

$bstr  = [IntPtr]::Zero
$chars = $null
$bytes = $null
$rc = 1
try {
    if ($secure.Length -eq 0) { throw 'nothing was entered; nothing stored' }
    # SecureString -> char[] -> UTF-8 byte[] (no immutable .NET string is created for the value).
    $bstr  = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $len   = $secure.Length
    $chars = New-Object char[] $len
    [Runtime.InteropServices.Marshal]::Copy($bstr, $chars, 0, $len)
    $start = 0; $end = $len - 1
    while ($start -le $end -and [char]::IsWhiteSpace($chars[$start])) { $start++ }
    while ($end -ge $start -and [char]::IsWhiteSpace($chars[$end])) { $end-- }
    if ($end -lt $start) { throw 'only spaces were entered; nothing stored' }
    $bytes = [Text.Encoding]::UTF8.GetBytes($chars, $start, $end - $start + 1)

    $sha = [Security.Cryptography.SHA256]::Create()
    try { $want = -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) } finally { $sha.Dispose() }

    $r = Invoke-WithStdin -Exe $StoreExe -Arguments $StoreArgs -InputBytes $bytes
    if ($r.Code -ne 0) { throw (Format-Error ($r.Err + "`n" + $r.Out)) }

    $v = Invoke-WithStdin -Exe $VenvPy -Arguments $VerifyArgs -InputBytes $null
    if ($v.Code -ne 0) { throw ('could not read back from Credential Manager: ' + (Format-Error $v.Err)) }
    if ($v.Out.Trim() -ne $want) { throw "the value is not in Credential Manager (service $Service) after storing; nothing was confirmed" }

    Write-Host 'stored'
    $rc = 0
} catch {
    Write-Host ('error: ' + $_.Exception.Message)
    $rc = 1
} finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($chars) { [Array]::Clear($chars, 0, $chars.Length) }
    if ($bytes) { [Array]::Clear($bytes, 0, $bytes.Length) }
    if (-not $SecureValue -and $secure) { $secure.Dispose() }
    $chars = $null; $bytes = $null
}
exit $rc
