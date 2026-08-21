param(
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [Parameter(Mandatory=$true)][string[]]$AdminEmails,
    [Parameter(Mandatory=$true)][string]$AccessTeamDomain,
    [Parameter(Mandatory=$true)][string]$AccessAudience,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$configRoot = Join-Path $StorageRoot 'config'
$target = Join-Path $configRoot 'remote-config.json'
if ((Test-Path -LiteralPath $target) -and -not $Force) {
    throw "Remote configuration already exists. Re-run with -Force only after reviewing it: $target"
}

$normalizedAdmins = @($AdminEmails | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
if (-not $normalizedAdmins.Count) { throw 'At least one administrator email is required.' }
foreach ($email in $normalizedAdmins) {
    if ($email -notmatch '^[^@\s]+@saigeai\.com$') { throw "Administrator must use @saigeai.com: $email" }
}
if ($AccessTeamDomain -notmatch '^https://[a-z0-9-]+\.cloudflareaccess\.com/?$') {
    throw 'AccessTeamDomain must be an https://*.cloudflareaccess.com URL.'
}
if ($AccessAudience -notmatch '^[0-9a-f]{64}$') { throw 'AccessAudience must be the 64-character Access AUD tag.' }

New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
$payload = [ordered]@{
    admin_emails = $normalizedAdmins
    access_team_domain = $AccessTeamDomain.TrimEnd('/')
    access_audience = $AccessAudience
    max_project_size = 21474836480
    max_managed_storage = 1099511627776
    min_free_space = 214748364800
    retention_seconds = 604800
    recycle_seconds = 86400
    lease_seconds = 120
}
$temporary = Join-Path $configRoot ('.remote-config.{0}.tmp' -f ([Guid]::NewGuid().ToString('N')))
$utf8 = New-Object System.Text.UTF8Encoding($false)
try {
    [System.IO.File]::WriteAllText($temporary, ($payload | ConvertTo-Json -Depth 4), $utf8)
    if (Test-Path -LiteralPath $target) {
        $backup = Join-Path $configRoot ('remote-config.{0}.bak.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        [System.IO.File]::Replace($temporary, $target, $backup)
    } else {
        [System.IO.File]::Move($temporary, $target)
    }
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
}
Write-Host "Remote configuration written: $target" -ForegroundColor Green
