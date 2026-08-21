param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [string]$TaskName = 'Saige Label Reviewer Remote Origin'
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$startScript = Join-Path $project 'deploy\start-remote-origin.ps1'
$configuration = Join-Path $StorageRoot 'config\remote-config.json'
if (-not (Test-Path -LiteralPath $configuration -PathType Leaf)) {
    throw "Remote configuration is missing: $configuration"
}
$powershell = (Get-Command powershell.exe).Source
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -ProjectRoot "{1}" -StorageRoot "{2}"' -f $startScript,$project,$StorageRoot
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $project
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$qualifiedUser = $identity.Name
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn -User $qualifiedUser)
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $qualifiedUser -LogonType S4U -RunLevel Highest
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'Loopback-only Saige remote workbench origin. Restarts automatically after failure.'
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $releaseRoot = Join-Path $StorageRoot 'releases'
    New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
    $backup = Join-Path $releaseRoot 'scheduled-task-v0.0.1.xml'
    if (-not (Test-Path -LiteralPath $backup)) {
        Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $backup -Encoding UTF8
    }
}
# Register first so an invalid task definition cannot stop the working origin.
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started scheduled task: $TaskName" -ForegroundColor Green
