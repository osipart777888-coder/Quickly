from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from .catalog import PRESETS, load_cities, load_rubrics
from .exporting import export_results
from .runner import collect, enrich, run_lock
from .storage import Store
from .sources import SOURCES, build_tasks, load_config
from .geography import yandex_cities, select_yandex_cities
from .checkpoints import atomic_json
from .adapters import NeedsAccess


def positive_int(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("Нужно число больше нуля")
    return n


def saved_plan(args):
    if any((args.city, args.query, args.rubric, args.cities_file, args.source_url,
            args.urls_file, args.selection_name, args.sample)):
        raise ValueError("При --resume-plan не задавайте новые города/сферы: используется сохранённый план")
    tasks = json.loads((args.workdir / "plan.json").read_text(encoding="utf-8"))
    if not isinstance(tasks, list) or not tasks or any(
            not isinstance(t, dict) or t.get("source", "2gis") != args.source
            or not all(key in t for key in ("id", "url", "query", "city", "city_code", "limit"))
            for t in tasks):
        raise ValueError("Сохранённый план повреждён или его источник не совпадает с выбранным")
    return tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Публичные email компаний по сферам деятельности и городам России")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="Показать источники и состояние настройки")
    setup = sub.add_parser("configure", help="Войти в источник и сохранить выборку")
    setup.add_argument("--source", choices=SOURCES, required=True)
    setup.add_argument("--source-url")
    for name in ("cities", "rubrics"):
        command = sub.add_parser(name, help="Поиск в каталоге городов" if name == "cities" else "Поиск рубрик деятельности")
        command.add_argument("search", nargs="?", default="")
        if name == "cities":
            command.add_argument("--source", choices=("2gis", "yandex"), default="2gis")
    plan = sub.add_parser("plan", help="Показать задания без запуска браузера")
    run = sub.add_parser("collect", help="Собрать карточки компаний")
    for command in (plan, run):
        command.add_argument("--source", choices=SOURCES, default="2gis")
        command.add_argument("--source-url", action="append", default=[], help="Адрес выборки или карточки СПАРК/Rusprofile")
        command.add_argument("--urls-file", type=Path, help="Текстовый файл со ссылками СПАРК/Rusprofile, по одной в строке")
        command.add_argument("--selection-name", help="Название уже отфильтрованной выборки")
        command.add_argument("--preset", choices=PRESETS, default="legal")
        command.add_argument("--rubric", action="append", default=[], help="Код конечной рубрики; можно повторять")
        command.add_argument("--query", action="append", default=[], help="Свободный поисковый запрос; можно повторять")
        command.add_argument("--city", action="append", default=[], help="Название или код города; по умолчанию весь каталог выбранного источника")
        command.add_argument("--cities-file", type=Path, help="Своя копия каталога городов в формате upstream")
        command.add_argument("--max-records", type=positive_int, default=1000, help="Лимит на одну пару город × рубрика")
        command.add_argument("--sample", action="store_true", help="Тула, Рязань, Калуга; одна рубрика; по 3 карточки")
        command.add_argument("--workdir", type=Path)
    run.add_argument("--timeout", type=positive_int, default=900, help="Лимит секунд на одно задание")
    run.add_argument("--resume-plan", action="store_true", help="Продолжить точный сохранённый план из рабочей папки")
    run.add_argument("--delay", type=float, default=1, help="Задержка между карточками, минимум 0.5 с")
    run.add_argument("--visible", action="store_true", help="Показывать окно Chrome")
    run.add_argument("--retry-failed", action="store_true", help="Повторить задания с ошибками и пустой выдачей")
    run.add_argument("--enrich", action="store_true", help="После карточек искать email на сайтах")
    run.add_argument("--max-pages", type=positive_int, default=4)
    run.add_argument("--site-limit", type=int, default=0, help="Лимит сайтов для обогащения; 0 — все")
    extra = sub.add_parser("enrich", help="Поиск дополнительных почт на уже собранных сайтах")
    extra.add_argument("--source", choices=SOURCES, default="2gis")
    extra.add_argument("--workdir", type=Path)
    extra.add_argument("--max-pages", type=positive_int, default=4)
    extra.add_argument("--site-limit", type=int, default=0)
    extra.add_argument("--refresh", action="store_true")
    for name in ("export", "status"):
        command = sub.add_parser(name, help="Выгрузить Excel/CSV" if name == "export" else "Показать прогресс")
        command.add_argument("--source", choices=SOURCES, default="2gis")
        command.add_argument("--workdir", type=Path)
    importer = sub.add_parser("import", help="Загрузить официальную выгрузку СПАРК/Rusprofile (CSV/XLSX)")
    importer.add_argument("--source", choices=("spark", "rusprofile"), required=True)
    importer.add_argument("--file", type=Path, required=True)
    importer.add_argument("--workdir", type=Path)
    importer.add_argument("--selection-name", default="Импортированная выборка")
    importer.add_argument("--enrich", action="store_true")
    importer.add_argument("--max-pages", type=positive_int, default=4)
    importer.add_argument("--site-limit", type=int, default=0)
    args = parser.parse_args(argv)
    if hasattr(args, "workdir") and args.workdir is None:
        args.workdir = Path("runs/legal-russia" if args.source == "2gis" else f"runs/legal-{args.source}")
    if args.command == "sources":
        for key, info in SOURCES.items():
            state = "прямой сбор" if info["mode"] == "maps" else "выборка сохранена" if load_config(key).get("urls") else "нужны вход и выборка"
            print(f'{key:12} {info["name"]:18} {state}')
        return 0
    if args.command == "configure":
        from .browser import configure
        try:
            return configure(args.source, args.source_url) or 0
        except (ValueError, OSError, NeedsAccess) as exc:
            print(f"Ошибка настройки: {exc}", file=sys.stderr)
            return 2
    if args.command == "cities":
        for city in (yandex_cities() if args.source == "yandex" else load_cities()):
            if args.search.casefold() in (city["name"] + " " + city["code"]).casefold():
                print(f'{city["code"]:20} {city["name"]}')
        return 0
    if args.command == "rubrics":
        for code, rubric in load_rubrics().items():
            if rubric.get("label") and not rubric.get("children") and args.search.casefold() in rubric["label"].casefold():
                print(f'{code:8} {rubric["label"]}')
        return 0
    tasks = None
    try:
        if args.command in ("plan", "collect"):
            if getattr(args, "resume_plan", False):
                tasks = saved_plan(args)
            else:
                cities = yandex_cities(args.cities_file) if args.source == "yandex" else load_cities(args.cities_file)
                if args.source in ("spark", "rusprofile") and args.city:
                    raise ValueError("Для СПАРК/Rusprofile регион выбирается на сайте. --city применяется к картам.")
                if args.sample and not args.city and SOURCES[args.source]["mode"] == "maps":
                    args.city = ["tula", "ryazan", "kaluga"]
                if args.city:
                    if args.source == "yandex":
                        cities = select_yandex_cities(cities, args.city)
                    else:
                        selected = []
                        for name in args.city:
                            matches = [c for c in cities if name.casefold() in (c["name"].casefold(), c["code"].casefold())]
                            if not matches:
                                raise ValueError(f"Город не найден: {name}. Посмотрите список командой cities.")
                            selected.extend(c for c in matches if c not in selected)
                        cities = selected
                rubrics = args.rubric or ([] if args.query or args.source != "2gis" else PRESETS[args.preset]["rubrics"])
                queries = args.query
                if args.source == "yandex" and not queries:
                    queries = ["Юридические компании", "Услуги адвоката"]
                if args.sample:
                    args.max_records = min(args.max_records, 3)
                    if not args.rubric and not args.query:
                        rubrics = rubrics[:1]
                        queries = queries[:1]
                urls = args.source_url[:]
                if args.urls_file:
                    urls.extend(line.strip() for line in args.urls_file.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#"))
                tasks = build_tasks(args.source, cities, rubrics, queries, args.max_records, urls, args.selection_name)
            if not tasks:
                raise ValueError("Не выбраны города или сферы")
            print(f'Источник: {SOURCES[args.source]["name"]}; заданий: {len(tasks)}; лимит карточек на задание: {max(t["limit"] for t in tasks)}')
            if SOURCES[args.source]["mode"] == "maps":
                label = "Проектов 2ГИС" if args.source == "2gis" else "Населённых пунктов"
                print(f"{label}: {len({t['city_code'] for t in tasks})}.")
                if args.source == "yandex":
                    print("Поиск по географии справочника GeoNames; дополнительные города можно указать вручную.")
                else:
                    print("Каталог 2ГИС — снимок Parser2GIS (2024).")
            else:
                print("Используются фактические фильтры сохранённой выборки на сайте.")
            if args.command == "plan":
                for task in tasks[:8]:
                    print(f'{task["city"]} · {task["query"]}: {task["url"]}')
                if len(tasks) > 8:
                    print(f"… ещё {len(tasks) - 8} заданий")
                return 0
            if not math.isfinite(args.delay) or args.delay < .5 or args.site_limit < 0:
                raise ValueError("Задержка должна быть не меньше 0.5 с, лимит сайтов — неотрицательным")
        elif args.command != "import" and not (args.workdir / "contacts.sqlite").exists():
            raise ValueError("В этой папке пока нет базы. Сначала выполните collect.")
        if getattr(args, "site_limit", 0) < 0:
            raise ValueError("Лимит сайтов должен быть неотрицательным")
        if args.command == "status":
            store = Store(args.workdir)
            try:
                print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
            finally:
                store.close()
            return 0
        with run_lock(args.workdir):
            store = Store(args.workdir)
            try:
                success = True
                try:
                    if args.command == "collect":
                        atomic_json(args.workdir / "plan.json", tasks)
                        if args.source in ("spark", "rusprofile"):
                            # Start/reuse Chrome in the parent so worker cleanup never kills its session.
                            from .spark_browser import SparkBrowser
                            from .rusprofile_browser import RusprofileBrowser
                            browser_factory = SparkBrowser if args.source == "spark" else RusprofileBrowser
                            from .sources import LOCAL
                            store.add_tasks(tasks)
                            pending = [task for task in tasks if store.db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0] not in ("done", "limited")]
                            if pending:
                                with run_lock(LOCAL / "profiles" / args.source):
                                    with browser_factory():
                                        pass
                        visited_sites = set()
                        def after_task(task):
                            if args.site_limit and len(visited_sites) >= args.site_limit:
                                return
                            remaining = args.site_limit - len(visited_sites) if args.site_limit else 0
                            enrich(store, args.max_pages, remaining, task_id=task["id"], skip_urls=visited_sites)
                            export_results(store, args.workdir / "export")
                        success = collect(store, tasks, args.timeout, args.visible, args.delay, args.retry_failed,
                                          on_task=after_task if args.enrich else None)
                    elif args.command == "enrich":
                        enrich(store, args.max_pages, args.site_limit, args.refresh)
                    elif args.command == "import":
                        from .imports import import_file
                        import_file(store, args.source, args.file, args.selection_name)
                        if args.enrich:
                            enrich(store, args.max_pages, args.site_limit)
                except KeyboardInterrupt:
                    print("Остановлено. Сохраняю уже полученные контакты.")
                    success = False
                except NeedsAccess as exc:
                    if tasks:
                        store.add_tasks(tasks)
                        for task in tasks:
                            row = store.db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()
                            if row[0] not in ("done", "limited"):
                                store.mark(task["id"], "needs_auth", store.task_count(task["id"]), str(exc))
                    print(str(exc))
                    success = False
                finally:
                    summary = export_results(store, args.workdir / "export")
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
                    print(f'Файлы: {(args.workdir / "export").resolve()}')
                has_failures = any(summary["tasks"].get(s) for s in ("partial", "error", "timeout", "interrupted", "running", "pending", "needs_auth"))
                return 0 if success and not has_failures else 2
            finally:
                store.close()
    except (ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    from .signals import install_shutdown_handlers
    install_shutdown_handlers()
    raise SystemExit(main())
