param(
    [string]$Cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe',
    [string]$TokenFile = "$env:LOCALAPPDATA\SaigeLabelReviewer\cloudflared-tunnel.token"
)

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This installer must run as Administrator.'
}
if (-not (Test-Path -LiteralPath $Cloudflared -PathType Leaf)) { throw "cloudflared not found: $Cloudflared" }

$manualPids = @(Get-Process -Name cloudflared -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
$serviceName = 'Cloudflared'
$existing = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction SilentlyContinue
if ($existing) {
    $expectedPrefix = '"' + $Cloudflared + '"'
    if (-not $existing.PathName.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
            $existing.PathName -notmatch '\btunnel\s+run\b') {
        throw "An unrelated $serviceName service already exists; refusing to replace it."
    }
    Write-Host "Reusing existing $serviceName service." -ForegroundColor Yellow
} else {
    if (-not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) { throw "Tunnel token file not found: $TokenFile" }
    $token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
    if (-not $token) { throw 'Tunnel token file is empty.' }
    & $Cloudflared service install $token | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cloudflared service installation failed with exit code $LASTEXITCODE" }
}
Set-Service -Name $serviceName -StartupType Automatic
Start-Service -Name $serviceName
sc.exe failure $serviceName reset= 60 actions= restart/5000/restart/5000/restart/5000 | Out-Null
Start-Sleep -Seconds 2
$service = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
foreach ($pid in $manualPids) {
    if ($pid -ne [int]$service.ProcessId) { Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue }
}
Get-Service $serviceName | Select-Object Name,Status,StartType | Format-Table -AutoSize
