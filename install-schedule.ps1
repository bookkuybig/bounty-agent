# Registers a Windows Scheduled Task that runs one collection cycle every 5 minutes.
# Run with -Remove to unregister. Nothing is installed until you run this file.

param(
    [switch]$Remove,
    [int]$IntervalMinutes = 5,
    [string]$TaskName = "bounty-agent-collect"
)

$ErrorActionPreference = "Stop"
$root   = $PSScriptRoot
$python = (Get-Command python).Source
# pythonw runs without a console window: nothing pops up every cycle, and
# nothing can be closed by accident (which killed runs with 0xC000013A).
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$log    = Join-Path $root "collect.log"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    exit 0
}

$arguments = "`"$(Join-Path $root 'main.py')`" collect --quiet --log `"$log`""

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Collect and score paid open-source issues" -Force | Out-Null

Write-Host "Registered '$TaskName': every $IntervalMinutes minutes."
Write-Host "Log: $log"
Write-Host "Check it with:  Get-ScheduledTaskInfo -TaskName $TaskName"
