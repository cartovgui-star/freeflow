#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName "FreeFlow"
#define MyAppPublisher "FreeFlow contributors"
#define MyAppURL "https://github.com/cartovgui-star/freeflow"
#define MyAppExeName "FreeFlow.exe"

[Setup]
AppId={{32790484-A2C7-4B77-A83A-A3951ED893A9}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\FreeFlow
DefaultGroupName=FreeFlow
DisableProgramGroupPage=yes
AllowNoIcons=yes
LicenseFile=..\LICENSE
OutputDir=..\dist\installer
OutputBaseFilename=FreeFlow-Setup-{#MyAppVersion}-x64
SetupIconFile=..\assets\freeflow.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription=FreeFlow installer
VersionInfoProductName=FreeFlow
VersionInfoProductVersion={#MyAppVersion}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startup"; Description: "Start FreeFlow when I sign in to Windows"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\FreeFlow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\FreeFlow"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall FreeFlow"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FreeFlow"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\FreeFlow"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch FreeFlow"; Flags: nowait postinstall skipifsilent
