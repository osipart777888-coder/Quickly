from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .__main__ import main as cli
from .sources import SOURCES, load_config


def choose(prompt: str, options: list[tuple[str, str]], default="1") -> str:
    print()
    for key, text in options:
        print(f"{key} — {text}")
    selected = input(f"{prompt} [{default}]: ").strip() or default
    if selected not in dict(options):
        raise ValueError("Выберите один из указанных вариантов")
    return selected


def main():
    print("\nСбор публичных контактов компаний")
    source = {str(i): key for i, key in enumerate(SOURCES, 1)}[choose(
        "Источник", [(str(i), info["name"]) for i, info in enumerate(SOURCES.values(), 1)], default="2")]
    if source == "yandex":
        from .yandex_menu import main as yandex_main
        return yandex_main()
    if source == "2gis":
        from .twogis_menu import main as twogis_main
        return twogis_main()
    if source == "spark":
        from .spark_menu import main as spark_main
        return spark_main()
    if source == "rusprofile":
        from .rusprofile_menu import main as rusprofile_main
        return rusprofile_main()
    maps = SOURCES[source]["mode"] == "maps"
    options = [("1", "Пробный сбор"), ("2", "Сбор / продолжение"), ("3", "Вход и выборка в браузере"),
               ("4", "Дополнить почты с сайтов"), ("5", "Открыть результаты")]
    if not maps:
        options.append(("6", "Загрузить официальную выгрузку CSV/Excel"))
    action = choose("Действие", options)
    workdir = Path("runs/legal-russia" if source == "2gis" else f"runs/legal-{source}")
    code = 0
    if action == "3":
        return cli(["configure", "--source", source])
    if action == "5":
        if not (workdir / "export").exists():
            sample = Path("runs/legal-sample" if source == "2gis" else f"runs/legal-{source}-sample")
            if (sample / "export").exists():
                workdir = sample
            else:
                print("Для выбранного источника результатов пока нет.")
                return 0
    elif action == "6":
        path = input("Перетащите файл выгрузки в это окно или введите путь: ").strip()
        import shlex
        parts = shlex.split(path)
        if len(parts) != 1:
            raise ValueError("Нужен один файл CSV или XLSX")
        code = cli(["import", "--source", source, "--file", parts[0], "--workdir", str(workdir), "--enrich"])
    elif action == "4":
        code = cli(["enrich", "--workdir", str(workdir)])
    else:
        if not maps and not load_config(source).get("urls"):
            print(f'Для {SOURCES[source]["name"]} сначала нужны вход и сохранённая выборка.')
            code = cli(["configure", "--source", source])
            if code:
                return code
        command = ["collect", "--source", source, "--workdir", str(workdir), "--enrich", "--retry-failed"]
        if maps:
            print("Сфера по умолчанию: юридические компании и адвокаты.")
            query = input("Другая сфера/поисковый запрос [Enter — юридические компании]: ").strip()
            if query:
                command += ["--query", query]
            geography = input("Города через запятую [Enter — вся Россия]: ").strip() if action == "2" else ""
            if geography:
                for city in geography.split(","):
                    command += ["--city", city.strip()]
        if action == "1":
            command.append("--sample")
            workdir = Path("runs/legal-sample" if source == "2gis" else f"runs/legal-{source}-sample")
            command[command.index("--workdir") + 1] = str(workdir)
        else:
            print("Прогресс сохраняется. Ctrl+C завершает сбор с выгрузкой уже полученных контактов.")
        code = cli(command)
    output = workdir / "export"
    if output.exists():
        print(f"Результаты: {output.resolve()}")
        if sys.platform == "darwin":
            subprocess.run(["open", str(output)], check=False)
    return code


if __name__ == "__main__":
    from .signals import install_shutdown_handlers
    install_shutdown_handlers()
    try:
        raise SystemExit(main())
    except (ValueError, KeyboardInterrupt, EOFError) as exc:
        print(f"Остановлено: {exc}")
        raise SystemExit(2)
