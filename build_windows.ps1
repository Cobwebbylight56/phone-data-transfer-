# Builds PhoneDataTransfer.exe with PyInstaller.
#
#   .\build_windows.ps1              normal build
#   .\build_windows.ps1 -OneFile     single .exe (slower to start)
#   .\build_windows.ps1 -WithTools   bundle Google's platform-tools inside
#
# Run from the repo root, in a shell where `python` is 3.10+.

param(
    [switch]$OneFile,
    [switch]$WithTools,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

Write-Host "== Phone Data Transfer - Windows build ==" -ForegroundColor Cyan

# --- environment ---------------------------------------------------------
$python = "python"
& $python --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Python not found on PATH." }

Write-Host "-- Installing build dependencies"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -e ".[dev]"

if (-not $SkipTests) {
    Write-Host "-- Running tests"
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Tests failed - not building." }
}

# --- optional: bundle adb/fastboot --------------------------------------
$toolsArgs = @()
if ($WithTools) {
    Write-Host "-- Fetching platform-tools to bundle"
    & $python -c "from ptransfer.platform_tools import download_platform_tools; print(download_platform_tools('build-tools'))"
    $toolsDir = Join-Path $root "build-tools\platform-tools"
    if (Test-Path $toolsDir) {
        $toolsArgs = @("--add-data", "$toolsDir;platform-tools")
    } else {
        Write-Warning "platform-tools not found after download; building without them."
    }
}

# --- build ---------------------------------------------------------------
Write-Host "-- Running PyInstaller"
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    $mode,
    "--windowed",
    "--name", "PhoneDataTransfer",
    "--paths", "src",
    "--collect-submodules", "ptransfer",
    "--collect-submodules", "ptransfer_gui",
    "--hidden-import", "PySide6.QtSvg",
    "src\ptransfer_gui\app.py"
) + $toolsArgs

& $python @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

# --- ship the CLI alongside ---------------------------------------------
Write-Host "-- Building the CLI executable"
& $python -m PyInstaller --noconfirm --clean --onefile --console `
    --name "ptransfer" --paths src --collect-submodules ptransfer `
    "src\ptransfer\cli.py"

$out = Join-Path $root "dist"
Write-Host ""
Write-Host "Done. Output in $out" -ForegroundColor Green
Write-Host "  dist\PhoneDataTransfer\PhoneDataTransfer.exe   the app"
Write-Host "  dist\ptransfer.exe                             the command line"
if (-not $WithTools) {
    Write-Host ""
    Write-Host "adb/fastboot are not bundled - the app offers to download them on first run."
}
