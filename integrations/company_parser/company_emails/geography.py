from __future__ import annotations

import json
from pathlib import Path

from .catalog import ROOT, load_cities


def yandex_cities(path: Path | None = None) -> list[dict]:
    path = path or ROOT / "data" / "yandex-cities.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    cities = data["cities"] if isinstance(data, dict) else data
    return [c for c in cities if c.get("country_code", "ru").lower() == "ru"]


def select_yandex_cities(cities: list[dict], names: list[str]) -> list[dict]:
    if not names:
        return cities
    legacy = {c["code"]: c["name"] for c in load_cities()}
    selected = []
    for value in names:
        name = legacy.get(value.casefold(), value)
        matches = [c for c in cities if name.casefold() in (c["name"].casefold(), c["code"].casefold())]
        if not matches:
            # Yandex accepts place names directly, including small towns absent from the bundled gazetteer.
            import hashlib
            matches = [{"name": value, "code": "manual-" + hashlib.sha256(value.casefold().encode()).hexdigest()[:12], "country_code": "ru", "domain": "ru"}]
        selected.extend(c for c in matches if c not in selected)
    return selected
