; Inno Setup script: wraps dist\RadarForge (from packaging\build.py) in a normal Windows installer.
; Build:  iscc /DAppVersion=1.8.0 packaging\windows\radarforge.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F4C7A52-3D0B-4B7E-9C1E-5A2B0A7D9E11}
AppName=RadarForge
AppVersion={#AppVersion}
AppPublisher=RadarForge
DefaultDirName={localappdata}\Programs\RadarForge
DefaultGroupName=RadarForge
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=RadarForge-{#AppVersion}-setup
SetupIconFile=..\..\radarforge\assets\radarforge.ico
UninstallDisplayIcon={app}\RadarForge.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Files]
Source: "..\..\dist\RadarForge\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\RadarForge"; Filename: "{app}\RadarForge.exe"
Name: "{userdesktop}\RadarForge"; Filename: "{app}\RadarForge.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\RadarForge.exe"; Description: "Start RadarForge"; Flags: nowait postinstall skipifsilent
