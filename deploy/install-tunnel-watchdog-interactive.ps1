$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot 'install-tunnel-watchdog-task.ps1'

try {
    & $installer
    Write-Host ''
    Write-Host 'Tunnel watchdog installation completed successfully.' -ForegroundColor Green
    [void](Read-Host 'Press Enter to close this window')
    exit 0
} catch {
    Write-Host ''
    Write-Host 'Tunnel watchdog installation failed:' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    [void](Read-Host 'Press Enter to close this window')
    exit 1
}
