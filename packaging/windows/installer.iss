#ifndef AppVersion
  #define AppVersion "1.1.2"
#endif
#define Root "..\.."
#include "..\..\build\windows-qq-pin.iss"
#include "..\..\build\windows-prerequisite-pin.iss"

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

[InstallDelete]
; Only remove obsolete tooling in the private interpreter, never user data.
Type: filesandordirs; Name: "{app}\runtime\python\Lib\site-packages\pip"
Type: filesandordirs; Name: "{app}\runtime\python\Lib\site-packages\pip-*.dist-info"
Type: filesandordirs; Name: "{app}\runtime\python\Lib\ensurepip"
Type: filesandordirs; Name: "{app}\runtime\python\Lib\idlelib"
Type: filesandordirs; Name: "{app}\runtime\python\Lib\pydoc_data"

[Icons]
Name: "{group}\Momoi"; Filename: "{app}\Momoi.exe"
Name: "{autodesktop}\Momoi"; Filename: "{app}\Momoi.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Momoi.exe"; Description: "Launch Momoi"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime\napcat"
Type: filesandordirs; Name: "{app}\runtime\qq-call"
Type: filesandordirs; Name: "{app}\runtime\qq-pair"

[Code]
var
  QQDownloadPage: TDownloadWizardPage;

procedure InitializeWizard;
begin
  QQDownloadPage := CreateDownloadPage('Installing QQ components', 'Downloading the version locked to this Momoi installer.', nil);
end;

procedure StopMomoi; forward;

function InstallQQPair: String;
var
  PackagePath: String;
  Marker: AnsiString;
  Code: Integer;
begin
  Result := '';
  Marker := '';
  if LoadStringFromFile(ExpandConstant('{app}\runtime\qq-pair\pair-id.txt'), Marker) then
    if (Trim(Marker) = '{#QQPairId}') and
       FileExists(ExpandConstant('{app}\runtime\napcat\wrapper.node')) and
       FileExists(ExpandConstant('{app}\runtime\qq-call\qq\Files\QQ.exe')) and
       FileExists(ExpandConstant('{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\QQNT.dll')) and
       FileExists(ExpandConstant('{app}\runtime\qq-call\qq\Files\versions\9.9.31-49738\resources\app\avsdk\AVSDKPlugin.dll')) then exit;
  try
    PackagePath := ExpandConstant('{src}\components\{#QQPackageName}');
    if not FileExists(PackagePath) then begin
      QQDownloadPage.Clear;
      QQDownloadPage.Add('{#QQPackageURL}', '{#QQPackageName}', '{#QQPackageSHA256}');
      QQDownloadPage.Show;
      try
        QQDownloadPage.Download;
      finally
        QQDownloadPage.Hide;
      end;
      PackagePath := ExpandConstant('{tmp}\{#QQPackageName}');
    end;
    if CompareText(GetSHA256OfFile(PackagePath), '{#QQPackageSHA256}') <> 0 then
      RaiseException('QQ component package checksum mismatch.');
    if not Exec(PackagePath, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="' + ExpandConstant('{app}') + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      RaiseException('Could not start QQ component installer.');
    if Code <> 0 then RaiseException(Format('QQ component installation failed (%d).', [Code]));
    if not LoadStringFromFile(ExpandConstant('{app}\runtime\qq-pair\pair-id.txt'), Marker) then
      RaiseException('QQ component version marker missing.');
    if Trim(Marker) <> '{#QQPairId}' then RaiseException('QQ component version mismatch.');
  except
    Result := GetExceptionMessage;
  end;
end;
function WebViewInstalled: Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKLM32, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'))
    or (RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

function VCInstalled: Boolean;
var
  Installed, Major, Minor, Build: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1);
  if Result then begin
    Result := RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Major', Major) and
      RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Minor', Minor) and
      RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Bld', Build);
    if Result then Result := (Major > 14) or ((Major = 14) and ((Minor > 44) or ((Minor = 44) and (Build >= 35211))));
  end;
end;

function InstallPrerequisite(Name, URL, SHA256, Arguments: String; var ErrorMessage: String): Boolean;
var
  ResultCode: Integer;
  PackagePath: String;
begin
  ResultCode := -1;
  Result := False;
  try
    PackagePath := ExpandConstant('{src}\components\prerequisites\') + Name;
    if not FileExists(PackagePath) then begin
      QQDownloadPage.Clear;
      QQDownloadPage.Add(URL, Name, SHA256);
      QQDownloadPage.Show;
      try
        QQDownloadPage.Download;
      finally
        QQDownloadPage.Hide;
      end;
      PackagePath := ExpandConstant('{tmp}\') + Name;
    end;
    if CompareText(GetSHA256OfFile(PackagePath), SHA256) <> 0 then
      RaiseException('Prerequisite checksum mismatch.');
  except
    ErrorMessage := GetExceptionMessage;
    exit;
  end;
  Result := Exec(PackagePath, Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := (ResultCode = 0) or (ResultCode = 3010) or (ResultCode = 1638);
  if not Result then
    ErrorMessage := 'Could not install ' + Name + ' (code ' + IntToStr(ResultCode) + ').';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  StopMomoi;
  if (not VCInstalled) or (ExpandConstant('{param:forceprerequisites|0}') = '1') then
    if not InstallPrerequisite('{#VCName}', '{#VCURL}', '{#VCSHA256}', '/install /quiet /norestart', Result) then exit;
  if not WebViewInstalled then begin
    if not InstallPrerequisite('{#WebViewName}', '{#WebViewURL}', '{#WebViewSHA256}', '/silent /install', Result) then exit;
    if not WebViewInstalled then begin
      Result := 'WebView2 Runtime installation could not be verified.';
      exit;
    end;
  end;
  Result := InstallQQPair;
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
