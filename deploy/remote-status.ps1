param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [int]$Port = 8770
)

$ErrorActionPreference = 'Continue'
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ready = $null
try { $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/readyz" -Headers @{Host='127.0.0.1'} -TimeoutSec 5 } catch {}
$public = $null
try {
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(12)
    $public = $client.GetAsync('https://saige-label-reviewer-beta.saigeai.com/healthz').GetAwaiter().GetResult()
} catch {
    $public = $null
} finally {
    if ($client) { $client.Dispose() }
    if ($handler) { $handler.Dispose() }
}
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($StorageRoot).TrimEnd(':\')) -ErrorAction SilentlyContinue
$task = Get-ScheduledTask -TaskName 'Saige Label Reviewer Remote Origin' -ErrorAction SilentlyContinue
$tunnel = Get-Process -Name cloudflared -ErrorAction SilentlyContinue
$cuda = if (Test-Path -LiteralPath $python) { & $python -c "import torch; print('available' if torch.cuda.is_available() else 'unavailable')" 2>$null } else { 'python-missing' }

[pscustomobject]@{
    Origin       = if ($ready) { $ready.status } else { 'offline' }
    Database     = if ($ready) { $ready.database } else { 'unknown' }
    Storage      = if ($ready) { $ready.storage } else { 'unknown' }
    Worker       = if ($ready) { $ready.analysis_worker } else { 'unknown' }
    Tunnel       = if ($tunnel) { "running (pid $($tunnel.Id -join ','))" } else { 'offline' }
    StartupTask  = if ($task) { $task.State } else { 'not installed' }
    CUDA         = $cuda
    FreeGB       = if ($drive) { [math]::Round($drive.Free / 1GB, 1) } else { $null }
    PublicStatus = if ($public) { [int]$public.StatusCode } else { 'unreachable' }
} | Format-List

$log = Join-Path $StorageRoot 'logs\origin.log'
if (Test-Path -LiteralPath $log) {
    Write-Host 'Recent origin log:' -ForegroundColor Cyan
    Get-Content -LiteralPath $log -Tail 20
}
