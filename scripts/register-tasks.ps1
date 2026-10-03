<#
.SYNOPSIS
  Create or update Reel Engine's three Windows Task Scheduler tasks.

.DESCRIPTION
  Brain - Reel Engine Tick    every 15 minutes, indefinitely     (stops a run after 14 min)
  Brain - Reel Engine Daily   every day at 03:30                  (stops a run after 1 h)
  Brain - Reel Engine Weekly  every Saturday at 20:00             (stops a run after 2 h)

  Each task runs  <root>\.venv\Scripts\pythonw.exe -m reelengine.cli tick|daily|weekly
  with <root> as the working folder (pythonw: no console window flashes).
  Settings: wake the computer to run, start as soon as possible after a missed start,
  never run two copies at once (new one is ignored), run and keep running on battery.
  Runs as you, only while you are signed in (a locked screen is fine), with normal rights.

  Idempotent: tasks that already exist with these names are updated in place.
  Try -WhatIf first: it prints the full plan and registers nothing.
  This script never changes power settings. "Allow wake timers" must be on (see the note it prints).

.PARAMETER RunWhetherLoggedOn
  Also run while you are signed out. Uses LogonType Password: asks for your Windows password
  once (Windows stores it for the task). See the printed note for why S4U is not used.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1 -WhatIf

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$RunWhetherLoggedOn
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$DryRun  = [bool]$WhatIfPreference
$UserId  = "$env:USERDOMAIN\$env:USERNAME"
$Stamp   = Get-Date -Format 'yyyy-MM-dd'

$Jobs = @(
    [pscustomobject]@{ Name = 'Brain - Reel Engine Tick';   Job = 'tick';   When = 'every 15 minutes, indefinitely (:00 :15 :30 :45)'; Limit = New-TimeSpan -Minutes 14; LimitText = '14 min' },
    [pscustomobject]@{ Name = 'Brain - Reel Engine Daily';  Job = 'daily';  When = 'every day at 03:30 (Riyadh)';                       Limit = New-TimeSpan -Hours 1;    LimitText = '1 h' },
    [pscustomobject]@{ Name = 'Brain - Reel Engine Weekly'; Job = 'weekly'; When = 'every Saturday at 20:00 (Riyadh)';                 Limit = New-TimeSpan -Hours 2;    LimitText = '2 h' }
)

function New-JobTrigger([string]$Job) {
    switch ($Job) {
        'tick'   { return New-ScheduledTaskTrigger -Once -At ([datetime]::Today) -RepetitionInterval (New-TimeSpan -Minutes 15) }  # no RepetitionDuration = indefinitely
        'daily'  { return New-ScheduledTaskTrigger -Daily -At '03:30' }
        'weekly' { return New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '20:00' }
    }
    throw "unknown job $Job"
}

function Get-ExistingTask([string]$Name) {
    try {
        return Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -eq $Name } | Select-Object -First 1
    } catch {
        return $null
    }
}

# Read-only look at the "Allow wake timers" power setting (0 Disable, 1 Enable, 2 Important only).
function Get-WakeTimerState {
    $names = @{ 0 = 'Disable'; 1 = 'Enable'; 2 = 'Important wake timers only' }
    try {
        $q = (& powercfg.exe /query SCHEME_CURRENT SUB_SLEEP RTCWAKE 2>$null) -join "`n"
        $ac = [regex]::Match($q, 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)')
        $dc = [regex]::Match($q, 'Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)')
        if (-not ($ac.Success -and $dc.Success)) { return $null }
        $a = [Convert]::ToInt32($ac.Groups[1].Value, 16); $d = [Convert]::ToInt32($dc.Groups[1].Value, 16)
        return @{ Ok = ($a -eq 1 -and $d -eq 1); Text = "plugged in = $($names[$a]), on battery = $($names[$d])" }
    } catch {
        return $null
    }
}

$exitCode = 0
try {
    Write-Host 'Reel Engine: scheduled tasks' -ForegroundColor White
    if ($DryRun) { Write-Host 'WhatIf mode: this is the plan. Nothing will be registered or changed.' -ForegroundColor Yellow }
    Write-Host ''

    # Checks ------------------------------------------------------------------
    if (-not (Test-Path -LiteralPath $Pythonw)) {
        if ($DryRun) { Write-Host "  warning: $Pythonw not found (run scripts\setup.ps1 first)." -ForegroundColor Yellow }
        else { throw "$Pythonw not found. Run scripts\setup.ps1 first." }
    }
    $tz = Get-TimeZone
    $tzOk = ($tz.BaseUtcOffset -eq [TimeSpan]::FromHours(3)) -and (-not $tz.SupportsDaylightSavingTime)
    if ($tzOk) {
        Write-Host "Time zone: $($tz.Id) (UTC+3). Windows stores the start times in UTC, so they stay on Riyadh time even if the PC's time zone changes later."
    } else {
        $msg = "This PC's time zone is '$($tz.Id)', not UTC+3. Set Windows to '(UTC+03:00) Kuwait, Riyadh' first (Settings > Time & language), then run this again."
        if ($DryRun) { Write-Host "  warning: $msg" -ForegroundColor Yellow } else { throw $msg }
    }

    # Principal ---------------------------------------------------------------
    $cred = $null
    if ($RunWhetherLoggedOn) {
        $runsAs = "$UserId, whether signed in or not (LogonType Password: Windows stores your password for the task), normal rights"
        Write-Host ''
        Write-Host 'Run whether signed in or not:' -ForegroundColor Cyan
        Write-Host '  - Why not S4U ("do not store password")? An S4U task cannot decrypt your Windows Credential Manager,'
        Write-Host '    so keyring could not read the Instagram token and every publish and insights call would fail.'
        Write-Host '    A Password logon loads your profile, so Credential Manager works.'
        Write-Host '  - OneDrive syncs the Reel Inbox only while you are signed in. Videos you upload from the iPhone'
        Write-Host '    reach this PC only after you sign in; reels already in the queue still publish.'
        Write-Host '  - If you change your Windows password, run this script again with -RunWhetherLoggedOn.'
        if (-not $DryRun) {
            $cred = Get-Credential -UserName $UserId -Message 'Reel Engine tasks: your Windows password (stored by Task Scheduler, not by Reel Engine)'
            if (-not $cred) { throw 'No password entered; nothing registered.' }
        } else {
            Write-Host "  would ask for the Windows password of $UserId (Get-Credential)." -ForegroundColor Yellow
        }
        $principal = $null
    } else {
        $runsAs = "$UserId, only while signed in (LogonType Interactive; a locked screen is fine), normal rights (RunLevel Limited)"
        $principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
    }

    # Tasks -------------------------------------------------------------------
    foreach ($j in $Jobs) {
        $action   = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m reelengine.cli $($j.Job)" -WorkingDirectory $Root
        $trigger  = New-JobTrigger $j.Job
        $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew `
                        -ExecutionTimeLimit $j.Limit -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $desc = "Reel Engine $($j.Job) job. Registered by scripts\register-tasks.ps1 on $Stamp. Remove with scripts\unregister-tasks.ps1."
        $existing = Get-ExistingTask $j.Name
        $verb = if ($existing) { 'exists, will be UPDATED' } else { 'new, will be CREATED' }
        $rep = if ($trigger.Repetition -and $trigger.Repetition.Interval) {
                   $dur = if ($trigger.Repetition.Duration) { "for $($trigger.Repetition.Duration)" } else { 'indefinitely' }
                   ", repeat every $($trigger.Repetition.Interval), $dur"
               } else { '' }

        Write-Host ''
        Write-Host "Task: $($j.Name)   [$verb]" -ForegroundColor Cyan
        Write-Host "  Runs:      $Pythonw -m reelengine.cli $($j.Job)"
        Write-Host "  Start in:  $Root"
        Write-Host "  When:      $($j.When)   (trigger start $($trigger.StartBoundary)$rep)"
        Write-Host "  Settings:  wake to run = yes; start when available = yes; if already running = ignore new;"
        Write-Host "             stop after $($j.LimitText); runs on battery and keeps running if unplugged"
        Write-Host "  Runs as:   $runsAs"

        if ($PSCmdlet.ShouldProcess($j.Name, 'Register scheduled task (create or update)')) {
            $common = @{ TaskName = $j.Name; TaskPath = '\'; Action = $action; Trigger = $trigger; Settings = $settings; Description = $desc; Force = $true }
            if ($RunWhetherLoggedOn) {
                Register-ScheduledTask @common -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited | Out-Null
            } else {
                Register-ScheduledTask @common -Principal $principal | Out-Null
            }
            $info = Get-ScheduledTaskInfo -TaskName $j.Name -TaskPath '\'
            Write-Host "  done:      registered; next run $($info.NextRunTime)" -ForegroundColor Green
        }
    }
    $cred = $null

    # Wake timers (read-only) ---------------------------------------------------
    Write-Host ''
    Write-Host 'Wake timers: the tasks can wake the PC from sleep only if "Allow wake timers" is Enable.' -ForegroundColor Cyan
    Write-Host '  This script does not change it. Click path: Control Panel > Power Options > Change plan settings >'
    Write-Host '  Change advanced power settings > Sleep > Allow wake timers > Enable (On battery and Plugged in).'
    Write-Host '  A PC that is shut down (not asleep) cannot be woken; missed runs start when it is back on.'
    $wake = Get-WakeTimerState
    if ($wake) {
        $color = if ($wake.Ok) { 'Green' } else { 'Yellow' }
        Write-Host "  Current setting: $($wake.Text)" -ForegroundColor $color
    }

    # How to operate --------------------------------------------------------------
    Write-Host ''
    Write-Host 'Commands:' -ForegroundColor Cyan
    Write-Host "  Run now:        Start-ScheduledTask -TaskName 'Brain - Reel Engine Tick'"
    Write-Host "  Status:         Get-ScheduledTaskInfo -TaskName 'Brain - Reel Engine Tick'   (LastTaskResult 0 = ok)   or   .venv\Scripts\reel.exe doctor"
    Write-Host "  Pause all:      Get-ScheduledTask -TaskName 'Brain - Reel Engine *' | Disable-ScheduledTask"
    Write-Host "  Resume all:     Get-ScheduledTask -TaskName 'Brain - Reel Engine *' | Enable-ScheduledTask"
    Write-Host '  Stop posting only (kill switch, tasks keep running): .venv\Scripts\reel.exe pause'
    Write-Host '  Remove all:     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister-tasks.ps1'
    if (-not $DryRun) {
        Write-Host ''
        Write-Host 'Note: the Tick task may start within a minute because its first start time is already past (start when available).'
    } else {
        Write-Host ''
        Write-Host 'WhatIf finished: nothing was registered. Run again without -WhatIf to apply.' -ForegroundColor Yellow
    }
} catch {
    Write-Host ''
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.Exception.Message -match 'denied') { Write-Host 'If Windows says Access is denied, open PowerShell as Administrator and run this again.' -ForegroundColor Yellow }
    $exitCode = 1
}
exit $exitCode
