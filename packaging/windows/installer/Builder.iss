; Builder installer (Inno Setup 6). build_signed_installer.ps1 compiles it twice:
;   /DPerUser   -> Builder-Setup-<ver>-PerUser.exe   (%LOCALAPPDATA%\Programs\Builder, no admin)
;   (default)   -> Builder-Setup-<ver>.exe           (C:\Program Files\Builder, all users)
; and passes /DAppVersion=<ver> /DSourceDir=<dist\Builder> /DOutDir=<Output> /Ssigntool=... so the
; setup and its uninstaller are signed with the Cydcor certificate.
; User data lives in %APPDATA%\Builder and is never touched by install, upgrade or uninstall.

#ifndef AppVersion
  #define AppVersion "0.3.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Builder"
#endif
#ifndef OutDir
  #define OutDir "Output"
#endif

[Setup]
; Fixed AppId: upgrades install over the old version (keep it the same forever).
AppId={{6B0F3C2E-5D1A-4E7B-9C3F-2A8E4B1D7C55}
AppName=Builder
AppVersion={#AppVersion}
AppVerName=Builder {#AppVersion}
AppPublisher=Cydcor LLC
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Cydcor LLC
VersionInfoDescription=Builder setup
DefaultGroupName=Builder
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\Builder.exe
UninstallDisplayName=Builder
SetupIconFile=..\builder.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
OutputDir={#OutDir}
#ifdef PerUser
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Builder
OutputBaseFilename=Builder-Setup-{#AppVersion}-PerUser
#else
PrivilegesRequired=admin
DefaultDirName={autopf}\Builder
OutputBaseFilename=Builder-Setup-{#AppVersion}
#endif
#ifdef Sign
SignTool=signtool
SignedUninstaller=yes
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start Builder when I sign in to Windows (recommended: other apps and Claude Code rely on it)"; GroupDescription: "Startup:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\..\README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion

[Icons]
Name: "{group}\Builder"; Filename: "{app}\Builder.exe"
Name: "{group}\Builder settings"; Filename: "{userappdata}\Builder\builder.env"
Name: "{group}\Uninstall Builder"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Builder"; Filename: "{app}\Builder.exe"; Tasks: desktopicon

[Registry]
; The same value the tray menu's "Start with Windows" toggles, for the person installing.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Builder"; ValueData: """{app}\Builder.exe"" --no-browser"; Tasks: autostart

[Run]
Filename: "{app}\Builder.exe"; Description: "Start Builder now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM Builder.exe /F"; Flags: runhidden; RunOnceId: "StopBuilder"
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v Builder /f"; Flags: runhidden; RunOnceId: "NoAutostart"

[Code]
// Stop a running Builder before files are replaced on upgrade.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM Builder.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
