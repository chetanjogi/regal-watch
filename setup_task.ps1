# Registers "Regal Watch" in Windows Task Scheduler so it checks Regal every
# N minutes in the background (no console window). Run once from PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup_task.ps1
# To remove:
#   Unregister-ScheduledTask -TaskName "Regal Watch" -Confirm:$false

param([int]$EveryMinutes = 10)

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "watch_all.py"   # runs the Regal and Cinemark watchers back to back

# pythonw.exe = same Python, but no console window pops up every 10 minutes
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$action  = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
           -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
           -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Regal Watch" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Watches Regal for movie ticket availability" -Force | Out-Null

Write-Host "Registered 'Regal Watch' to run every $EveryMinutes minutes using $pythonw"
Write-Host "Log: $here\regal_watch.log"
