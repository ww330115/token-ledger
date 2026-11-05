# Build a portable Windows package:
#   dist/token-ledger-portable-win64.zip
#
# It uses the official Python embeddable distribution (small, no installer)
# and copies tkinter/tcl/tk from the local full CPython install.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build_portable.ps1
#   powershell -ExecutionPolicy Bypass -File build_portable.ps1 -PythonVersion 3.12.10

param(
    [string]$PythonVersion = "3.12.10",
    [string]$EmbedUrl = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dist = Join-Path $Root "dist"
$Name = "token-ledger-portable-win64"
$Out = Join-Path $Dist $Name
$Zip = Join-Path $Dist "$Name.zip"

if (-not $EmbedUrl) {
    $EmbedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
}

Write-Host "==> output: $Out"
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
New-Item -ItemType Directory -Force -Path $Dist | Out-Null

$embedZip = Join-Path $Dist "python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path $embedZip)) {
    Write-Host "==> download $EmbedUrl"
    Invoke-WebRequest -Uri $EmbedUrl -OutFile $embedZip -UseBasicParsing
}
Write-Host "==> expand embed package"
Expand-Archive -Path $embedZip -DestinationPath $Out -Force

Write-Host "==> copy app files"
foreach ($f in @("token_ledger.py", "run.bat", "README.md", "LICENSE", "config.example.json")) {
    Copy-Item (Join-Path $Root $f) (Join-Path $Out $f) -Force
}
New-Item -ItemType File -Force -Path (Join-Path $Out "portable.txt") | Out-Null

# The embed package ships without tkinter, so add it from the full install.
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "python not found. A full CPython install is required to copy tkinter." }
$pyRoot = Split-Path -Parent $py
Write-Host "==> tkinter from $pyRoot"

Copy-Item (Join-Path $pyRoot "Lib\tkinter") (Join-Path $Out "tkinter") -Recurse -Force
foreach ($f in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll")) {
    $p = Join-Path $pyRoot "DLLs\$f"
    if (Test-Path $p) { Copy-Item $p $Out -Force }
}
if (Test-Path (Join-Path $pyRoot "tcl")) {
    Copy-Item (Join-Path $pyRoot "tcl") (Join-Path $Out "tcl") -Recurse -Force
} else {
    throw "tcl directory not found in $pyRoot"
}

Write-Host "==> smoke test"
& (Join-Path $Out "python.exe") (Join-Path $Out "token_ledger.py") --version

if (Test-Path $Zip) { Remove-Item $Zip -Force }
Write-Host "==> zip"
Compress-Archive -Path (Join-Path $Out "*") -DestinationPath $Zip -CompressionLevel Optimal

$size = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
Write-Host "==> done: $Zip ($size MB)"
