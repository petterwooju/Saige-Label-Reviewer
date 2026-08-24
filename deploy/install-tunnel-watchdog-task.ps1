param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [string]$TaskName = 'Saige Label Reviewer Tunnel Watchdog'
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This installer must run as Administrator.'
}

$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$watchdogScript = Join-Path $project 'deploy\watch-cloudflared.ps1'
if (-not (Test-Path -LiteralPath $watchdogScript -PathType Leaf)) {
    throw "Tunnel watchdog is missing: $watchdogScript"
}

$powershell = (Get-Command powershell.exe).Source
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -StorageRoot "{1}"' -f $watchdogScript,$StorageRoot
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
    -LogonType ServiceAccount -RunLevel Highest
$task = New-ScheduledTask -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Restarts cloudflared when the Windows service is running but the Named Tunnel connector is offline.'

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started scheduled task: $TaskName" -ForegroundColor Green
