[CmdletBinding()]
param([string]$InstallDir = 'C:\Program Files\Momoi', [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin -and -not $ValidateOnly) {
    $args = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -InstallDir "' + $InstallDir + '"'
    $child = Start-Process powershell.exe -Verb RunAs -ArgumentList $args -PassThru -Wait
    exit $child.ExitCode
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$payload = Join-Path $PSScriptRoot 'payload'
$manifest = Get-Content (Join-Path $PSScriptRoot 'shell-manifest.json') -Raw | ConvertFrom-Json
if (-not(Test-Path (Join-Path $InstallDir 'Momoi.exe')) -or -not(Test-Path (Join-Path $InstallDir 'runtime\python\python.exe'))) {
    throw 'An existing Momoi installation is required. This package updates only the native shell.'
}
foreach($entry in $manifest.files.PSObject.Properties) {
    $source = [IO.Path]::GetFullPath((Join-Path $payload $entry.Name))
    $target = [IO.Path]::GetFullPath((Join-Path $InstallDir $entry.Name))
    if (-not $source.StartsWith($payload + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not $target.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid shell payload path' }
    $topLevel = $target.Substring($InstallDir.Length + 1).Replace('\', '/').Split('/')[0]
    if ($topLevel -in @('data', 'runtime', 'models', 'releases')) { throw 'Invalid shell payload path' }
    if ((Get-FileHash $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { throw ('Payload checksum failed: ' + $entry.Name) }
}
if ($ValidateOnly) { Write-Host 'PASS: all shell paths and SHA256 checks; installation untouched'; exit 0 }
$installed = Get-Process Momoi -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Join-Path $InstallDir 'Momoi.exe') }
if ($installed) {
    Start-Process (Join-Path $InstallDir 'Momoi.exe') -ArgumentList '--shutdown' -Wait
    $deadline = (Get-Date).AddSeconds(30)
    do {
        $installed = Get-Process Momoi -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Join-Path $InstallDir 'Momoi.exe') }
        if (-not $installed) { break }
        Start-Sleep -Milliseconds 250
    } while((Get-Date) -lt $deadline)
    if ($installed) { throw 'Momoi did not exit. Quit it from the tray and retry.' }
}
$backup = Join-Path $InstallDir ('data\shell-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $backup | Out-Null
$changed = New-Object System.Collections.Generic.List[object]
try {
    foreach($entry in $manifest.files.PSObject.Properties) {
        $source = Join-Path $payload $entry.Name
        $target = Join-Path $InstallDir $entry.Name
        $old = Join-Path $backup $entry.Name
        $existed = Test-Path $target
        if ($existed) {
            New-Item -ItemType Directory -Force (Split-Path $old) | Out-Null
            Copy-Item $target $old -Force
        }
        $changed.Add(@{path=$target; old=$old; existed=$existed})
        New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
        Copy-Item $source $target -Force
    }
    [IO.File]::WriteAllText((Join-Path $backup 'backup.json'), ($changed | ConvertTo-Json -Depth 4))
} catch {
    foreach($item in $changed) {
        if($item.existed) { Copy-Item $item.old $item.path -Force }
        else { Remove-Item $item.path -Force -ErrorAction SilentlyContinue }
    }
    throw
}
Write-Host ('Momoi shell updated to commit ' + $manifest.commit + '. Backup: ' + $backup)
Write-Host 'You can now launch Momoi from your existing shortcut.'
Read-Host 'Press Enter to finish'
