"""A dedicated entry point for the verified Yandex workflow."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .__main__ import main as cli
from .catalog import ROOT
from .checkpoints import atomic_json
from .geography import yandex_cities
from .sources import LOCAL

LAST_RUN = LOCAL / "yandex-last-run.json"


def open_results(workdir):
    output = workdir / "export"
    if output.exists():
        print(f"Результаты: {output.resolve()}")
        if sys.platform == "darwin":
            subprocess.run(["open", str(output.resolve())], check=False)
    else:
        print("В этой папке ещё нет выгрузки.")


def last_run():
    if not LAST_RUN.exists():
        raise ValueError("Сначала запустите новый сбор — сохранённой очереди пока нет.")
    data = json.loads(LAST_RUN.read_text(encoding="utf-8"))
    workdir = Path(data["workdir"])
    if not (workdir / "plan.json").exists():
        raise ValueError(f"Не найден сохранённый план: {workdir}")
    return workdir


def main():
    print("\nЯндекс Карты — сбор опубликованных контактов компаний")
    print("1 — Новый сбор\n2 — Продолжить последний сбор\n3 — Дополнить почты с сайтов")
    print("4 — Открыть последние результаты\n5 — Открыть браузер для проверки доступа\n0 — Выход")
    action = input("Действие [1]: ").strip() or "1"
    if action == "0":
        return 0
    if action == "5":
        return cli(["configure", "--source", "yandex"])
    if action not in ("1", "2", "3", "4"):
        raise ValueError("Выберите действие от 0 до 5")
    if action == "1":
        queries = [q.strip() for q in (input("Сфера [Юридические компании]; несколько — через точку с запятой: ").strip()
                                      or "Юридические компании").split(";") if q.strip()]
        print(f"В каталоге {len(yandex_cities())} населённых пунктов России: от 15 тыс. жителей и административные центры.")
        print("Маленький город можно вписать вручную. При совпадении названий укажите также регион.")
        cities = [c.strip() for c in input("Города через запятую [Enter — весь каталог России]: ").split(",") if c.strip()]
        limit = int(input("Максимум карточек на город и запрос [100]: ").strip() or "100")
        if limit <= 0 or not queries:
            raise ValueError("Нужны поисковый запрос и положительный лимит")
        workdir = ROOT / "runs" / ("yandex-" + datetime.now().strftime("%Y-%m-%d-%H%M%S-%f"))
        command = ["collect", "--source", "yandex", "--workdir", str(workdir), "--enrich", "--max-records", str(limit)]
        for query in queries:
            command += ["--query", query]
        for city in cities:
            command += ["--city", city]
        # Save before starting: Ctrl+C or a closed terminal must not lose the folder.
        atomic_json(LAST_RUN, {"workdir": str(workdir), "queries": queries, "cities": cities, "limit": limit})
    else:
        workdir = last_run()
        if action == "4":
            open_results(workdir)
            return 0
        command = (["collect", "--resume-plan", "--retry-failed", "--enrich"] if action == "2" else ["enrich"])
        command += ["--source", "yandex", "--workdir", str(workdir)]
    print(f"Папка сбора: {workdir}")
    print("Ctrl+C — остановить и выгрузить полученные контакты. Действие 2 продолжит сохранённую очередь.")
    code = cli(command)
    print("Сбор завершён. Проверьте листы «Задания» и «Проверка сайтов»." if code == 0
          else "Сбор остановлен или завершён частично. Причины указаны на листе «Задания».")
    open_results(workdir)
    return code


if __name__ == "__main__":
    from .signals import install_shutdown_handlers
    install_shutdown_handlers()
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, KeyboardInterrupt, EOFError) as exc:
        print(f"Остановлено: {exc}")
        raise SystemExit(2)
