"""Freeze installed runtime dependencies for the macOS/Linux local build.

Run inside a tested venv after changing requirements.txt. This does not
resolve or download packages; it records the installed, tested closure.
"""
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path
from packaging.requirements import Requirement
from packaging.markers import default_environment
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parent.parent
pending = []
for line in (root / 'requirements.txt').read_text().splitlines():
    line = line.split('#', 1)[0].strip()
    if line:
        pending.append(Requirement(line))
seen = {}
extras_seen = {}
while pending:
    req = pending.pop()
    name = canonicalize_name(req.name)
    extras = set(req.extras)
    if name in seen and extras <= extras_seen[name]:
        continue
    dist = distribution(name)
    seen[name] = dist.version
    extras_seen.setdefault(name, set()).update(extras)
    for raw in dist.requires or []:
        dependency = Requirement(raw)
        enabled = dependency.marker is None
        for system in ('Linux', 'Darwin'):
            for extra in {''} | extras_seen[name]:
                env = default_environment()
                env.update(platform_system=system, sys_platform='linux' if system == 'Linux' else 'darwin', extra=extra)
                if dependency.marker is None or dependency.marker.evaluate(env):
                    enabled = True
        if enabled:
            pending.append(dependency)
(root / 'requirements.lock').write_text('# Tested runtime dependency closure, Python 3.12, macOS/Linux.\n' + ''.join(f'{name}=={seen[name]}\n' for name in sorted(seen)))
print(f'Locked {len(seen)} runtime packages')
