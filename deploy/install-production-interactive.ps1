$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot 'install-production.ps1'

try {
    & $installer
    Write-Host ''
    Write-Host 'Installation completed successfully.' -ForegroundColor Green
    [void](Read-Host 'Press Enter to close this window')
    exit 0
} catch {
    Write-Host ''
    Write-Host 'Installation failed:' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    [void](Read-Host 'Press Enter to close this window')
    exit 1
}
