; ============================================================
; WA Channel Auto Publisher - Inno Setup 6 Installer Script
; Build with: ISCC.exe setup.iss
; Requires: Inno Setup 6.x from https://jrsoftware.org/isinfo.php
; ============================================================

[Setup]
AppName=WA Channel Auto Publisher
AppVersion=1.0.0
AppPublisher=WA Auto Publisher
AppPublisherURL=https://github.com/
AppSupportURL=https://github.com/
AppUpdatesURL=https://github.com/
DefaultDirName={autopf}\WA Auto Publisher
DefaultGroupName=WA Auto Publisher
OutputBaseFilename=WA_Auto_Publisher_Setup_v1.0
OutputDir=.\output
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
WizardStyle=modern
DisableProgramGroupPage=no
UninstallDisplayName=WA Channel Auto Publisher
; Uncomment and set icon path when you have an icon:
; SetupIconFile=..\assets\icon.ico
; UninstallDisplayIcon={app}\assets\icon.ico
MinVersion=10.0
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "starttask"; Description: "Start automatically at &Windows login (recommended)"; GroupDescription: "Startup:"; Flags: checked

[Dirs]
Name: "{app}\downloads"
Name: "{app}\posted"
Name: "{app}\logs"
Name: "{app}\database"
Name: "{app}\database\wa_session"

[Files]
; Copy all project files recursively
Source: "..\*.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\*.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\*.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\*.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\*.json.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dashboard\*"; DestDir: "{app}\dashboard"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\scripts\*"; DestDir: "{app}\scripts"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\tests\*"; DestDir: "{app}\tests"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\WA Auto Publisher Dashboard"; Filename: "{app}\scripts\start_dashboard.bat"; \
  Comment: "Open the WA Auto Publisher dashboard in your browser"
Name: "{group}\Run Setup Wizard"; Filename: "{app}\run_setup.bat"; \
  Comment: "Configure WA Auto Publisher"
Name: "{group}\Uninstall WA Auto Publisher"; Filename: "{uninstallexe}"
Name: "{commondesktop}\WA Auto Publisher"; Filename: "{app}\scripts\start_dashboard.bat"; \
  Tasks: desktopicon; Comment: "Open the WA Auto Publisher dashboard"

[Run]
; Step 1: Install Python packages
Filename: "{cmd}"; \
  Parameters: "/c python -m pip install -r ""{app}\requirements.txt"" --quiet"; \
  WorkingDir: "{app}"; \
  StatusMsg: "Installing Python dependencies (this may take a minute)..."; \
  Flags: runhidden waituntilterminated

; Step 2: Install Playwright Chromium
Filename: "{cmd}"; \
  Parameters: "/c python -m playwright install chromium"; \
  WorkingDir: "{app}"; \
  StatusMsg: "Installing Chromium browser for WhatsApp monitoring..."; \
  Flags: runhidden waituntilterminated

; Step 3: Register startup task (if user chose it)
Filename: "{app}\scripts\install_task.bat"; \
  StatusMsg: "Registering startup task..."; \
  Tasks: starttask; \
  Flags: runhidden waituntilterminated

; Step 4: Offer to run setup wizard
Filename: "{app}\run_setup.bat"; \
  Description: "Run the Setup Wizard to configure WhatsApp and Meta API tokens"; \
  Flags: postinstall shellexec skipifsilent unchecked

[UninstallRun]
; Remove scheduled task on uninstall
Filename: "{app}\scripts\uninstall_task.bat"; \
  Flags: runhidden waituntilterminated

[Code]
// Check Python is installed before setup begins
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
  PythonPath: String;
begin
  Result := True;
  
  // Try to find Python
  if not Exec('python', '--version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if MsgBox(
      'Python 3.10 or higher is required but was not found on your system.' + #13#10 + #13#10 +
      'Please install Python from python.org (make sure to check "Add to PATH")' + #13#10 +
      'and then run this installer again.' + #13#10 + #13#10 +
      'Click OK to open python.org, or Cancel to exit.',
      mbError, MB_OKCANCEL
    ) = IDOK then
    begin
      ShellExec('open', 'https://python.org/downloads/', '', '', SW_SHOW, ewNoWait, ResultCode);
    end;
    Result := False;
  end;
end;
