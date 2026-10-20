"""Merge reviewed English<TAB>Russian UI strings from stdin."""
import json
from pathlib import Path
import sys
path = Path(__file__).resolve().parent.parent / 'src/i18n/ru.json'
data = json.loads(path.read_text())
for line in sys.stdin.read().splitlines():
    if not line.strip():
        continue
    if '\t' not in line:
        raise ValueError('Translation line needs a tab: ' + line[:80])
    english, russian = line.split('\t', 1)
    data[english] = russian
path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
print(len(data), 'reviewed translations')
