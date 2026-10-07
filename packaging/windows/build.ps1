[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$SkipInstaller,
    [string]$IsccPath = "",
    [string]$WebViewInstaller = "",
    [string]$VcInstaller = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
Set-Location $Root
if (-not [Environment]::Is64BitOperatingSystem -or $env:OS -ne "Windows_NT" -or $env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "Build on Windows x64. Cross-compiling the Python backend is unsupported."
}
function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
}
Invoke-Checked "uv" @("sync", "--locked", "--python", "3.12", "--extra", "desktop", "--group", "windows-build", "--group", "test")
if (-not $SkipInstaller) {
    if (-not $IsccPath) { $IsccPath = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6/ISCC.exe" }
    Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/smoke_installer_scripts.py", "--compiler", $IsccPath)
}
Invoke-Checked "uv" @("run", "--no-sync", "python", "-c", "import platform,struct; assert platform.machine() in ('AMD64','x86_64') and struct.calcsize('P')==8, 'Use x64 Python'")
if (-not $Version) {
    $Version = (& uv run --no-sync python -c "import importlib.metadata; print(importlib.metadata.version('momoi'))").Trim()
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?$') { throw "Invalid package version: $Version" }
Invoke-Checked "npm.cmd" @("ci")
Invoke-Checked "npm.cmd" @("run", "build")
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_icon.py")
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_model.py", "--output", "build/windows-model/bge-small-zh-v1.5")
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/build_release.py", "--version", $Version)
$CodeArchives = @(Get-ChildItem "dist/windows/releases/Momoi-Code-$Version-*.zip" | Sort-Object LastWriteTime -Descending)
$CodeArchive = $CodeArchives[0].FullName
$Stage = Join-Path $Root "dist/windows/app"
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
$Project = "desktop/Momoi.Desktop/Momoi.Desktop.csproj"
Invoke-Checked "dotnet" @("restore", $Project, "-r", "win-x64", "--locked-mode")
Invoke-Checked "dotnet" @("publish", $Project, "-c", "Release", "-r", "win-x64", "--self-contained", "true", "--no-restore", "-p:PublishSingleFile=false", "-p:Version=$Version", "-o", $Stage)
# The private interpreter and dependencies are installation components, not app code.
Invoke-Checked "uv" @("python", "install", "3.12")
$ManagedPython = (& uv python find --managed-python 3.12).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not find managed Python" }
$PythonRoot = (& $ManagedPython -c "import sys; print(sys.base_prefix)").Trim()
$Runtime = Join-Path $Stage "runtime"
New-Item -ItemType Directory -Path $Runtime -Force | Out-Null
Copy-Item $PythonRoot (Join-Path $Runtime "python") -Recurse
$PrivatePython = Join-Path $Runtime "python/python.exe"
$Requirements = Join-Path $Root "dist/windows/releases/requirements.txt"
Invoke-Checked "uv" @("pip", "install", "--python", $PrivatePython, "--target", (Join-Path $Runtime "python/Lib/site-packages"), "--require-hashes", "--only-binary", ":all:", "-r", $Requirements)
Copy-Item "dist/windows/releases/runtime.json" (Join-Path $Runtime "runtime.json")
$Bundled = Join-Path $Stage "releases/bundled"
New-Item -ItemType Directory -Path $Bundled -Force | Out-Null
Expand-Archive $CodeArchive $Bundled -Force
New-Item -ItemType Directory -Path (Join-Path $Stage "models") -Force | Out-Null
Copy-Item "build/windows-model/bge-small-zh-v1.5" (Join-Path $Stage "models/bge-small-zh-v1.5") -Recurse
$ModelCache = Join-Path $Stage "models/bge-small-zh-v1.5/.cache"
if (Test-Path $ModelCache) { Remove-Item $ModelCache -Recurse -Force }
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/trim_python.py", "--root", (Join-Path $Runtime "python"), "--report", "build/windows-python-trim.json")
$Entry = Join-Path $Bundled "app/backend_entry.py"
Invoke-Checked $PrivatePython @("-I", "-B", "-X", "utf8", $Entry, "--check-model", "--model-path", (Join-Path $Stage "models/bge-small-zh-v1.5"))
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/smoke_backend.py", "--python", $PrivatePython, "--entry", $Entry, "--model-path", (Join-Path $Stage "models/bge-small-zh-v1.5"))
# Collect notices from the shipped environment rather than developer tooling.
Invoke-Checked $PrivatePython @("-I", "packaging/windows/collect_licenses.py", "--output", (Join-Path $Stage "licenses"))
Copy-Item "tools/AudioRoutePoc/EarTrumpet-LICENSE.txt" (Join-Path $Stage "licenses/EarTrumpet-LICENSE.txt")
Copy-Item "desktop/Momoi.Desktop/packages.lock.json" (Join-Path $Stage "licenses/dotnet-packages.lock.json")
& (Join-Path $PSScriptRoot "prepare_mcp.ps1") -Stage $Stage
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_napcat.py", "--stage", $Stage)
# The fork exports Windows-only code into the code ZIP; native QQ/AVSDK and
# loader are separately staged installation components. No Linux scripts run.
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_qq_call.py", "--stage", $Stage)
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/smoke_mcp.py", "--node", (Join-Path $Stage "runtime/node/node.exe"), "--uv", (Join-Path $Stage "runtime/uv/uv.exe"))
Copy-Item "uv.lock" (Join-Path $Stage "licenses/uv.lock")
Copy-Item "LICENSE" (Join-Path $Stage "licenses/Momoi-LICENSE")
if ($SkipInstaller) { Write-Output "Application ready: $Stage"; return }
$Prerequisites = Join-Path $Root "build/windows-prerequisites"
New-Item -ItemType Directory -Path $Prerequisites -Force | Out-Null
function Get-MicrosoftInstaller {
    param([string]$Provided, [string]$Url, [string]$Name)
    $Destination = Join-Path $Prerequisites $Name
    $OriginPath = "$Destination.origin.json"
    if ($Provided) {
        Copy-Item $Provided $Destination -Force
        $ProvidedOrigin = "$Provided.origin.json"
        if (-not (Test-Path $ProvidedOrigin)) { throw "Provided prerequisite requires its official origin metadata: $ProvidedOrigin" }
        Copy-Item $ProvidedOrigin $OriginPath -Force
    }
    elseif (-not (Test-Path $Destination) -or -not (Test-Path $OriginPath)) {
        $Partial = "$Destination.partial"
        for ($Attempt = 1; $Attempt -le 4; $Attempt++) {
            try {
                $Response = Invoke-WebRequest -Uri $Url -OutFile $Partial -PassThru
                $OfficialURL = $Response.BaseResponse.RequestMessage.RequestUri.AbsoluteUri
                Move-Item $Partial $Destination -Force
                @{url=$OfficialURL; sha256=(Get-FileHash $Destination -Algorithm SHA256).Hash.ToLowerInvariant()} | ConvertTo-Json | Set-Content $OriginPath -Encoding UTF8
                break
            }
            catch {
                Remove-Item $Partial -Force -ErrorAction SilentlyContinue
                if ($Attempt -eq 4) { throw }
                Write-Warning "Download of $Name failed (attempt $Attempt/4); retrying: $($_.Exception.Message)"
                Start-Sleep -Seconds (2 * $Attempt)
            }
        }
    }
    $Signature = Get-AuthenticodeSignature $Destination
    if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        throw "Microsoft installer signature verification failed: $Destination"
    }
    return $Destination
}
Get-MicrosoftInstaller $WebViewInstaller "https://go.microsoft.com/fwlink/?linkid=2124701" "WebView2RuntimeInstallerX64.exe" | Out-Null
Get-MicrosoftInstaller $VcInstaller "https://aka.ms/vs/17/release/vc_redist.x64.exe" "vc_redist.x64.exe" | Out-Null
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_prerequisites.py", "--source", $Prerequisites, "--destination", "dist/windows/components/prerequisites", "--include", "build/windows-prerequisite-pin.iss")
Get-ChildItem $Prerequisites -Filter *.exe | Get-FileHash -Algorithm SHA256 | Format-Table | Out-String | Set-Content (Join-Path $Prerequisites "SHA256SUMS.txt")
if (-not $IsccPath) {
    $IsccPath = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6/ISCC.exe"
}
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_qq_pair.py", "--stage", $Stage, "--output", "build/windows-qq-pin.iss")
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_installer_files.py", "--stage", $Stage, "--component", "qq", "--output", "build/windows-qq-files.iss")
Invoke-Checked $IsccPath @("packaging/windows/qq_components.iss")
$Pair = Get-Content (Join-Path $Stage "runtime/qq-pair/components.json") -Raw | ConvertFrom-Json
$QQInstallers = @(Get-ChildItem "dist/windows/Momoi-QQ-Components-$($Pair.version)-*-x64.exe" | Sort-Object LastWriteTime -Descending)
$QQInstaller = $QQInstallers[0].FullName
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_qq_pair.py", "--stage", $Stage, "--output", "build/windows-qq-pin.iss", "--archive", $QQInstaller)
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/prepare_installer_files.py", "--stage", $Stage, "--component", "main", "--output", "build/windows-installer-files.iss")
Invoke-Checked $IsccPath @("/DAppVersion=$Version", "packaging/windows/installer.iss")
$Installer = Join-Path $Root "dist/windows/Momoi-Setup-$Version-x64.exe"
Get-FileHash $Installer -Algorithm SHA256 | Format-List
Write-Output "Installer ready: $Installer"

$Components = Join-Path $Root "dist/windows/components"
New-Item -ItemType Directory -Path $Components -Force | Out-Null
Move-Item "dist/windows/Momoi-QQ-Components-*-x64.exe" $Components -Force
Move-Item "dist/windows/Momoi-QQ-Components-*-x64.json" $Components -Force
Invoke-Checked "uv" @("run", "--no-sync", "python", "packaging/windows/package_installer.py", "--directory", "dist/windows", "--version", $Version)
