#ifndef AppVersion
  #define AppVersion "1.1.2"
#endif
#define Root "..\.."

[Setup]
AppId={{9043C31D-8A34-49ED-81BB-2AE3057FEBA7}
AppName=Momoi
AppVersion={#AppVersion}
AppPublisher=Momoi
DefaultDirName={autopf}\Momoi
DefaultGroupName=Momoi
UninstallDisplayIcon={app}\Momoi.exe
SetupIconFile={#Root}\desktop\Momoi.Desktop\Assets\momoi.ico
OutputDir={#Root}\dist\windows
OutputBaseFilename=Momoi-Setup-{#AppVersion}-x64
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
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
LicenseFile={#Root}\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Dirs]
; Keep user data on upgrades and uninstall. Only this subdirectory is writable.
Name: "{app}\data"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
#include "..\..\build\windows-installer-files.iss"
Source: "{#Root}\build\windows-prerequisites\WebView2RuntimeInstallerX64.exe"; Flags: dontcopy
Source: "{#Root}\build\windows-prerequisites\vc_redist.x64.exe"; Flags: dontcopy

[InstallDelete]
; Remove obsolete native files left by older installers; user data is separate.
Type: filesandordirs; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\wmpfsdk"
Type: filesandordirs; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\miniapp"
Type: filesandordirs; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\QQScreenShot"
Type: files; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\major.node"
Type: files; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\wrapper.node"
Type: files; Name: "{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\application.asar"

[Icons]
Name: "{group}\Momoi"; Filename: "{app}\Momoi.exe"
Name: "{autodesktop}\Momoi"; Filename: "{app}\Momoi.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Momoi.exe"; Description: "Launch Momoi"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
function WebViewInstalled: Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKLM32, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'))
    or (RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

function InstallPrerequisite(Name, Arguments: String; var ErrorMessage: String): Boolean;
var
  ResultCode: Integer;
begin
  ResultCode := -1;
  ExtractTemporaryFile(Name);
  Result := Exec(ExpandConstant('{tmp}\') + Name, Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := (ResultCode = 0) or (ResultCode = 3010) or (ResultCode = 1638);
  if not Result then
    ErrorMessage := 'Could not install ' + Name + ' (code ' + IntToStr(ResultCode) + ').';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not InstallPrerequisite('vc_redist.x64.exe', '/install /quiet /norestart', Result) then exit;
  if not WebViewInstalled then begin
    if not InstallPrerequisite('WebView2RuntimeInstallerX64.exe', '/silent /install', Result) then exit;
    if not WebViewInstalled then Result := 'WebView2 Runtime installation could not be verified.';
  end;
end;

procedure StopMomoi;
var
  ResultCode: Integer;
begin
  if FileExists(ExpandConstant('{app}\Momoi.exe')) then
    Exec(ExpandConstant('{app}\Momoi.exe'), '--shutdown', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then StopMomoi;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then StopMomoi;
end;
