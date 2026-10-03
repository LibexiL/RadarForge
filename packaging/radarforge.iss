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

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\RadarForge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\RadarForge"; Filename: "{app}\RadarForge.exe"
Name: "{group}\Uninstall RadarForge"; Filename: "{uninstallexe}"
Name: "{userdesktop}\RadarForge"; Filename: "{app}\RadarForge.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RadarForge.exe"; Description: "Start RadarForge"; Flags: nowait postinstall skipifsilent
