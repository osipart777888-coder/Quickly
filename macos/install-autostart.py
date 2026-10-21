#!/usr/bin/env python3
"""Restore the isolated local stack at the next macOS login."""
from pathlib import Path
import plistlib, shutil, re
root=Path(__file__).resolve().parent.parent
folder=Path.home()/'Library/LaunchAgents';folder.mkdir(exist_ok=True)
logs=Path.home()/'Library/Application Support/Quickly/logs';logs.mkdir(parents=True,exist_ok=True)
service=Path.home()/'Library/Application Support/Quickly/service'
(service/'macos').mkdir(parents=True,exist_ok=True,mode=0o700)
service.chmod(0o700)
for name in ['quickly','restore.py']:
    shutil.copy2(root/'macos'/name,service/'macos'/name)
    (service/'macos'/name).chmod(0o700)
# Startup uses the installed image and does not read source files in Documents.
compose=(root/'compose.macos.yml').read_text()
compose,count=re.subn(r'    build:\n(?:      [^\n]*\n)+','',compose)
assert count == 1
(service/'compose.macos.yml').write_text(compose)
(service/'compose.macos.yml').chmod(0o600)
p=folder/'local.quickly.start.plist'
config={
 'Label':'local.quickly.start',
 'ProgramArguments':['/bin/bash',str(service/'macos/quickly'),'start'],
 'RunAtLoad':True,
 'StandardOutPath':str(logs/'autostart.log'),
 'StandardErrorPath':str(logs/'autostart.log'),
 'ProcessType':'Background',
}
with p.open('wb') as out:plistlib.dump(config,out)
p.chmod(0o600)
print(p)
print('Startup runtime installed outside protected Documents. Reload the LaunchAgent to apply.')
