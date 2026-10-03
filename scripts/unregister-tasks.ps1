<#
.SYNOPSIS
  Remove Reel Engine's three scheduled tasks, and nothing else.

.DESCRIPTION
  Removes exactly these tasks from the Task Scheduler root folder, if they exist:
    Brain - Reel Engine Tick
    Brain - Reel Engine Daily
    Brain - Reel Engine Weekly
  A running instance is stopped first. Asks for confirmation unless -Force is given.
  Data, secrets, the venv and the inbox are not touched.
  To pause instead of removing: Get-ScheduledTask -TaskName 'Brain - Reel Engine *' | Disable-ScheduledTask

.PARAMETER Force
  Do not ask for confirmation.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister-tasks.ps1 -WhatIf

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\unregister-tasks.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$TaskNames = @('Brain - Reel Engine Tick', 'Brain - Reel Engine Daily', 'Brain - Reel Engine Weekly')
$DryRun = [bool]$WhatIfPreference

$exitCode = 0
try {
    if ($DryRun) { Write-Host 'WhatIf mode: nothing will be removed.' -ForegroundColor Yellow }

    $all = @(Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue)
    $found = @($all | Where-Object { $TaskNames -contains $_.TaskName })
    foreach ($n in $TaskNames) {
        $state = ($found | Where-Object { $_.TaskName -eq $n } | Select-Object -First 1)
        if ($state) { Write-Host "  found     $n  (state: $($state.State))" } else { Write-Host "  not found $n" -ForegroundColor DarkGray }
    }
    if ($found.Count -eq 0) {
        Write-Host 'Nothing to remove.'
        exit 0
    }

    if (-not $DryRun -and -not $Force) {
        $list = ($found | ForEach-Object { $_.TaskName }) -join ', '
        if (-not $PSCmdlet.ShouldContinue("Remove $($found.Count) scheduled task(s): $list ?", 'Reel Engine')) {
            Write-Host 'Cancelled. Nothing was removed.'
            exit 0
        }
    }

    foreach ($t in $found) {
        if ($PSCmdlet.ShouldProcess($t.TaskName, 'Stop (if running) and unregister scheduled task')) {
            if ($t.State -eq 'Running') { Stop-ScheduledTask -TaskName $t.TaskName -TaskPath '\' }
            Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath '\' -Confirm:$false
            Write-Host "  removed   $($t.TaskName)" -ForegroundColor Green
        }
    }
    if ($DryRun) { Write-Host 'WhatIf finished: nothing was removed.' -ForegroundColor Yellow }
} catch {
    Write-Host "Stopped: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.Exception.Message -match 'denied') { Write-Host 'If Windows says Access is denied, open PowerShell as Administrator and run this again.' -ForegroundColor Yellow }
    $exitCode = 1
}
exit $exitCode
