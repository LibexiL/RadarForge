; Inno Setup script for the RadarForge Windows installer.
; Built by .github/workflows/release.yml after PyInstaller has made dist\RadarForge\.
; Installs for the current user only (no administrator rights needed).
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C1F3B7E-2D4A-4C59-9E3B-5A7D0F2B9C11}
AppName=RadarForge
AppVersion={#AppVersion}
AppPublisher=LibexiL
AppPublisherURL=https://github.com/LibexiL/RadarForge
DefaultDirName={localappdata}\Programs\RadarForge
DefaultGroupName=RadarForge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=RadarForge-Setup-{#AppVersion}
SetupIconFile=..\radarforge\assets\radarforge.ico
UninstallDisplayIcon={app}\RadarForge.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; updates from inside RadarForge run this setup with /SILENT: close anything still holding the program files
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; an update replaces the whole program folder: libraries left over from the previous version can clash
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\RadarForge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\RadarForge"; Filename: "{app}\RadarForge.exe"
Name: "{group}\Uninstall RadarForge"; Filename: "{uninstallexe}"
Name: "{userdesktop}\RadarForge"; Filename: "{app}\RadarForge.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RadarForge.exe"; Description: "Start RadarForge"; Flags: nowait postinstall skipifsilent
; RadarForge's own updater passes /RELAUNCH=1: start the updated program once a silent install is done
Filename: "{app}\RadarForge.exe"; Flags: nowait; Check: RelaunchAfterUpdate

[Code]
const
  SYNCHRONIZE = $00100000;

function OpenProcess(dwDesiredAccess: DWORD; bInheritHandle: BOOL; dwProcessId: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: DWORD): DWORD;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

// RadarForge's updater passes /WAITPID=<its process id>: wait (up to a minute) until it has exited, so no
// program file is still in use when [InstallDelete] and [Files] run
function InitializeSetup: Boolean;
var
  Pid: Integer;
  H: THandle;
begin
  Result := True;
  Pid := StrToIntDef(ExpandConstant('{param:waitpid|0}'), 0);
  if Pid > 0 then
  begin
    H := OpenProcess(SYNCHRONIZE, False, Pid);
    if H <> 0 then
    begin
      WaitForSingleObject(H, 60000);
      CloseHandle(H);
    end;
  end;
end;

function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|0}') = '1');
end;
