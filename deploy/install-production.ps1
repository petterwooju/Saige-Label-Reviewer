param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [string]$Cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe',
    [string]$TokenFile = "$env:LOCALAPPDATA\SaigeLabelReviewer\cloudflared-tunnel.token",
    [string]$TaskName = 'Saige Label Reviewer Remote Origin'
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This installer must run as Administrator.'
}

& (Join-Path $PSScriptRoot 'install-cloudflared-service.ps1') `
    -Cloudflared $Cloudflared -TokenFile $TokenFile
& (Join-Path $PSScriptRoot 'install-remote-origin-task.ps1') `
    -ProjectRoot $ProjectRoot -StorageRoot $StorageRoot -TaskName $TaskName
& (Join-Path $PSScriptRoot 'install-tunnel-watchdog-task.ps1') `
    -ProjectRoot $ProjectRoot -StorageRoot $StorageRoot

Write-Host 'Saige Label Reviewer v0.1.0 production services are installed.' -ForegroundColor Green
Write-Host 'The origin and Cloudflare Tunnel will start automatically after Windows restarts.' -ForegroundColor Green
Write-Host 'A five-minute watchdog will repair a stalled Tunnel connector.' -ForegroundColor Green
