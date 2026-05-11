      ; Inno Setup script for Map in a Box
; Inno Setup 6.x required (https://jrsoftware.org/isinfo.php)
;
; Build after PyInstaller:
;   1. pyinstaller MapInABox.spec
;   2. Open this file in the Inno Setup IDE and click Build, or:
;      iscc MapInABox.iss

#define AppName    "Map in a Box"
#define AppVersion "1.0"
#define AppExe     "MapInABox.exe"
#define AppDir     "dist\MapInABox"

[Setup]
AppName={#AppName}
AppVersion=1.0
AppVerName={#AppName} {#AppVersion}
AppPublisher=Sam Taylor
AppCopyright=Copyright (C) 2026 Sam Taylor

; Default: per-user (no UAC). If user has admin rights they can switch to
; all-users (Program Files) via the dropdown on the install location page.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}

; Installer output
OutputDir=installer
OutputBaseFilename=MapInABox-{#AppVersion}-setup

; Compression — lzma2 ultra gives the smallest exe at the cost of slower build
Compression=lzma2/ultra64
SolidCompression=yes
CompressionThreads=auto

; Appearance
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no

; Versioning (lets Windows/Add-Remove Programs detect upgrades)
VersionInfoVersion={#AppVersion}.0.0
VersionInfoDescription={#AppName}
VersionInfoProductName={#AppName}

; Icon shown in Add/Remove Programs
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Everything PyInstaller built
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Accessible map explorer"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; Optional desktop icon
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Offer to launch after install
Filename: "{app}\{#AppExe}"; \
    Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove the cache folder the app creates (the user data in %APPDATA% is intentionally left alone)
Type: filesandordirs; Name: "{localappdata}\MapInABox"
