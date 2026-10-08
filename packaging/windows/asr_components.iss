#define Root "..\.."
#ifndef AppVersion
#define AppVersion "1.1.4"
#endif
[Setup]
AppId=Momoi-ASR-Components
AppName=Momoi Local ASR Components
AppVersion={#AppVersion}
DefaultDirName={autopf}\Momoi
OutputDir={#Root}\dist\windows\components
OutputBaseFilename=Momoi-ASR-Components-{#AppVersion}-x64
Uninstallable=no
DisableDirPage=yes
DisableProgramGroupPage=yes
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
PrivilegesRequired=admin
CloseApplications=no
RestartApplications=no
[Files]
#include "..\..\build\windows-asr-files.iss"
