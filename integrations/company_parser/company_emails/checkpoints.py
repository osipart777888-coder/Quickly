from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def recover_jsonl(path: Path) -> dict[str, dict]:
    """Keep complete rows after an interrupted write; don't discard prior rows."""
    records = {}
    if not path.exists():
        return records
    complete = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            identity = str(item.get("external_id") or item.get("id", "")).split("_")[0]
            if identity:
                records[identity] = item
        except (ValueError, AttributeError):
            continue
    # A trailing partial row would otherwise be joined to the next append.
    complete = [json.dumps(item, ensure_ascii=False) for item in records.values()]
    temporary = path.with_suffix(".recovered")
    temporary.write_text("\n".join(complete) + ("\n" if complete else ""), encoding="utf-8")
    temporary.replace(path)
    return records
