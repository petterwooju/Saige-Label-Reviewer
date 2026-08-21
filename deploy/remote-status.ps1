param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [int]$Port = 8770,
    [string]$ExpectedHostname = 'saige-label-reviewer-beta.saigeai.com',
    [string]$PublicUrl = 'https://saige-label-reviewer-beta.saigeai.com'
)

$ErrorActionPreference = 'Continue'
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$health = $null
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" `
        -Headers @{Host=$ExpectedHostname} -TimeoutSec 5
} catch {}
$databaseStatus = $null
$database = Join-Path $StorageRoot 'db\remote.sqlite3'
if ((Test-Path -LiteralPath $python -PathType Leaf) -and
        (Test-Path -LiteralPath $database -PathType Leaf)) {
    $databaseProbe = @'
import json, sqlite3, sys
try:
    connection = sqlite3.connect(sys.argv[1], timeout=5)
    check = connection.execute("PRAGMA quick_check").fetchone()[0]
    queued = connection.execute("SELECT count(*) FROM analysis_runs WHERE state='queued'").fetchone()[0]
    running = connection.execute("SELECT count(*) FROM analysis_runs WHERE state='running'").fetchone()[0]
    projects = connection.execute("SELECT count(*) FROM projects WHERE state NOT IN ('purged')").fetchone()[0]
    print(json.dumps({"check": check, "queued": queued, "running": running, "projects": projects}))
except Exception as error:
    print(json.dumps({"error": type(error).__name__}))
'@
    try { $databaseStatus = (& $python -c $databaseProbe $database | ConvertFrom-Json) } catch {}
}
$public = $null
try {
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(12)
    $public = $client.GetAsync(($PublicUrl.TrimEnd('/') + '/healthz')).GetAwaiter().GetResult()
} catch {
    $public = $null
} finally {
    if ($client) { $client.Dispose() }
    if ($handler) { $handler.Dispose() }
}
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($StorageRoot).TrimEnd(':\')) -ErrorAction SilentlyContinue
$task = Get-ScheduledTask -TaskName 'Saige Label Reviewer Remote Origin' -ErrorAction SilentlyContinue
$tunnel = Get-Service -Name Cloudflared -ErrorAction SilentlyContinue
$cuda = if (Test-Path -LiteralPath $python) { & $python -c "import torch; print('available' if torch.cuda.is_available() else 'unavailable')" 2>$null } else { 'python-missing' }

[pscustomobject]@{
    Origin       = if ($health) { "$($health.status) v$($health.version) api-$($health.api_version)" } else { 'offline' }
    Database     = if ($databaseStatus.error) { "error ($($databaseStatus.error))" } elseif ($databaseStatus) { $databaseStatus.check } else { 'unknown' }
    Storage      = if ((Test-Path -LiteralPath $StorageRoot -PathType Container) -and $drive) { 'available' } else { 'unavailable' }
    Worker       = if ($task -and $task.State -eq 'Running') { 'running' } else { 'offline' }
    Queue        = if ($databaseStatus) { "$($databaseStatus.running) running / $($databaseStatus.queued) queued" } else { 'unknown' }
    Projects     = if ($databaseStatus) { $databaseStatus.projects } else { 'unknown' }
    Tunnel       = if ($tunnel) { "$($tunnel.Status) / $($tunnel.StartType)" } else { 'offline' }
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
