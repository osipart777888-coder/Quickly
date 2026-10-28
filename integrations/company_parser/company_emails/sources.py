from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, parse_qsl

from .catalog import ROOT, make_tasks

SOURCES = {
    "2gis": {"name": "2ГИС", "home": "https://2gis.ru", "domains": ("2gis.ru",), "mode": "maps"},
    "yandex": {"name": "Яндекс Карты", "home": "https://yandex.ru/maps/", "domains": ("yandex.ru", "yandex.com"), "mode": "maps"},
    "spark": {"name": "СПАРК", "home": "https://spark-interfax.ru/", "domains": ("spark-interfax.ru", "spark-interfax.com"), "mode": "selection"},
    "rusprofile": {"name": "Rusprofile", "home": "https://www.rusprofile.ru/", "domains": ("rusprofile.ru",), "mode": "selection"},
}
LOCAL = ROOT / ".local"


def source_url(source: str, url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username or parts.password
            or not any(host == d or host.endswith("." + d) for d in SOURCES[source]["domains"])):
        raise ValueError(f'Нужна HTTPS-ссылка сервиса {SOURCES[source]["name"]}')
    if source in ("spark", "rusprofile"):
        params = parse_qsl(parts.query) + parse_qsl(parts.fragment.partition("?")[2])
        if any(key.casefold() in ("password", "passwd", "access_token", "id_token", "token", "authorization", "otp", "twofatoken") for key, _ in params):
            raise ValueError(f"Нужна ссылка выборки {SOURCES[source]['name']}, содержащая фильтры, а не адрес с данными авторизации")
    return parts.geturl()


def config_file(source: str) -> Path:
    return LOCAL / "sources" / f"{source}.json"


def load_config(source: str) -> dict:
    path = config_file(source)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_config(source: str, urls: list[str], name: str, options=None):
    path = config_file(source)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"source": source, "urls": [source_url(source, u) for u in urls], "selection_name": name}
    if options is not None:
        payload["options"] = options
    from .checkpoints import atomic_json
    atomic_json(path, payload)
    path.chmod(0o600)


def build_tasks(source: str, cities: list[dict], rubric_ids: list[str], queries: list[str], limit: int,
                urls: list[str] | None = None, selection_name: str | None = None) -> list[dict]:
    if source == "2gis":
        return make_tasks(cities, rubric_ids, queries, limit)
    if source == "yandex":
        if rubric_ids:
            raise ValueError("Для Яндекс Карт используйте поисковые запросы --query, а не коды рубрик 2ГИС")
        tasks = []
        for city in cities:
            for query in dict.fromkeys(queries or ["Юридические компании", "Услуги адвоката"]):
                url = "https://yandex.ru/maps/?text=" + quote(query + " " + city["name"], safe="")
                if city.get("latitude") is not None and city.get("longitude") is not None:
                    point = f'{city["longitude"]},{city["latitude"]}'
                    url += "&" + urlencode({"ll": point, "sll": point, "z": 12, "lang": "ru_RU"})
                tasks.append(new_task(source, url, query, limit, city["name"], city["code"]))
        return tasks
    config = load_config(source)
    urls = urls or config.get("urls", [])
    if not urls:
        raise ValueError(f'Сначала настройте {SOURCES[source]["name"]}: команда configure --source {source} '
                         'или пункт «Вход и выборка» в меню запуска. Можно передать --source-url или --urls-file.')
    if queries or rubric_ids:
        raise ValueError("Для СПАРК/Rusprofile сферу, ОКВЭД и регион задайте в выборке на сайте, затем сохраните её через configure")
    label = selection_name or config.get("selection_name") or "Выборка пользователя"
    tasks = [new_task(source, source_url(source, url), label, limit) for url in dict.fromkeys(urls)]
    if source in ("spark", "rusprofile"):
        guard = config.get("options", {}).get("selection_fingerprint", [])
        for task in tasks:
            if guard and task["url"] in config.get("urls", []):
                task["selection_fingerprint"] = guard
                task["id"] = hashlib.sha256(json.dumps([task["id"], guard], ensure_ascii=False).encode()).hexdigest()[:20]
    return tasks


def new_task(source: str, url: str, query: str, limit: int, city="Россия", city_code="") -> dict:
    key = json.dumps([source, url, query, limit], ensure_ascii=False)
    return {"id": hashlib.sha256(key.encode()).hexdigest()[:20], "source": source, "url": url,
            "query": query, "limit": limit, "city": city, "city_code": city_code, "rubric": None}
