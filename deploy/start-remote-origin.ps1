param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StorageRoot = 'E:\remote\SaigeLabelReviewer',
    [int]$Port = 8770
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectRoot).Path
$python = Join-Path $project '.venv\Scripts\python.exe'
$runner = Join-Path $project 'run_remote.py'
$configuration = Join-Path $StorageRoot 'config\remote-config.json'
$logRoot = Join-Path $StorageRoot 'logs'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment is missing: $python"
}
if (-not (Test-Path -LiteralPath $configuration -PathType Leaf)) {
    throw "Remote configuration is missing: $configuration"
}
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

$log = Join-Path $logRoot 'origin.log'
if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 20MB) {
    $archive = Join-Path $logRoot ("origin-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Move-Item -LiteralPath $log -Destination $archive
    Get-ChildItem -LiteralPath $logRoot -Filter 'origin-*.log' -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 8 |
        Remove-Item -Force
}

Push-Location $project
try {
    & $python (Join-Path $project 'run.py') --check-setup
    if ($LASTEXITCODE -ne 0) { throw 'Saige setup preflight failed.' }
    & $python $runner --port $Port `
        --expected-hostname 'saige-label-reviewer-beta.saigeai.com' `
        --allowed-domain 'saigeai.com' `
        --workspace $StorageRoot *>> $log
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
