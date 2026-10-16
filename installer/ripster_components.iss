; Ripster — страница дополнительных компонентов и проверка установки (подключается из ripster.iss через #include).
;
; Что делает установщик сверх копирования файлов (владелец, 11.10.2026: «зависимости сразу, максимально просто»):
;   1. Минимум программ уже лежит в пакете: ffmpeg, ffprobe, mp4decrypt, mp4extract (папка tools\).
;   2. Страница «Дополнительные компоненты»: SoundCloud, Spotify+Beatport, JioSaavn докачиваются прямо при установке.
;      Всё можно пропустить (снять галочки) — тогда они ставятся позже из Настроек, как раньше.
;   3. Перед кнопкой «Готово» установщик САМ проверяет установку (python -m ripster.install_check): пакеты Python,
;      встроенные программы, конфиг, файлы интерфейса и пробный запуск сервера на запасном порту.
;      Итог — на последней странице; подробности — logs\install_check.txt.
;
; Тихая установка (/VERYSILENT): страница не показывается; компоненты — параметром /DL=soundcloud,orpheus,jiosaavn;
; проверка выполняется всегда, итог пишется в журнал и в logs\install_check.txt.
;
; Файл с BOM (UTF-8): кириллица стоит только в [CustomMessages]. В коде Pascal — ASCII.

[CustomMessages]
english.CompTitle=Optional components
russian.CompTitle=Дополнительные компоненты
english.CompSub=Download extra engines now, so you do not have to set them up later
russian.CompSub=Скачать дополнительные движки сейчас, чтобы не настраивать их потом
english.CompDesc=Recommended. Uncheck everything to skip: you can install these later in Settings > Setup. Needs internet; may take a few minutes.
russian.CompDesc=Рекомендуется. Чтобы пропустить, снимите все галочки: позже их можно поставить в Настройках > Setup. Нужен интернет, это может занять несколько минут.
english.CompSoundcloud=SoundCloud (Node.js + downloader)
russian.CompSoundcloud=SoundCloud (Node.js и загрузчик)
english.CompOrpheus=Spotify and Beatport (OrpheusDL engine)
russian.CompOrpheus=Spotify и Beatport (движок OrpheusDL)
english.CompJio=JioSaavn (India)
russian.CompJio=JioSaavn (Индия)
english.RunTitle=Finishing setup
russian.RunTitle=Завершение установки
english.RunSub=Installing selected components and checking the installation
russian.RunSub=Устанавливаю выбранные компоненты и проверяю установку
english.StInstalling=Downloading: %1 (this can take a few minutes)...
russian.StInstalling=Скачиваю: %1 (это может занять несколько минут)...
english.StChecking=Checking the installation...
russian.StChecking=Проверяю установку...
english.ResOk=Installation check passed: everything works.
russian.ResOk=Проверка установки пройдена: всё работает.
english.ResWarn=Installation check passed. Not critical: %1. Details: %2
russian.ResWarn=Проверка установки пройдена. Не критично: %1. Подробности: %2
english.ResFail=Installation check found PROBLEMS: %1. Details: %2
russian.ResFail=Проверка установки нашла ПРОБЛЕМЫ: %1. Подробности: %2
english.ResNoRun=Installation check could not be started. Details: %1
russian.ResNoRun=Проверку установки не удалось запустить. Подробности: %1

[Code]
var
  CompPage: TInputOptionWizardPage;
  CheckText: String;
  CheckBad: Boolean;

function DlParam(): String;
begin
  Result := ExpandConstant('{param:DL|}');
end;

procedure InitializeWizard();
begin
  CompPage := CreateInputOptionPage(wpSelectTasks, CustomMessage('CompTitle'), CustomMessage('CompSub'),
    CustomMessage('CompDesc'), False, False);
  CompPage.Add(CustomMessage('CompSoundcloud'));
  CompPage.Add(CustomMessage('CompOrpheus'));
  CompPage.Add(CustomMessage('CompJio'));
  CompPage.Values[0] := True;
  CompPage.Values[1] := True;
  CompPage.Values[2] := True;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (PageID = CompPage.ID) and WizardSilent then
    Result := True;
end;

{ Список ключей компонентов через запятую: из страницы или из параметра /DL= (тихая установка) }
function SelectedComponents(): String;
begin
  Result := '';
  if WizardSilent then
  begin
    Result := DlParam();
    exit;
  end;
  if CompPage.Values[0] then Result := Result + 'soundcloud,';
  if CompPage.Values[1] then Result := Result + 'orpheus,';
  if CompPage.Values[2] then Result := Result + 'jiosaavn,';
  if (Length(Result) > 0) and (Result[Length(Result)] = ',') then
    Delete(Result, Length(Result), 1);
end;

function NextKey(var List: String): String;
var
  P: Integer;
begin
  P := Pos(',', List);
  if P = 0 then
  begin
    Result := Trim(List);
    List := '';
  end
  else
  begin
    Result := Trim(Copy(List, 1, P - 1));
    Delete(List, 1, P);
  end;
end;

function KeyTitle(Key: String): String;
begin
  if Key = 'soundcloud' then Result := 'SoundCloud'
  else if Key = 'orpheus' then Result := 'OrpheusDL (Spotify, Beatport)'
  else if Key = 'jiosaavn' then Result := 'JioSaavn'
  else Result := Key;
end;

function Py(): String;
begin
  Result := ExpandConstant('{app}\python\python.exe');
end;

{ Один компонент: тем же кодом, что кнопка во вкладке Setup. Сбой не прерывает установку — он попадёт в итог проверки. }
procedure InstallOne(Key: String);
var
  Rc: Integer;
begin
  Exec(Py(), '-m ripster.setup_cli --base "' + ExpandConstant('{app}') + '" --component ' + Key,
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Rc);
  Log('component ' + Key + ' exit code ' + IntToStr(Rc));
end;

{ Итог проверки из ASCII-файла «FAIL id» / «WARN id»: строки соберём в текст для последней страницы }
procedure BuildCheckText(Rc: Integer; SummaryFile, ReportFile: String);
var
  Lines: TArrayOfString;
  I, Fails, Warns: Integer;
  FailIds, WarnIds: String;
begin
  Fails := 0; Warns := 0; FailIds := ''; WarnIds := '';
  if LoadStringsFromFile(SummaryFile, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      if Copy(Lines[I], 1, 5) = 'FAIL ' then
      begin
        Fails := Fails + 1;
        if Fails <= 5 then FailIds := FailIds + Copy(Lines[I], 6, 40) + '; ';
      end
      else if Copy(Lines[I], 1, 5) = 'WARN ' then
      begin
        Warns := Warns + 1;
        if Warns <= 5 then WarnIds := WarnIds + Copy(Lines[I], 6, 40) + '; ';
      end;
    end
  else if Rc <> 0 then
  begin
    CheckBad := True;
    CheckText := FmtMessage(CustomMessage('ResNoRun'), [ReportFile]);
    exit;
  end;
  if Fails > 0 then
  begin
    CheckBad := True;
    CheckText := FmtMessage(CustomMessage('ResFail'), [FailIds, ReportFile]);
  end
  else if Warns > 0 then
    CheckText := FmtMessage(CustomMessage('ResWarn'), [WarnIds, ReportFile])
  else
    CheckText := CustomMessage('ResOk');
end;

procedure RunCheck(Comps: String);
var
  Rc: Integer;
  Report, Summary, Params: String;
begin
  Report := ExpandConstant('{app}\logs\install_check.txt');
  Summary := ExpandConstant('{app}\logs\install_check.summary');
  ForceDirectories(ExpandConstant('{app}\logs'));
  DeleteFile(Summary);
  Params := '-m ripster.install_check --base "' + ExpandConstant('{app}') + '" --report "' + Report + '" --summary "' + Summary + '"';
  if Comps <> '' then Params := Params + ' --components ' + Comps;
  { Как обычный пользователь, а не администратор: так проверка видит ровно то, что увидит само приложение при запуске }
  if not ExecAsOriginalUser(Py(), Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Rc) then
    Rc := -1;
  Log('selfcheck exit code ' + IntToStr(Rc));
  BuildCheckText(Rc, Summary, Report);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Comps, Rest, Key: String;
  Pg: TOutputProgressWizardPage;
begin
  if CurStep <> ssPostInstall then exit;
  Comps := SelectedComponents();
  Pg := CreateOutputProgressPage(CustomMessage('RunTitle'), CustomMessage('RunSub'));
  Pg.Show;
  try
    Rest := Comps;
    while Rest <> '' do
    begin
      Key := NextKey(Rest);
      if Key <> '' then
      begin
        Pg.SetText(FmtMessage(CustomMessage('StInstalling'), [KeyTitle(Key)]), '');
        Pg.SetProgress(0, 0);
        InstallOne(Key);
      end;
    end;
    Pg.SetText(CustomMessage('StChecking'), '');
    RunCheck(Comps);
  finally
    Pg.Hide;
  end;
  Log('install check: ' + CheckText);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (CheckText <> '') then
  begin
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + CheckText;
    if CheckBad then
      WizardForm.FinishedHeadingLabel.Font.Color := clRed;
  end;
end;
