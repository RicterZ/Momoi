[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Stage)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$Versions = Get-Content (Join-Path $PSScriptRoot "components.json") -Raw | ConvertFrom-Json
$Cache = Join-Path $Root "build/windows-mcp-components"
New-Item -ItemType Directory -Path $Cache -Force | Out-Null
function Download-File {
    param([string]$Url, [string]$Target)
    if (-not (Test-Path $Target)) { Invoke-WebRequest $Url -OutFile $Target }
}
$NodeName = "node-v$($Versions.node)-win-x64"
$NodeArchive = Join-Path $Cache "$NodeName.zip"
$NodeChecksums = Join-Path $Cache "node-$($Versions.node)-SHASUMS256.txt"
Download-File "https://nodejs.org/dist/v$($Versions.node)/$NodeName.zip" $NodeArchive
Download-File "https://nodejs.org/dist/v$($Versions.node)/SHASUMS256.txt" $NodeChecksums
$NodeHashLine = @(Get-Content $NodeChecksums | Where-Object { $_ -match "\s+$([regex]::Escape($NodeName)).zip$" })
if ($NodeHashLine.Count -ne 1) { throw "Missing Node checksum" }
$ExpectedNodeHash = ($NodeHashLine[0] -split '\s+')[0]
if ((Get-FileHash $NodeArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedNodeHash) { throw "Node archive checksum mismatch" }
$NodeExtract = Join-Path $Cache $NodeName
if (-not (Test-Path $NodeExtract)) { Expand-Archive $NodeArchive $Cache -Force }
$NodeTarget = Join-Path $Stage "runtime/node"
Copy-Item $NodeExtract $NodeTarget -Recurse
$UvArchive = Join-Path $Cache "uv-$($Versions.uv).zip"
$UvChecksum = Join-Path $Cache "uv-$($Versions.uv).sha256"
$UvBase = "https://github.com/astral-sh/uv/releases/download/$($Versions.uv)/uv-x86_64-pc-windows-msvc.zip"
Download-File $UvBase $UvArchive
Download-File "$UvBase.sha256" $UvChecksum
$ExpectedUvHash = ((Get-Content $UvChecksum -Raw).Trim() -split '\s+')[0]
if ((Get-FileHash $UvArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedUvHash) { throw "uv archive checksum mismatch" }
$UvTarget = Join-Path $Stage "runtime/uv"
New-Item -ItemType Directory -Path $UvTarget -Force | Out-Null
Expand-Archive $UvArchive $UvTarget -Force
# Normalize upstream archive layout, if the release has a enclosing directory.
foreach ($Name in @("uv.exe", "uvx.exe")) {
    $Candidates = @(Get-ChildItem $UvTarget -Filter $Name -Recurse)
    if ($Candidates.Count -ne 1) { throw "Missing $Name" }
    if ($Candidates[0].FullName -ne (Join-Path $UvTarget $Name)) { Copy-Item $Candidates[0].FullName (Join-Path $UvTarget $Name) }
}
Copy-Item (Join-Path $PSScriptRoot "components.json") (Join-Path $Stage "runtime/components.json")
Copy-Item (Join-Path $NodeTarget "LICENSE") (Join-Path $Stage "licenses/Node-LICENSE")
