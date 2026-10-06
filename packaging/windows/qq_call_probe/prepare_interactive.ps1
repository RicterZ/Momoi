param([string]$Root = (Join-Path $env:USERPROFILE 'Momoi-call-research'))
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = [System.IO.Path]::GetFullPath($Root)
$expectedInstaller = '0beb5cb4ff776cba822caa0abadbd53f5892f12628a86dabea1b372c82c086bc'
if ((Get-FileHash (Join-Path $root 'QQ.exe') -Algorithm SHA256).Hash -ne $expectedInstaller) { throw 'QQ installer SHA256 mismatch' }
$expectedLoaders = @{
  'NapCatWinBootMain.exe' = 'bd8ec316582bfb25e3b9b7f27a22c91437584f29879950462f789fd7bb111949'
  'NapCatWinBootHook.dll' = '962bd5caf59e9792c37eed99c9130bf1464d619d0209820be570874017d66925'
}
foreach ($name in $expectedLoaders.Keys) {
  if ((Get-FileHash (Join-Path $root ('loader\' + $name)) -Algorithm SHA256).Hash -ne $expectedLoaders[$name]) { throw "Loader SHA256 mismatch: $name" }
}
$qq = Join-Path $root 'qq'
& 'C:\Program Files\7-Zip\7z.exe' x -y "-o$qq" (Join-Path $root 'QQ.exe') | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'QQ extraction failed' }
# Use the pinned shell-loader binaries, not OneKey bootmain (different CLI).
$loader = Join-Path $root 'loader'
New-Item -ItemType Directory -Force $loader | Out-Null
foreach ($name in @('NapCatWinBootMain.exe', 'NapCatWinBootHook.dll')) {
  if (-not (Test-Path (Join-Path $loader $name))) { throw "Missing shell-loader binary: $name" }
}
$app = Join-Path $qq 'Files\versions\9.9.31-49738\resources\app'
$package = Get-Content (Join-Path $app 'package.json') -Raw | ConvertFrom-Json
$package.main = './loadNapCat.js'
[System.IO.File]::WriteAllText((Join-Path $root 'qqnt.json'), ($package | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
$bootstrap = "require(" + ((Join-Path $root 'host.cjs').Replace('\', '/') | ConvertTo-Json -Compress) + ");"
[System.IO.File]::WriteAllText((Join-Path $app 'loadNapCat.js'), $bootstrap, [System.Text.UTF8Encoding]::new($false))
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $root 'run_interactive.ps1') + '" -Root "' + $root + '"') -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName 'Momoi-AVSDK-Probe' -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'Momoi-AVSDK-Probe'
@{ root=$root; task='Momoi-AVSDK-Probe'; sha256=(Get-FileHash (Join-Path $root 'QQ.exe') -Algorithm SHA256).Hash } | ConvertTo-Json
