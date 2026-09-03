$ErrorActionPreference = "Stop"

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bat = Join-Path $dir "run_cycle.bat"
$name = "DailiesWatchItWork"

if (-not (Test-Path $bat)) {
    Write-Host "run_cycle.bat not found next to this script." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $admin) {
    Write-Host "This window is not elevated." -ForegroundColor Yellow
    Write-Host "Close it, then right-click PowerShell and choose 'Run as administrator'."
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Registering scheduled task '$name'" -ForegroundColor Cyan
Write-Host "  script   : $bat"
Write-Host "  interval : every 75 minutes"

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $dir

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 75)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Description "Runs one trading cycle" -Force | Out-Null

$task = Get-ScheduledTask -TaskName $name
Write-Host ""
Write-Host "Registered. State: $($task.State)" -ForegroundColor Green
Write-Host "First run in about 2 minutes, then every 75 minutes."
Write-Host ""
Write-Host "Useful later:"
Write-Host "  Start-ScheduledTask   -TaskName $name    # run one cycle now"
Write-Host "  Get-ScheduledTaskInfo -TaskName $name    # last run time and result"
Write-Host "  Unregister-ScheduledTask -TaskName $name -Confirm:`$false"
Write-Host ""
Write-Host "Cycle output appends to logs\scheduled.log"
Read-Host "Press Enter to close"
