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
set action to button returned of (display dialog "Quickly — локальная почта" buttons {"Остановить", "Резервная копия", "Открыть"} default button "Открыть")
try
if action is "Открыть" then
  do shell script RUNNER & " open >>" & LOGFILE & " 2>&1"
else if action is "Остановить" then
  do shell script RUNNER & " stop >>" & LOGFILE & " 2>&1"
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
source=source.replace('RUNNER','quoted form of "'+runner+'"').replace('LOGFILE','quoted form of "'+log+'"')
subprocess.run(['osacompile','-o',str(app),'-'],input=source,text=True,check=True)
info=app/'Contents/Info.plist'
with info.open('rb') as f:config=plistlib.load(f)
config.update(CFBundleIdentifier='local.quickly.launcher',CFBundleName='Quickly',LSUIElement=False)
with info.open('wb') as f:plistlib.dump(config,f)
subprocess.run(['codesign','--force','--deep','--sign','-',str(app)],check=True)
print(app)
