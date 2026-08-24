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
    # Windows PowerShell 5.1 removes embedded quotes when a multiline string is
    # handed directly to a native executable. Base64 keeps the probe as one
    # argument and makes the status script work from both powershell and pwsh.
    $databaseProbeEncoded = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($databaseProbe)
    )
    $databaseProbeCommand = "import base64;exec(base64.b64decode('$databaseProbeEncoded'))"
    try { $databaseStatus = (& $python -c $databaseProbeCommand $database | ConvertFrom-Json) } catch {}
}
$publicStatus = $null
$publicResponse = $null
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $publicRequest = [Net.HttpWebRequest]::Create($PublicUrl.TrimEnd('/') + '/healthz')
    $publicRequest.AllowAutoRedirect = $false
    $publicRequest.Timeout = 12000
    $publicResponse = $publicRequest.GetResponse()
    $publicStatus = [int]$publicResponse.StatusCode
} catch {
    if ($_.Exception.Response) {
        $publicStatus = [int]$_.Exception.Response.StatusCode
    }
} finally {
    if ($publicResponse) { $publicResponse.Close() }
}
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($StorageRoot).TrimEnd(':\')) -ErrorAction SilentlyContinue
$task = Get-ScheduledTask -TaskName 'Saige Label Reviewer Remote Origin' -ErrorAction SilentlyContinue
$watchdog = Get-ScheduledTask -TaskName 'Saige Label Reviewer Tunnel Watchdog' -ErrorAction SilentlyContinue
$tunnel = Get-Service -Name Cloudflared -ErrorAction SilentlyContinue
$tunnelProcess = Get-CimInstance Win32_Service -Filter "Name='Cloudflared'" -ErrorAction SilentlyContinue
$tunnelTcpCount = 0
$tunnelUdpCount = 0
if ($tunnelProcess -and [int]$tunnelProcess.ProcessId -gt 0) {
    $tunnelProcessId = [int]$tunnelProcess.ProcessId
    $tunnelTcpCount = @(
        Get-NetTCPConnection -OwningProcess $tunnelProcessId -State Established -ErrorAction SilentlyContinue
    ).Count
    $tunnelUdpCount = @(
        Get-NetUDPEndpoint -OwningProcess $tunnelProcessId -ErrorAction SilentlyContinue
    ).Count
}
$tunnelConnected = $tunnel -and $tunnel.Status -eq 'Running' -and
    ($tunnelTcpCount -gt 0 -or $tunnelUdpCount -gt 0)
$cuda = if (Test-Path -LiteralPath $python) { & $python -c "import torch; print('available' if torch.cuda.is_available() else 'unavailable')" 2>$null } else { 'python-missing' }

[pscustomobject]@{
    Origin       = if ($health) { "$($health.status) v$($health.version) api-$($health.api_version)" } else { 'offline' }
    Database     = if ($databaseStatus.error) { "error ($($databaseStatus.error))" } elseif ($databaseStatus) { $databaseStatus.check } else { 'unknown' }
    Storage      = if ((Test-Path -LiteralPath $StorageRoot -PathType Container) -and $drive) { 'available' } else { 'unavailable' }
    Worker       = if ($task -and $task.State -eq 'Running') { 'running' } else { 'offline' }
    Queue        = if ($databaseStatus) { "$($databaseStatus.running) running / $($databaseStatus.queued) queued" } else { 'unknown' }
    Projects     = if ($databaseStatus) { $databaseStatus.projects } else { 'unknown' }
    Tunnel       = if ($tunnel) { "$($tunnel.Status) / $($tunnel.StartType)" } else { 'offline' }
    Connector    = if ($tunnelConnected) { "connected ($tunnelTcpCount TCP / $tunnelUdpCount UDP)" } elseif ($tunnel) { 'disconnected' } else { 'unavailable' }
    Watchdog     = if ($watchdog) { $watchdog.State } else { 'not installed' }
    StartupTask  = if ($task) { $task.State } else { 'not installed' }
    CUDA         = $cuda
    FreeGB       = if ($drive) { [math]::Round($drive.Free / 1GB, 1) } else { $null }
    PublicStatus = if ($null -ne $publicStatus) { $publicStatus } else { 'unreachable' }
} | Format-List

$log = Join-Path $StorageRoot 'logs\origin.log'
if (Test-Path -LiteralPath $log) {
    Write-Host 'Recent origin log:' -ForegroundColor Cyan
    Get-Content -LiteralPath $log -Tail 20
}
