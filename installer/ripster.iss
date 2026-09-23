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
#define AppVersion "3.7.1"
#define AppPublisher "Raccoon-Trashpanda"
#define AppURL "https://github.com/Raccoon-Trashpanda/Raccoon-Ripster"
#define SrcDir ".."

; Firewall-замок портов Apple-враппера. Одно имя на установку и удаление — иначе
; [UninstallRun] не найдёт то, что поставил [Run]. Совпадает с константами
; `tools/ripster_healthcheck.py` (тот ищет по DisplayName и при необходимости
; пересоздаёт ПРАВИЛЬНОЕ правило, а не второе поверх), поэтому тест сверяет эти
; строки с healthcheck и не даёт им разойтись.
#define GuardName "Ripster Apple wrapper only local"
#define GuardDisplay "Ripster: Apple wrapper only local"
#define GuardPorts "10020-10049,20020-20049,30020"
#define GuardDescription "Ripster: Apple wrapper ports are for this machine only (decrypt 10020-10049, m3u8 20020-20049, token 30020). Removing it exposes your Apple session to the local network."

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
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible

; Установка ПОВЕРХ работающего Ripster тихо проваливалась: Ripster.exe и
; python\python.exe заняты, файлы не перезаписываются, человек получает старую
; сборку и уверен, что обновился. Мьютекс ловит запущенное приложение ДО начала
; копирования, а CloseApplications предлагает закрыть его штатно (иначе Ripster
; продолжает жить в трее и остаётся незамеченным).
AppMutex=Global\RipsterRunning
CloseApplications=yes
CloseApplicationsFilter=Ripster.exe,python.exe,pythonw.exe
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayName={#AppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Nuke the package + python folders BEFORE extracting. Windows is case-INSENSITIVE
; on disk but Python imports are case-SENSITIVE: a stale capital-cased 'Ripster'
; (or '..\Ripster' bundled-python) left by an OLDER install keeps its old case on
; a plain overwrite, so `import ripster` then fails with "No module named
; 'ripster'" even though the folder is right there. Deleting first forces the
; fresh, correctly-cased lowercase 'ripster' from the source manifest.
Type: filesandordirs; Name: "{app}\ripster"
Type: filesandordirs; Name: "{app}\__pycache__"

[Files]
; Bundle the whole app tree INCLUDING the embedded Python (python\) so the
; install is a pure file copy -- no download, no PowerShell, no pip at install
; time. Works fully offline. Note: *.exe / *.pyc are NOT excluded (the embedded
; interpreter needs python.exe / pythonw.exe / *.dll); we exclude only the
; installer's own output and any stray packaged exe instead.
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; \
  Excludes: "\.git\*,\.git,\.venv\*,\.venv,run.bat,installer\output\*,dist\*,*-portable.zip,RipsterSetup*.exe,apple-music-downloader\*,downloads\*,tokens\*,logs\*,backups\*,*.wvd,frida-server*,tools\widevine\*,tools\lucida\node_modules\*,tools\lucida\node_modules,tools\node\*,tools\node,AppleMusicDecrypt\*,AppleMusicDecrypt,orpheus\*,orpheus,_widevine_setup\_keydive_out\*,config.yaml,*token*.txt,*.db,tgbot\*,tgbot,guest_history\*,guest_history"

; The orpheus\ tree is excluded above (43 MB of engine + its own sessions), but
; ONE file out of it must ship: the standalone PKCE helper that performs the
; Spotify login. Without it "Войти в Spotify" dies with
; "Auth helper не найден: ...\Ripster\orpheus\_auth_helper.py" — reported by a
; user on 3.0.38. It has no orpheus imports of its own (stdlib + requests), so
; shipping it alone is enough.
Source: "{#SrcDir}\orpheus\_auth_helper.py"; DestDir: "{app}\orpheus"; Flags: ignoreversion

; Seed config.yaml from the example only if the user doesn't already have one.
Source: "{#SrcDir}\config.example.yaml"; DestDir: "{app}"; DestName: "config.yaml"; \
  Flags: onlyifdoesntexist uninsneveruninstall

[Run]
; ── Firewall-замок на порты Apple-враппера: железно, на машине любого пользователя ──
; Порт 30020 враппера БЕЗ АВТОРИЗАЦИИ отдаёт dev_token, storefront_id и
; media-user-token активной учётки Apple: любой в локальной сети получал живую
; сессию владельца. Код публикует порты на петлю (`_publish` в `ripster/amd.py`),
; но это только первый замок: Docker Desktop теряет HostIp на stop+start, а любой
; процесс, поднявший враппер старой командой, переписывает публикацию на 0.0.0.0.
; Второй замок — правило firewall. На машине владельца его заводит
; `tools/ripster_healthcheck.py`; человек, скачавший RipsterSetup с GitHub, не
; имеет ни healthcheck, ни правила, и самолечения через ИИ у него нет.
;
; Name/DisplayName/Group/порты — ОДИН В ОДИН как в healthcheck (тот ищет по
; DisplayName): установщик и самоление сходятся в одно правило, а не копят дубли.
; Старое правило снимается перед созданием, поэтому повторная установка (и ремонт)
; не плодят копии. Правило НЕ привязано к Program=: слушатель на хосте — прокси
; Docker Desktop, а не Ripster.exe, фильтр по программе просто не матчил бы
; трафик. На петлю правило не влияет (Windows firewall петлевой трафик не
; фильтрует) — локальное приложение и враппер продолжают работать как работали.
;
; Снимается при удалении программы: см. [UninstallRun].
;
; Почему «снять, потом создать», а не «создать, если нет»: снимает и создаёт ОДИН
; процесс PowerShell, поэтому «снял, но не создал» возможно только если модуль
; NetSecurity грузится для Remove и отказывает для New — а при отсутствии PowerShell
; не проходит ни то ни другое и существующее правило остаётся целым. Секундная щель
; между командами безвредна: в момент установки приложение ещё не поднято.
;
; Замок: входящий TCP на порты враппера блокируется для всех профилей сети.
;
; Видимые человеку строки этого файла — ASCII (StatusMsg, Comment у ярлыка): сам
; .iss лежит в UTF-8 БЕЗ byte-order mark, и кириллица в значении, а не в
; комментарии, зависит от того, как её разберёт ISCC. Проверять это на сборке
; установщика нельзя (правило задачи), поэтому новые строки здесь английские.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; StatusMsg: "Closing Apple wrapper ports to the local network..."; \
  Parameters: "-NoProfile -Command ""Remove-NetFirewallRule -DisplayName '{#GuardDisplay}' -ErrorAction SilentlyContinue | Out-Null; Remove-NetFirewallRule -Name '{#GuardName}' -ErrorAction SilentlyContinue | Out-Null; New-NetFirewallRule -Name '{#GuardName}' -DisplayName '{#GuardDisplay}' -Group 'Ripster' -Direction Inbound -Action Block -Protocol TCP -LocalPort {#GuardPorts} -Profile Any -Description '{#GuardDescription}' | Out-Null"""; \
  Flags: runhidden skipifdoesntexist

; Про 7799 здесь СОЗНАТЕЛЬНО ничего нет: app.py слушает 127.0.0.1, а удалённый
; доступ — исходящий туннель (serveo/cloudflared), входящее правило под него не
; нужно. Входящий ALLOW без фильтра портов открывает ВЕСЬ входящий TCP — так на
; машине владельца накопились пять правил «Ripster 7799», три из них пустые по
; портам. Устанавливать их нечем и незачем.
;
; 8081 (локальный Telegram Bot API, тоже без авторизации) в замок НЕ включён:
; сервера нет в публичной сборке (`tgbot\` исключён из [Files] выше), а на машине
; владельца бот работает именно через 8081 — блокировать его значило бы ломать
; свой же сервис ради порта, которого у адресата нет.

; Offer to launch after install — Ripster's OWN native window, no browser/terminal.
Filename: "{app}\Ripster.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; \
  WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Замок снимается вместе с программой: без Ripster блокировать нечего, а
; осиротевшее Block-правило пользователь потом ищет сам. Удаляются оба имени
; (Name и DisplayName): правило мог завести и `tools/ripster_healthcheck.py`
; с другим Name. Чужие правила — в том числе любые «Ripster 7799» — не трогаем.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; \
  Parameters: "-NoProfile -Command ""Remove-NetFirewallRule -DisplayName '{#GuardDisplay}' -ErrorAction SilentlyContinue | Out-Null; Remove-NetFirewallRule -Name '{#GuardName}' -ErrorAction SilentlyContinue | Out-Null"""; \
  Flags: runhidden skipifdoesntexist; RunOnceId: "RipsterRemoveWrapperGuard"

[Icons]
; Primary launch = Ripster.exe (frozen launcher → native pywebview window; no
; .cmd/.vbs/terminal). It auto-falls-back to the browser if WebView2 is missing.
Name: "{group}\Ripster";             Filename: "{app}\Ripster.exe"; WorkingDir: "{app}"; IconFilename: "{app}\ripster.ico"
Name: "{group}\Ripster (browser)";   Filename: "{app}\Ripster (browser).cmd"; WorkingDir: "{app}"; Comment: "Use this if the main Ripster window doesn't open"
Name: "{group}\Uninstall Ripster";   Filename: "{uninstallexe}"
Name: "{autodesktop}\Ripster";       Filename: "{app}\Ripster.exe"; WorkingDir: "{app}"; IconFilename: "{app}\ripster.ico"; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files;          Name: "{app}\Ripster.cmd"
Type: files;          Name: "{app}\Ripster.vbs"

