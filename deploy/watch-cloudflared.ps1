param(
    [string]$ServiceName = 'Cloudflared',
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [int]$ProbeDelaySeconds = 15
)

$ErrorActionPreference = 'Stop'
$logDirectory = Join-Path $StorageRoot 'logs'
$logPath = Join-Path $logDirectory 'tunnel-watchdog.log'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-WatchdogLog {
    param([string]$Message)
    if ((Test-Path -LiteralPath $logPath -PathType Leaf) -and
            (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
        $rotatedLog = "$logPath.1"
        if (Test-Path -LiteralPath $rotatedLog -PathType Leaf) {
            Remove-Item -LiteralPath $rotatedLog -Force
        }
        Move-Item -LiteralPath $logPath -Destination $rotatedLog
    }
    Add-Content -LiteralPath $logPath `
        -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}

function Get-CloudflaredConnectorState {
    $service = Get-CimInstance Win32_Service `
        -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
    if (-not $service) {
        return [pscustomobject]@{
            Exists = $false; Running = $false; ProcessId = 0
            TcpCount = 0; UdpCount = 0; Connected = $false
        }
    }
    $serviceProcessId = [int]$service.ProcessId
    $running = $service.State -eq 'Running' -and $serviceProcessId -gt 0
    $tcpCount = 0
    $udpCount = 0
    if ($running) {
        $tcpCount = @(
            Get-NetTCPConnection -OwningProcess $serviceProcessId `
                -State Established -ErrorAction SilentlyContinue
        ).Count
        $udpCount = @(
            Get-NetUDPEndpoint -OwningProcess $serviceProcessId `
                -ErrorAction SilentlyContinue
        ).Count
    }
    return [pscustomobject]@{
        Exists = $true
        Running = $running
        ProcessId = $serviceProcessId
        TcpCount = $tcpCount
        UdpCount = $udpCount
        Connected = $running -and ($tcpCount -gt 0 -or $udpCount -gt 0)
    }
}

$first = Get-CloudflaredConnectorState
if (-not $first.Exists) {
    Write-WatchdogLog "ERROR service '$ServiceName' is not installed"
    exit 1
}

if ($first.Connected) {
    Write-WatchdogLog "OK pid=$($first.ProcessId) tcp=$($first.TcpCount) udp=$($first.UdpCount)"
    exit 0
}

# A connector can briefly have no socket while reconnecting. Confirm the state
# before restarting so a momentary network change does not cause churn.
if ($first.Running -and $ProbeDelaySeconds -gt 0) {
    Start-Sleep -Seconds $ProbeDelaySeconds
    $second = Get-CloudflaredConnectorState
    if ($second.Connected) {
        Write-WatchdogLog "RECOVERED pid=$($second.ProcessId) tcp=$($second.TcpCount) udp=$($second.UdpCount)"
        exit 0
    }
}

$reason = if ($first.Running) { 'connector has no active network endpoint' } else { 'service is stopped' }
Write-WatchdogLog "RESTART $reason"
Restart-Service -Name $ServiceName -Force
Start-Sleep -Seconds 8
$restarted = Get-CloudflaredConnectorState
if ($restarted.Connected) {
    Write-WatchdogLog "OK restarted pid=$($restarted.ProcessId) tcp=$($restarted.TcpCount) udp=$($restarted.UdpCount)"
    exit 0
}

Write-WatchdogLog "ERROR restart did not establish a connector"
exit 1
