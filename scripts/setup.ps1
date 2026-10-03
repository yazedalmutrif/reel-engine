<#
.SYNOPSIS
  First-time setup and repair for Reel Engine. Safe to run again at any time.

.DESCRIPTION
  1. Checks Python 3.10 (py -3.10).
  2. Creates .venv if it is missing.
  3. pip install -r requirements.txt, then pip install -e . (the reelengine package).
  4. Downloads and checksum-verifies ffmpeg, fonts and the Whisper model
     (scripts\bootstrap_assets.py; -SkipModel skips the ~3 GB model).
  5. Creates the data\ folders and the OneDrive inbox folder ([paths].inbox in config.toml) if missing.
  6. Runs `reel doctor`.

  It never installs system software, changes PATH or power settings, creates scheduled
  tasks, or touches secrets. It stops at the first real error.

.PARAMETER SkipModel
  Skip the Whisper model download/check.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -WhatIf
  Shows what would happen and changes nothing.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -SkipModel

.NOTES
  Exit codes: 0 = all good; 1 = a setup step failed; 2 = setup done but `reel doctor` reported a failure.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$SkipModel
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root     = Split-Path -Parent $PSScriptRoot
$VenvDir  = Join-Path $Root '.venv'
$VenvPy   = Join-Path $VenvDir 'Scripts\python.exe'
$Reel     = Join-Path $VenvDir 'Scripts\reel.exe'
$DryRun   = [bool]$WhatIfPreference
$DataSubfolders = @('raw', 'work', 'out', 'thumbs', 'playbooks', 'backups', 'dashboard', 'logs', 'vault_preview')

function Write-Step([int]$n, [string]$text) { Write-Host ''; Write-Host "[$n/6] $text" -ForegroundColor Cyan }
function Write-Ok([string]$text)   { Write-Host "  ok     $text" -ForegroundColor Green }
function Write-Plan([string]$text) { Write-Host "  would  $text" -ForegroundColor Yellow }
function Write-Note([string]$text) { Write-Host "  note   $text" -ForegroundColor DarkYellow }

# Runs a native program, shows its output, throws on a non-zero exit code.
# (EAP is relaxed during the call so harmless stderr lines, e.g. pip warnings, do not abort.)
function Invoke-Native {
    param([string]$What, [string]$Exe, [string[]]$ArgList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @ArgList | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw "$What failed (exit code $code)." }
}

# Returns captured stdout lines of a native program, or $null if it could not run.
function Get-NativeOutput {
    param([string]$Exe, [string[]]$ArgList = @())
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @ArgList 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ,@($out | ForEach-Object { "$_" })   # leading comma: keep a 1-line result as an array
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Get-PyVersion([string]$Exe, [string[]]$Pre = @()) {
    $out = Get-NativeOutput $Exe ($Pre + @('-c', 'import sys; print(sys.version.split()[0])'))
    if ($out) { return $out[-1].Trim() }
    return $null
}

# Engine paths. Uses reelengine.config when the venv can import it; otherwise reads config.toml directly.
function Get-EnginePaths {
    if (Test-Path -LiteralPath $VenvPy) {
        $code = 'from reelengine.config import load_config as L; c=L(); print(c.inbox); print(c.data_dir); ' +
                'print(c.resolve(c[''paths''][''ffmpeg''])); print(c.fonts_dir); print(c.resolve(c[''whisper''][''model_path'']))'
        $out = Get-NativeOutput $VenvPy @('-c', $code)
        if ($out -and $out.Count -ge 5) {
            return @{ Inbox = $out[0]; Data = $out[1]; Ffmpeg = $out[2]; Fonts = $out[3]; Model = $out[4]; Source = 'reelengine.config' }
        }
    }
    $cfgFile = Join-Path $Root 'config.toml'
    if (-not (Test-Path -LiteralPath $cfgFile)) { $cfgFile = Join-Path $Root 'config.example.toml' }  # fresh clone
    $toml = Get-Content -LiteralPath $cfgFile -Raw
    $section = [regex]::Match($toml, '(?ms)^\[paths\]\s*$(.*?)(?=^\[)')
    if (-not $section.Success) { throw 'config.toml has no [paths] section.' }
    $get = {
        param($key)
        $m = [regex]::Match($section.Groups[1].Value, '(?m)^\s*' + $key + '\s*=\s*"([^"]+)"')
        if (-not $m.Success) { throw "config.toml [paths] has no '$key'." }
        $p = [Environment]::ExpandEnvironmentVariables($m.Groups[1].Value) -replace '/', '\'
        if (-not [IO.Path]::IsPathRooted($p)) { $p = Join-Path $Root $p }
        $p
    }
    return @{ Inbox = (& $get 'inbox'); Data = (& $get 'data_dir'); Ffmpeg = (& $get 'ffmpeg'); Fonts = $null; Model = $null; Source = 'config.toml (venv not ready)' }
}

function Confirm-Folder {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Container) {
        Write-Ok "present  $Path"
    } elseif ($PSCmdlet.ShouldProcess($Path, 'Create folder')) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        Write-Ok "created  $Path"
    }
}

$exitCode = 0
Push-Location $Root
try {
    Write-Host "Reel Engine setup  ($Root)" -ForegroundColor White
    if ($DryRun) { Write-Host 'WhatIf mode: nothing will be changed.' -ForegroundColor Yellow }

    # 1. Python 3.10 ---------------------------------------------------------
    Write-Step 1 'Python 3.10'
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $launcherVer = $null
    if ($pyLauncher) { $launcherVer = Get-PyVersion $pyLauncher.Source @('-3.10') }
    if ($launcherVer -and $launcherVer.StartsWith('3.10.')) {
        Write-Ok "py -3.10 -> Python $launcherVer"
    } elseif (Test-Path -LiteralPath $VenvPy) {
        Write-Note 'py -3.10 was not found, but .venv already exists; continuing with it.'
    } else {
        throw 'Python 3.10 was not found (py -3.10). Install Python 3.10 (64-bit) from python.org yourself, then run this script again.'
    }

    # 2. Virtual environment -------------------------------------------------
    Write-Step 2 'Virtual environment (.venv)'
    if (Test-Path -LiteralPath $VenvPy) {
        $venvVer = Get-PyVersion $VenvPy
        if (-not ($venvVer -and $venvVer.StartsWith('3.10.'))) {
            throw ".venv uses Python '$venvVer', not 3.10. Rename the .venv folder yourself (for example to .venv-old), then run this script again."
        }
        Write-Ok ".venv present (Python $venvVer)"
    } elseif ($PSCmdlet.ShouldProcess($VenvDir, 'Create virtual environment (py -3.10 -m venv)')) {
        Invoke-Native 'Creating .venv' $pyLauncher.Source @('-3.10', '-m', 'venv', $VenvDir)
        Write-Ok ".venv created"
    }
    $haveVenv = Test-Path -LiteralPath $VenvPy

    # 3. Python packages -----------------------------------------------------
    Write-Step 3 'Python packages'
    $req = Join-Path $Root 'requirements.txt'
    if ($PSCmdlet.ShouldProcess($VenvDir, 'pip install -r requirements.txt')) {
        Invoke-Native 'pip install -r requirements.txt' $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', $req)
        Write-Ok 'requirements.txt satisfied'
    } elseif ($haveVenv) {
        $plan = Get-NativeOutput $VenvPy @('-m', 'pip', 'install', '--dry-run', '--disable-pip-version-check', '-r', $req)
        $would = @($plan | Where-Object { $_ -like 'Would install*' })
        if ($would.Count -gt 0) { $would | ForEach-Object { Write-Plan "pip: $_" } } else { Write-Ok 'requirements.txt: everything already installed' }
    }
    if ($PSCmdlet.ShouldProcess($VenvDir, 'pip install -e . (the reelengine package and the reel command)')) {
        Invoke-Native 'pip install -e .' $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-e', $Root)
        Write-Ok 'reelengine package installed (editable); reel.exe ready'
    }

    # 4. Assets ---------------------------------------------------------------
    Write-Step 4 'Assets: ffmpeg, fonts, Whisper model (checksums verified)'
    $bootArgs = @((Join-Path $Root 'scripts\bootstrap_assets.py'))
    if ($SkipModel) { $bootArgs += '--skip-model'; Write-Note 'Whisper model skipped (-SkipModel).' }
    if ($PSCmdlet.ShouldProcess('bin\ffmpeg, fonts, models', "python $($bootArgs -join ' ')")) {
        Invoke-Native 'bootstrap_assets.py' $VenvPy $bootArgs
    } else {
        $p = Get-EnginePaths
        foreach ($item in @(@('ffmpeg', $p.Ffmpeg), @('fonts', $p.Fonts), @('model', $p.Model))) {
            if (-not $item[1]) { continue }
            if (($item[0] -eq 'model') -and $SkipModel) { continue }
            if (Test-Path -LiteralPath $item[1]) { Write-Ok "$($item[0]) present: $($item[1])" }
            else { Write-Plan "download $($item[0]) -> $($item[1])" }
        }
    }

    # 5. Folders --------------------------------------------------------------
    Write-Step 5 'Folders'
    $paths = Get-EnginePaths
    Write-Host "  (paths from $($paths.Source))" -ForegroundColor DarkGray
    Confirm-Folder $paths.Data
    foreach ($sub in $DataSubfolders) { Confirm-Folder (Join-Path $paths.Data $sub) }
    Confirm-Folder $paths.Inbox

    # 6. Doctor ---------------------------------------------------------------
    Write-Step 6 'Health check (reel doctor)'
    if ($PSCmdlet.ShouldProcess('reel doctor', 'Run the health check')) {
        if (-not (Test-Path -LiteralPath $Reel)) { throw "reel.exe was not found at $Reel (pip install -e . should have created it)." }
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & $Reel doctor | Out-Host; $doctorCode = $LASTEXITCODE } finally { $ErrorActionPreference = $prev }
        if ($doctorCode -ne 0) {
            Write-Host ''
            Write-Host "Setup steps are done, but reel doctor reported a failure (exit code $doctorCode)." -ForegroundColor Yellow
            Write-Host 'Fix what it lists above (for example run scripts\connect-instagram.ps1), then run: .venv\Scripts\reel.exe doctor' -ForegroundColor Yellow
            $exitCode = 2
        } else {
            Write-Ok 'reel doctor passed'
        }
    }

    Write-Host ''
    if ($DryRun) {
        Write-Host 'WhatIf finished: nothing was changed. Run again without -WhatIf to apply.' -ForegroundColor Yellow
    } elseif ($exitCode -eq 0) {
        Write-Host 'Setup finished.' -ForegroundColor Green
        Write-Host 'Next: scripts\connect-instagram.ps1 (connect the account), then scripts\register-tasks.ps1 -WhatIf (preview the schedule).'
    }
} catch {
    Write-Host ''
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
} finally {
    Pop-Location
}
exit $exitCode
