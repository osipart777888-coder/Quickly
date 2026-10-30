from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "vendor" / "parser_2gis" / "data"
PRESETS = {
    "legal": {"name": "Юридические компании", "rubrics": ["344", "110521"]},
}


def load_cities(path: Path | None = None) -> list[dict]:
    # Upstream puts Abkhazia under country_code=ru; omit that catalogue project.
    cities = json.loads((path or DATA / "cities.json").read_text(encoding="utf-8"))
    return [c for c in cities if c["country_code"] == "ru" and c["code"] != "abkhazia"]


def load_rubrics() -> dict:
    return json.loads((DATA / "rubrics.json").read_text(encoding="utf-8"))


def make_tasks(cities: list[dict], rubric_ids: list[str], queries: list[str], limit: int) -> list[dict]:
    rubrics = load_rubrics()
    selectors = []
    for code in dict.fromkeys(rubric_ids):
        if code not in rubrics or rubrics[code].get("children"):
            raise ValueError(f"Нужна конечная рубрика 2ГИС: {code}. Используйте команду rubrics.")
        selectors.append((code, rubrics[code]["label"]))
    selectors.extend((None, q.strip()) for q in dict.fromkeys(queries) if q.strip())
    tasks = []
    for city in cities:
        for rubric, query in selectors:
            url = f'https://2gis.{city["domain"]}/{city["code"]}/search/{quote(query, safe="")}'
            if rubric:
                url += f"/rubricId/{rubric}"
            key = json.dumps([city["code"], rubric, query, limit], ensure_ascii=False)
            tasks.append({"id": hashlib.sha256(key.encode()).hexdigest()[:20],
                          "source": "2gis",
                          "country": city.get('country', 'Россия'), "country_code": city.get('country_code', 'ru'),
                          "domain": city['domain'],
                          "city": city["name"], "city_code": city["code"],
                          "query": query, "rubric": rubric, "url": url, "limit": limit})
    return tasks
