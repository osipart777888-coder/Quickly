from __future__ import annotations

import json
import shlex
from datetime import datetime
from pathlib import Path

from .__main__ import main as cli
from .catalog import ROOT
from .checkpoints import atomic_json
from .sources import LOCAL, load_config
from .yandex_menu import open_results

LAST_RUN = LOCAL / "spark-last-run.json"


def last_run():
    if not LAST_RUN.exists():
        raise ValueError("Сначала запустите новый сбор СПАРК")
    folder = Path(json.loads(LAST_RUN.read_text())["workdir"])
    if not (folder / "contacts.sqlite").exists():
        raise ValueError("Не найдена база последнего сбора")
    return folder


def main():
    print("\nСПАРК — выборки компаний и опубликованные контакты")
    if not load_config("spark").get("options", {}).get("access_check", {}).get("verified"):
        print("Доступ к контактам пока не подтверждён. После входа выполните проверку (действие 4).")
    print("1 — Вход и выборка\n2 — Новый сбор сохранённой выборки\n3 — Продолжить последний сбор")
    print("4 — Проверить доступ к контактам\n5 — Открыть результаты\n6 — Дополнить почты с сайтов")
    print("7 — Импорт официальной выгрузки CSV/XLSX\n8 — Закрыть отдельный браузер СПАРК\n0 — Выход")
    default = "2" if load_config("spark").get("urls") else "1"
    action = input(f"Действие [{default}]: ").strip() or default
    if action == "0":
        return 0
    if action == "1":
        return cli(["configure", "--source", "spark"])
    if action == "4":
        from .spark_session import check
        return check()
    if action == "8":
        from .spark_browser import close_session, PROFILE
        from .runner import run_lock
        with run_lock(PROFILE):
            close_session()
        print("Отдельный браузер СПАРК закрыт.")
        return 0
    if action not in ("2", "3", "5", "6", "7"):
        raise ValueError("Выберите действие от 0 до 8")
    if action in ("2", "7"):
        config = load_config("spark")
        if action == "2" and not config.get("urls"):
            print("Сначала войдите в СПАРК и сохраните выборку через действие 1.")
            return 2
        folder = ROOT / "runs" / ("spark-" + datetime.now().strftime("%Y-%m-%d-%H%M%S-%f"))
        if action == "7":
            parts = shlex.split(input("Перетащите сюда официальную выгрузку CSV/XLSX: ").strip())
            if len(parts) != 1:
                raise ValueError("Нужен один файл выгрузки")
            command = ["import", "--file", parts[0], "--selection-name", config.get("selection_name", "Выгрузка СПАРК")]
        else:
            print("Сфера и регион определяются фильтрами сохранённой выборки СПАРК.")
            limit = int(input("Максимум компаний [100]: ").strip() or "100")
            if limit <= 0:
                raise ValueError("Нужен положительный лимит")
            command = ["collect", "--max-records", str(limit), "--retry-failed"]
        atomic_json(LAST_RUN, {"workdir": str(folder)})
        LAST_RUN.chmod(0o600)
        command += ["--enrich"]
    else:
        folder = last_run()
        if action == "5":
            open_results(folder)
            return 0
        if action == "3" and not (folder / "plan.json").exists():
            raise ValueError("Это импорт файла; для него используйте действие 6 или импортируйте новую выгрузку")
        command = ["collect", "--resume-plan", "--retry-failed", "--enrich"] if action == "3" else ["enrich"]
    command += ["--source", "spark", "--workdir", str(folder)]
    print(f"Папка сбора: {folder}")
    print("Ctrl+C — сохранить результат и остановиться. После повторного входа действие 3 продолжит этот же сбор.")
    code = cli(command)
    print("Сбор завершён; проверьте отчёт о доступе к контактам." if code == 0 else "Сбор остановлен или неполный. Полученные сведения сохранены; причины указаны в Excel.")
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
