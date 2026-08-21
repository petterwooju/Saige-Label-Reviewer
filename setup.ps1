param(
    [switch]$CpuOnly,
    [switch]$SkipModelDownload
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvRoot = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$setupMarker = Join-Path $venvRoot '.saige-reviewer-setup.json'
$setupLockPath = Join-Path $projectRoot '.saige-reviewer-setup.lock'

try {
    $setupLock = [System.IO.File]::Open(
        $setupLockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
} catch {
    throw 'Another setup is already running for this project. Wait for it to finish and retry.'
}

try {
Remove-Item -LiteralPath $setupMarker -Force -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $venvPython)) {
    $created = $false
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($selector in @('-3.12', '-3.13')) {
            $previousErrorActionPreference = $ErrorActionPreference
            try {
                # Windows PowerShell 5.1 turns a launcher's expected stderr for a
                # missing selector into a terminating NativeCommandError when the
                # script-wide preference is Stop. Probe it without aborting so a
                # machine with only Python 3.13 can proceed after the 3.12 check.
                $ErrorActionPreference = 'SilentlyContinue'
                & $pyLauncher.Source $selector -c "import platform,sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and (3, 12) <= sys.version_info[:2] < (3, 14) else 1)" 2>$null
                $selectorProbeExitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
            if ($selectorProbeExitCode -eq 0) {
                Write-Host "Creating .venv with Python $($selector.Substring(1)) from the Windows launcher..."
                & $pyLauncher.Source $selector -m venv $venvRoot
                if ($LASTEXITCODE -ne 0) { throw "Python $selector failed to create .venv" }
                $created = $true
                break
            }
        }
    }
    if (-not $created) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw 'Python 3.12 or 3.13 is required; Python 3.12 is recommended.'
        }
        & $pythonCommand.Source -c "import platform,sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and (3, 12) <= sys.version_info[:2] < (3, 14) else 1)"
        if ($LASTEXITCODE -ne 0) {
            throw 'Python must be version 3.12 or 3.13; Python 3.12 is recommended.'
        }
        Write-Host 'Python 3.12 launcher not found; creating .venv with the validated python.exe...'
        & $pythonCommand.Source -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) { throw 'python.exe failed to create .venv' }
    }
}

& $venvPython -c "import platform,sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and (3, 12) <= sys.version_info[:2] < (3, 14) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw 'Existing .venv is not Python 3.12/3.13. Move it aside and rerun setup.ps1.'
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed' }

$useCuda = -not $CpuOnly -and (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue)
if ($useCuda) {
    $torchIndex = 'https://download.pytorch.org/whl/cu130'
    $torchVariant = 'CUDA 13.0'
    $expectedTorchFlavor = 'cuda'
} else {
    $torchIndex = 'https://download.pytorch.org/whl/cpu'
    $torchVariant = 'CPU'
    $expectedTorchFlavor = 'cpu'
}

$torchCheck = @'
import sys

try:
    import torch
except Exception:
    print('no')
else:
    version = torch.__version__
    expected = sys.argv[1]
flavor_matches = '+cu130' in version if expected == 'cuda' else '+cu' not in version
print('yes' if version.startswith('2.13.0') and flavor_matches else 'no')
'@
$torchReady = & $venvPython -c $torchCheck $expectedTorchFlavor
if ($torchReady -notcontains 'yes') {
    Write-Host "Installing the PyTorch 2.13.0 $torchVariant build..."
    & $venvPython -m pip install --force-reinstall --no-deps 'torch==2.13.0' --index-url $torchIndex
    if ($LASTEXITCODE -ne 0) {
        throw "PyTorch $torchVariant install failed; retry with setup.ps1 -CpuOnly if appropriate"
    }
}

& $venvPython -m pip install -e "${projectRoot}[analysis,remote]"
if ($LASTEXITCODE -ne 0) { throw 'Analysis dependency installation failed' }

if ($useCuda) {
    & $venvPython -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw 'CUDA PyTorch was installed but the GPU is unavailable; update the driver or rerun setup.ps1 -CpuOnly'
    }
}

if (-not $SkipModelDownload) {
    Write-Host 'Downloading the pinned DINOv2 revision (about 350 MB)...'
    $modelName = & $venvPython -c "from saige_reviewer.analysis import AnalysisConfig; print(AnalysisConfig().model)"
    $modelRevision = & $venvPython -c "from saige_reviewer.analysis import AnalysisConfig; print(AnalysisConfig().model_revision)"
    if ($LASTEXITCODE -ne 0 -or -not $modelName -or -not $modelRevision) {
        throw 'Could not read the pinned model identity from the installed application'
    }
    $downloadScript = @'
import sys
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id=sys.argv[1],
    revision=sys.argv[2],
    allow_patterns=['config.json', 'model.safetensors'],
)
'@
    & $venvPython -c $downloadScript $modelName $modelRevision
    if ($LASTEXITCODE -ne 0) {
        throw 'DINOv2 download failed; check the network or retry with -SkipModelDownload'
    }
    $modelWeightOutput = & $venvPython -c "from huggingface_hub import try_to_load_from_cache; from saige_reviewer.analysis import AnalysisConfig; c=AnalysisConfig(); print(try_to_load_from_cache(c.model, 'model.safetensors', revision=c.model_revision))"
    $modelWeightPath = [string]($modelWeightOutput | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $modelWeightPath -PathType Leaf)) {
        throw 'Pinned DINOv2 weights were not found after download'
    }
    $modelWeightHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $modelWeightPath).Hash.ToLowerInvariant()
    $modelWeightSize = (Get-Item -LiteralPath $modelWeightPath).Length
    $modelConfigOutput = & $venvPython -c "from huggingface_hub import try_to_load_from_cache; from saige_reviewer.analysis import AnalysisConfig; c=AnalysisConfig(); print(try_to_load_from_cache(c.model, 'config.json', revision=c.model_revision))"
    $modelConfigPath = [string]($modelConfigOutput | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $modelConfigPath -PathType Leaf)) {
        throw 'Pinned DINOv2 config was not found after download'
    }
    $modelConfigHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $modelConfigPath).Hash.ToLowerInvariant()
    $modelConfigSize = (Get-Item -LiteralPath $modelConfigPath).Length
} else {
    Write-Host 'Model download skipped. Analysis remains offline until the pinned model is cached.' -ForegroundColor Yellow
    $modelWeightHash = $null
    $modelWeightSize = $null
    $modelConfigHash = $null
    $modelConfigSize = $null
}

$pythonImplementation = & $venvPython -c "import platform; print(platform.python_implementation())"
if ($LASTEXITCODE -ne 0 -or -not $pythonImplementation) {
    throw 'Could not identify the virtual environment Python implementation'
}
$pythonVersion = & $venvPython -c "import platform; print(platform.python_version())"
if ($LASTEXITCODE -ne 0 -or -not $pythonVersion) {
    throw 'Could not identify the virtual environment Python version'
}
$appVersion = & $venvPython -c "from saige_reviewer import __version__; print(__version__)"
$currentModelRevision = & $venvPython -c "from saige_reviewer.analysis import AnalysisConfig; print(AnalysisConfig().model_revision)"
if ($LASTEXITCODE -ne 0 -or -not $appVersion -or -not $currentModelRevision) {
    throw 'Installed application preflight failed; setup marker was not written'
}
$manifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $projectRoot 'pyproject.toml')).Hash.ToLowerInvariant()
$markerPayload = [ordered]@{
    schema = 1
    app_version = $appVersion.Trim()
    manifest_sha256 = $manifestHash
    model_revision = $currentModelRevision.Trim()
    python_implementation = ([string]$pythonImplementation).Trim()
    python_version = ([string]$pythonVersion).Trim()
    model_download_skipped = [bool]$SkipModelDownload
    model_weight_sha256 = $modelWeightHash
    model_weight_size = $modelWeightSize
    model_config_sha256 = $modelConfigHash
    model_config_size = $modelConfigSize
    torch_variant = $torchVariant
} | ConvertTo-Json
$markerTemporary = "$setupMarker.tmp-$PID"
try {
    [System.IO.File]::WriteAllText($markerTemporary, $markerPayload, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $markerTemporary -Destination $setupMarker -Force
} finally {
    Remove-Item -LiteralPath $markerTemporary -Force -ErrorAction SilentlyContinue
}

$postCheckSucceeded = $false
try {
    & $venvPython (Join-Path $projectRoot 'run.py') --check-setup
    $postCheckSucceeded = ($LASTEXITCODE -eq 0)
} finally {
    if (-not $postCheckSucceeded) {
        Remove-Item -LiteralPath $setupMarker -Force -ErrorAction SilentlyContinue
    }
}
if (-not $postCheckSucceeded) {
    throw 'Post-install dependency and model integrity checks failed; the setup marker was removed'
}

Write-Host ''
Write-Host 'Analysis environment is ready. Run: .\.venv\Scripts\python.exe run.py' -ForegroundColor Green
} finally {
    if ($setupLock) { $setupLock.Dispose() }
    Remove-Item -LiteralPath $setupLockPath -Force -ErrorAction SilentlyContinue
}
