#!/usr/bin/env python3
"""Create a local unsigned launcher. No Apple Developer account required."""
import plistlib
import subprocess
from pathlib import Path
root=Path(__file__).resolve().parent.parent
apps=Path.home()/'Applications'
apps.mkdir(exist_ok=True)
app=apps/'Quickly.app'
source='''on run
set choices to {"Открыть Quickly", "Остановить Quickly", "Резервная копия", "Восстановить копию", "Данные входа"}
set selected to choose from list choices with prompt "Quickly — локальная почта" default items {"Открыть Quickly"} OK button name "Выполнить" cancel button name "Закрыть"
if selected is false then return
set action to item 1 of selected
try
if action is "Открыть Quickly" then
  do shell script RUNNER & " open >>" & LOGFILE & " 2>&1"
else if action is "Остановить Quickly" then
  do shell script RUNNER & " stop >>" & LOGFILE & " 2>&1"
else if action is "Восстановить копию" then
  set backupFile to POSIX path of (choose file with prompt "Выберите PostgreSQL-копию Quickly (.dump)")
  display dialog "Восстановить эту копию в Quickly? Текущая база будет сохранена отдельно. Понадобится тот же внешний ключ шифрования, с которым создана копия." buttons {"Отмена", "Восстановить"} default button "Отмена" cancel button "Отмена"
  set restoreResult to do shell script RESTORER & " " & quoted form of backupFile & " --replace-local-database >>" & LOGFILE & " 2>&1"
  display dialog "Копия восстановлена. Предыдущая база сохранена; подробности в журнале." buttons {"OK"}
else if action is "Данные входа" then
  set adminPassword to do shell script "/usr/bin/security find-generic-password -a admin -s local.quickly.admin -w"
  set choice to button returned of (display dialog "Адрес: http://localhost:8765" & return & "Логин: admin" & return & "Пароль: " & adminPassword & return & return & "При смене пароля в Quickly обновите запись local.quickly.admin в Связке ключей." buttons {"Закрыть", "Копировать пароль"} default button "Закрыть")
  if choice is "Копировать пароль" then set the clipboard to adminPassword
else
  set resultPath to do shell script RUNNER & " backup"
  display dialog "Резервная копия создана: " & resultPath buttons {"OK"}
end if
on error errorMessage
 display dialog "Не удалось выполнить действие: " & errorMessage & ". Журнал: ~/Library/Application Support/Quickly/logs/launcher.log" buttons {"OK"}
end try
end run'''
# AppleScript string literals contain quoted POSIX paths produced at runtime.
runner=str(root/'macos/quickly').replace('\\','\\\\').replace('"','\\"')
log=str(Path.home()/'Library/Application Support/Quickly/logs/launcher.log').replace('"','\\"')
restorer=str(root/'macos/restore.py').replace('\\','\\\\').replace(chr(34),'\\'+chr(34))
source=source.replace('RESTORER','quoted form of '+chr(34)+restorer+chr(34))
source=source.replace('RUNNER','quoted form of "'+runner+'"').replace('LOGFILE','quoted form of "'+log+'"')
subprocess.run(['osacompile','-o',str(app),'-'],input=source,text=True,check=True)
info=app/'Contents/Info.plist'
with info.open('rb') as f:config=plistlib.load(f)
config.update(CFBundleIdentifier='local.quickly.launcher',CFBundleName='Quickly',LSUIElement=False)
with info.open('wb') as f:plistlib.dump(config,f)
subprocess.run(['codesign','--force','--deep','--sign','-',str(app)],check=True)
print(app)
