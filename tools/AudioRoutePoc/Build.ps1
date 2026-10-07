$ErrorActionPreference = 'Stop'
$project = Join-Path $PSScriptRoot 'AudioRoutePoc.csproj'
$output = Join-Path $PSScriptRoot 'publish'
dotnet publish $project -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o $output
if ($LASTEXITCODE -ne 0) { throw 'PoC build failed' }
Copy-Item (Join-Path $PSScriptRoot 'EarTrumpet-LICENSE.txt') $output
Copy-Item (Join-Path $PSScriptRoot 'README.md') $output
Write-Host "Ready: $output\AudioRoutePoc.exe"
