param([string]$Root = (Join-Path $env:USERPROFILE 'Momoi-call-research'))
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($Root)
Set-Location $root
$report = @{ session = [System.Diagnostics.Process]::GetCurrentProcess().SessionId; started = (Get-Date).ToString('o') }
try {
  $env:NAPCAT_PATCH_PACKAGE = Join-Path $root 'qqnt.json'
  $env:NAPCAT_LOAD_PATH = Join-Path $root 'qq\Files\versions\9.9.31-49738\resources\app\loadNapCat.js'
  $env:MOMOI_PROBE_AVSDK = Join-Path $root 'qq\Files\versions\9.9.31-49738\resources\app\avsdk\AVSDKPlugin.dll'
  $env:MOMOI_PROBE_REPORT = Join-Path $root 'host-report.json'
  $env:MOMOI_PROBE_PROFILE = Join-Path $root 'isolated-profile'
  New-Item -ItemType Directory -Force $env:MOMOI_PROBE_PROFILE | Out-Null
  Remove-Item $env:MOMOI_PROBE_REPORT, ($env:MOMOI_PROBE_REPORT + '.started') -ErrorAction SilentlyContinue
  & python (Join-Path $root 'audio_devices.py') (Join-Path $root 'audio-devices.json')
  if ($LASTEXITCODE -ne 0) { throw 'Audio device enumeration failed' }
  Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
  $launcher = Join-Path $root 'loader\NapCatWinBootMain.exe'
  $hook = Join-Path $root 'loader\NapCatWinBootHook.dll'
  $exe = Join-Path $root 'qq\Files\QQ.exe'
  $process = Start-Process $launcher -ArgumentList ('"' + $exe + '" "' + $hook + '" --no-sandbox --user-data-dir="' + $env:MOMOI_PROBE_PROFILE + '"') -PassThru -RedirectStandardOutput (Join-Path $root 'launcher.log') -RedirectStandardError (Join-Path $root 'launcher-error.log')
  $report.launcherPid = $process.Id
  Start-Sleep -Seconds 5
  $report.nativeModules = @(Get-Process QQ -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$root\qq\*" } | ForEach-Object { $p = $_; try { $p.Modules | Where-Object { $_.ModuleName -eq 'AVSDKPlugin.dll' } | ForEach-Object { @{ pid=$p.Id; session=$p.SessionId; path=$_.FileName } } } catch {} })
  $deadline = (Get-Date).AddSeconds(45)
  while ((Get-Date) -lt $deadline -and -not (Test-Path $env:MOMOI_PROBE_REPORT)) { Start-Sleep -Milliseconds 250 }
  $report.hostReport = Test-Path $env:MOMOI_PROBE_REPORT
  if ($report.hostReport) {
    $hostResult = Get-Content $env:MOMOI_PROBE_REPORT -Raw | ConvertFrom-Json
    $report.passed = ($report.session -ne 0 -and $report.nativeModules.Count -gt 0 -and $hostResult.plugin.postMessage -and -not $hostResult.error -and $hostResult.plugin.events.Count -gt 0)
  } else { $report.passed = $false }
} catch { $report.error = $_.Exception.ToString() }
finally {
  # Only processes launched from the isolated copied QQ directory are ours.
  Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "$root\qq\*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
  $report | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $root 'local-report.json')
}
