[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Installer)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$Target = Join-Path $env:TEMP ('Momoi 安装测试 ' + [Guid]::NewGuid().ToString('N'))
$Evidence = Join-Path $Root 'dist/windows/install-test'
New-Item -ItemType Directory -Path $Evidence -Force | Out-Null
function Install-App {
    param([string]$Log)
    $Setup = Start-Process -FilePath (Resolve-Path $Installer).Path -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$Target`"", "/LOG=`"$Log`"") -PassThru -Wait
    if ($Setup.ExitCode -ne 0) { throw "Installer failed: $($Setup.ExitCode)" }
}
try {
    Install-App (Join-Path $Evidence 'install.log')
    $Data = Join-Path $Target 'data'
    if (-not (Test-Path $Data)) { throw 'Installer did not create data directory' }
    $Acl = Get-Acl $Data
    $Users = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-545')
    $Writable = @($Acl.Access | Where-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $Users.Value -and
        $_.AccessControlType -eq 'Allow' -and ($_.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Modify) -eq [System.Security.AccessControl.FileSystemRights]::Modify
    })
    if ($Writable.Count -eq 0) { throw 'Installed data directory is not writable by ordinary Users' }
    $Acl | Format-List | Out-String | Set-Content (Join-Path $Evidence 'data-acl.txt')
    # Exercise the installed interpreter, model and authenticated dashboard.
    & uv run --no-sync python (Join-Path $PSScriptRoot 'smoke_backend.py') --python (Join-Path $Target 'runtime/python/python.exe') --entry (Join-Path $Target 'releases/bundled/app/backend_entry.py') --model-path (Join-Path $Target 'models/bge-small-zh-v1.5')
    if ($LASTEXITCODE -ne 0) { throw 'Installed backend smoke failed' }
    & uv run --no-sync python (Join-Path $PSScriptRoot 'smoke_mcp.py') --node (Join-Path $Target 'runtime/node/node.exe') --entry (Join-Path $Target 'runtime/mcp/node_modules/@brave/brave-search-mcp-server/dist/index.js')
    if ($LASTEXITCODE -ne 0) { throw 'Installed MCP smoke failed' }
    $Sentinel = Join-Path $Data 'preserve-check.txt'
    Set-Content $Sentinel 'Momoi user data survives upgrades and uninstall'
    $Expected = (Get-FileHash $Sentinel).Hash
    Install-App (Join-Path $Evidence 'reinstall.log')
    if ((Get-FileHash $Sentinel).Hash -ne $Expected) { throw 'Reinstall changed user data' }
    $Uninstall = Start-Process -FilePath (Join-Path $Target 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$(Join-Path $Evidence 'uninstall.log')`"") -PassThru -Wait
    if ($Uninstall.ExitCode -ne 0) { throw "Uninstaller failed: $($Uninstall.ExitCode)" }
    if (Test-Path (Join-Path $Target 'Momoi.exe')) { throw 'Uninstall left the native application installed' }
    if ((Get-FileHash $Sentinel).Hash -ne $Expected) { throw 'Uninstall removed user data' }
    'PASS: real silent installation, Users data ACL, installed backend/BGE/MCP, reinstall and uninstall data retention.' | Set-Content (Join-Path $Evidence 'result.txt')
}
finally {
    if (Test-Path (Join-Path $Target 'unins000.exe')) {
        Start-Process -FilePath (Join-Path $Target 'unins000.exe') -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -Wait
    }
    if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
}
