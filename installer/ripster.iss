; Ripster — Inno Setup installer.
; Builds RipsterSetup-<ver>.exe. The installer bundles the app source and, at
; install time, runs installer\provision.ps1 which installs Python + all Python
; dependencies on the user's machine. Heavy/secret engines (Go downloader,
; ffmpeg, Widevine L3 device) are pulled/compiled per-user from the in-app
; Setup tab — nothing secret is bundled or downloaded by the installer.
;
; Build:  see installer\BUILD.md  (needs Inno Setup 6 — ISCC.exe)
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\ripster.iss

#define AppName "Ripster"
#define AppVersion "1.0.0"
#define AppPublisher "Raccoon-Trashpanda"
#define AppURL "https://github.com/Raccoon-Trashpanda/Raccoon-Ripster"
#define SrcDir ".."

[Setup]
AppId={{B7E9A0C4-9D2F-4E61-9C8A-7A1F2E5D3C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={autopf}\Ripster
DefaultGroupName=Ripster
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=RipsterSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Bundle the whole app tree INCLUDING the embedded Python (python\) so the
; install is a pure file copy -- no download, no PowerShell, no pip at install
; time. Works fully offline. Note: *.exe / *.pyc are NOT excluded (the embedded
; interpreter needs python.exe / pythonw.exe / *.dll); we exclude only the
; installer's own output and any stray packaged exe instead.
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; \
  Excludes: "\.git\*,\.git,\.venv\*,\.venv,run.bat,installer\output\*,dist\*,RipsterSetup*.exe,apple-music-downloader.exe,downloads\*,tokens\*,logs\*,backups\*,*.wvd,frida-server*,tools\widevine\*,_widevine_setup\_keydive_out\*,config.yaml"

; Seed config.yaml from the example only if the user doesn't already have one.
Source: "{#SrcDir}\config.example.yaml"; DestDir: "{app}"; DestName: "config.yaml"; \
  Flags: onlyifdoesntexist uninsneveruninstall

[Run]
; Offer to launch after install (opens Ripster's own window, no browser).
Filename: "{app}\Ripster.vbs"; Description: "{cm:LaunchProgram,{#AppName}}"; \
  Flags: postinstall nowait shellexec skipifsilent

[Icons]
Name: "{group}\Ripster";             Filename: "{app}\Ripster.vbs"; WorkingDir: "{app}"
Name: "{group}\Ripster (browser)";   Filename: "{app}\Ripster (browser).cmd"; WorkingDir: "{app}"; Comment: "Use this if the main Ripster window doesn't open"
Name: "{group}\Uninstall Ripster";   Filename: "{uninstallexe}"
Name: "{autodesktop}\Ripster";     Filename: "{app}\Ripster (browser).cmd"; WorkingDir: "{app}"; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files;          Name: "{app}\Ripster.cmd"
Type: files;          Name: "{app}\Ripster.vbs"

