"""2GIS entry point with sector selection and exact-plan continuation."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .__main__ import main as cli
from .catalog import ROOT, load_cities, load_rubrics
from .checkpoints import atomic_json
from .sources import LOCAL
from .yandex_menu import open_results

LAST_RUN = LOCAL / "2gis-last-run.json"


def sectors(value):
    catalog = load_rubrics()
    names = {info["label"].casefold(): code for code, info in catalog.items() if info.get("label") and not info.get("children")}
    rubrics, queries = [], []
    for token in value.split(";"):
        token = token.strip()
        if not token:
            continue
        code = token if token.isdigit() else names.get(token.casefold())
        if code:
            if code not in catalog or catalog[code].get("children"):
                raise ValueError(f"Нет конечной рубрики 2ГИС {code}. Используйте название сферы или команду rubrics.")
            if code not in rubrics:
                rubrics.append(code)
        elif token not in queries:
            queries.append(token)
    if not rubrics and not queries:
        raise ValueError("Укажите хотя бы одну сферу")
    return rubrics, queries


def last_run():
    if not LAST_RUN.exists():
        raise ValueError("Сначала запустите новый сбор 2ГИС — сохранённой очереди пока нет.")
    folder = Path(json.loads(LAST_RUN.read_text(encoding="utf-8"))["workdir"])
    if not (folder / "plan.json").exists():
        raise ValueError(f"Не найден сохранённый план: {folder}")
    return folder


def main():
    print("\n2ГИС — сбор опубликованных контактов компаний")
    print("1 — Новый сбор\n2 — Продолжить последний сбор\n3 — Дополнить почты с сайтов")
    print("4 — Открыть последние результаты\n5 — Открыть браузер для проверки доступа\n0 — Выход")
    action = input("Действие [1]: ").strip() or "1"
    if action == "0":
        return 0
    if action == "5":
        return cli(["configure", "--source", "2gis"])
    if action not in ("1", "2", "3", "4"):
        raise ValueError("Выберите действие от 0 до 5")
    if action == "1":
        print("Сфера: точное название/код рубрики или свободный поисковый запрос. Несколько сфер — через точку с запятой.")
        value = input("Сфера [Юридические услуги]: ").strip() or "Юридические услуги"
        rubrics, queries = sectors(value)
        for code in rubrics:
            print(f'Рубрика {code}: {load_rubrics()[code]["label"]}')
        for query in queries:
            print(f"Поисковый запрос: {query}")
        catalog = load_cities()
        print(f"В каталоге {len(catalog)} проектов России. Проект 2ГИС может включать соседние города и пригороды.")
        cities = [c.strip() for c in input("Города/коды через запятую [Enter — весь каталог России]: ").split(",") if c.strip()]
        available = {v.casefold() for city in catalog for v in (city["name"], city["code"])}
        for city in cities:
            if city.casefold() not in available:
                raise ValueError(f"Нет проекта 2ГИС: {city}. Список: .venv/bin/python -m company_emails cities --source 2gis")
        limit = int(input("Максимум карточек на город и сферу [100]: ").strip() or "100")
        if limit <= 0:
            raise ValueError("Нужен положительный лимит")
        folder = ROOT / "runs" / ("2gis-" + datetime.now().strftime("%Y-%m-%d-%H%M%S-%f"))
        command = ["collect", "--source", "2gis", "--workdir", str(folder), "--enrich", "--max-records", str(limit)]
        for code in rubrics:
            command += ["--rubric", code]
        for query in queries:
            command += ["--query", query]
        for city in cities:
            command += ["--city", city]
        atomic_json(LAST_RUN, {"workdir": str(folder), "rubrics": rubrics, "queries": queries, "cities": cities, "limit": limit})
    else:
        folder = last_run()
        if action == "4":
            open_results(folder)
            return 0
        command = ["collect", "--resume-plan", "--retry-failed", "--enrich"] if action == "2" else ["enrich"]
        command += ["--source", "2gis", "--workdir", str(folder)]
    print(f"Папка сбора: {folder}")
    print("Ctrl+C — остановить и выгрузить полученные контакты. Действие 2 продолжит сохранённую очередь карточек и страниц.")
    code = cli(command)
    print("Сбор завершён. Проверьте листы «Задания» и «Проверка сайтов»." if code == 0
          else "Сбор остановлен или завершён частично. Причины указаны на листе «Задания».")
    open_results(folder)
    return code


if __name__ == "__main__":
    from .signals import install_shutdown_handlers
    install_shutdown_handlers()
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, KeyboardInterrupt, EOFError) as exc:
        print(f"Остановлено: {exc}")
        raise SystemExit(2)
