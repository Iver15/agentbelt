# One-time setup on Windows: Python deps, autostart at logon, start now.
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1
$root = Split-Path -Parent $PSScriptRoot
py -m pip install --user --quiet psutil
$run = Join-Path $PSScriptRoot "run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User (whoami)
$principal = New-ScheduledTaskPrincipal -UserId (whoami) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "agentbelt" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "agentbelt"
Write-Output "agentbelt installed: starts at logon; toolbelt window opens on the right."
