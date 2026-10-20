#!/usr/bin/env python3
"""Restore the isolated local stack at the next macOS login."""
from pathlib import Path
import plistlib, subprocess
root=Path(__file__).resolve().parent.parent
folder=Path.home()/'Library/LaunchAgents';folder.mkdir(exist_ok=True)
logs=Path.home()/'Library/Application Support/Quickly/logs';logs.mkdir(parents=True,exist_ok=True)
p=folder/'local.quickly.start.plist'
config={
 'Label':'local.quickly.start',
 'ProgramArguments':['/bin/bash',str(root/'macos/quickly'),'start'],
 'RunAtLoad':True,
 'StandardOutPath':str(logs/'autostart.log'),
 'StandardErrorPath':str(logs/'autostart.log'),
 'ProcessType':'Background',
}
with p.open('wb') as out:plistlib.dump(config,out)
p.chmod(0o600)
print(p)
print('Configured for next login. Current services are managed separately.')
