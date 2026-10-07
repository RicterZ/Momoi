#define Root "..\.."
#include "..\..\build\windows-qq-pin.iss"
[Setup]
AppId=Momoi-QQ-Components
AppName=Momoi QQ Components
AppVersion={#QQPairVersion}
DefaultDirName={autopf}\Momoi
OutputDir={#Root}\dist\windows
OutputBaseFilename={#QQPackageBase}
Uninstallable=no
CreateAppDir=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
Compression=lzma2/ultra64
LZMADictionarySize=131072
LZMANumFastBytes=273
LZMANumBlockThreads=1
LZMAUseSeparateProcess=yes
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
PrivilegesRequired=admin
CloseApplications=no
RestartApplications=no
LicenseFile={#Root}\LICENSE
[Files]
#include "..\..\build\windows-qq-files.iss"
[InstallDelete]
; Remove obsolete native files left by older installers; user data is separate.
Type: filesandordirs; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\wmpfsdk"
Type: filesandordirs; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\miniapp"
Type: filesandordirs; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\QQScreenShot"
Type: files; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\major.node"
Type: files; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\wrapper.node"
Type: files; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\application.asar"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssInstall then
    if FileExists(ExpandConstant('{app}\Momoi.exe')) then
      if not Exec(ExpandConstant('{app}\Momoi.exe'), '--shutdown', '', SW_HIDE, ewWaitUntilTerminated, Code) then
        RaiseException('Could not stop Momoi before updating QQ components.');
end;
