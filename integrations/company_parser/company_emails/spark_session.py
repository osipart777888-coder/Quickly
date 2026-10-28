"""Manual sign-in in an owned Chrome profile; no password handling in the program."""
from __future__ import annotations

import requests
from urllib.parse import urlsplit

from .adapters import NeedsAccess, card_url
from .spark_browser import SparkBrowser
from .checkpoints import atomic_json
from .runner import run_lock
from .sources import LOCAL, SOURCES, load_config, save_config, source_url
from .spark import SparkLayoutError, probe
from .storage import now


def selection_tab(browser):
    pages = requests.get(browser._dev_url + "/json", timeout=5).json()
    candidates = []
    for page in pages:
        if page.get("type") != "page":
            continue
        try:
            url = source_url("spark", page.get("url", ""))
        except ValueError:
            continue
        parts = urlsplit(url)
        if any(word in (parts.path + parts.fragment).lower() for word in ("login", "signin", "oauth", "authorize")):
            continue
        if parts.path in ("", "/") and not parts.fragment and not parts.query:
            continue
        score = 2 if any(word in (parts.path + parts.fragment).lower() for word in ("search", "list", "selection")) else 1
        candidates.append((score, page))
    if not candidates:
        return browser.execute_script("location.href")
    page = candidates[0][1]
    print("Найдена вкладка СПАРК: " + page.get("title", ""))
    if page["id"] != browser._chrome_tab.id:
        selected = next(tab for tab in browser._chrome_interface.list_tab(timeout=5) if tab.id == page["id"])
        browser._chrome_tab.stop()
        browser._chrome_tab = selected
        selected.start()
        browser._setup_tab()
    return page["url"]


def configure(initial_url=None):
    config = load_config("spark")
    url = initial_url or (config.get("urls") or [SOURCES["spark"]["home"]])[0]
    print("Открываю отдельный Chrome для СПАРК. Введите логин, пароль и код подтверждения только на сайте.")
    print("После входа выберите сферу/ОКВЭД и регион в СПАРК, затем откройте список компаний.")
    print("Сохраните выборку как список в СПАРК, чтобы её адрес восстанавливал те же фильтры после перезапуска.")
    print("Для юридических услуг: ОКВЭД 69.10; регион — нужный город или вся Россия.")
    with run_lock(LOCAL / "profiles/spark"):
        with SparkBrowser() as browser:
            browser.load(source_url("spark", url))
            input("Когда выборка открыта, вернитесь в терминал и нажмите Enter… ")
            current = selection_tab(browser)
            selected = input("Ссылка выборки [Enter — открытая вкладка СПАРК]: ").strip() or current
            selected = source_url("spark", selected)
            parts = urlsplit(selected)
            if parts.path in ("", "/") and not parts.query and not parts.fragment:
                raise ValueError("Открыта главная страница. Войдите и откройте выборку компаний.")
            name = input("Название выборки [Юридические услуги, ОКВЭД 69.10]: ").strip() or "Юридические услуги, ОКВЭД 69.10"
            try:
                result = {"checked_at": now(), **probe(browser, selected)}
            except (NeedsAccess, SparkLayoutError) as exc:
                result = {"checked_at": now(), "verified": False, "note": str(exc)}
            if result.get("selection_ready"):
                save_config("spark", [selected], name, options={"access_check": result, "selection_fingerprint": result.get("selection_fingerprint", [])})
            atomic_json(LOCAL / "spark-access.json", result)
            print("Выборка и профиль сохранены локально." if result.get("selection_ready") else "Профиль сохранён; выборку пока не удалось подтвердить.")
            print(result["note"])
            return 0 if result["verified"] else 2


def check():
    config = load_config("spark")
    urls = config.get("urls", [])
    if not urls:
        raise ValueError("Сначала выполните «Вход и выборка» для СПАРК")
    try:
        with run_lock(LOCAL / "profiles/spark"):
            with SparkBrowser() as browser:
                result = {"checked_at": now(), **probe(browser, urls[0])}
    except (NeedsAccess, SparkLayoutError) as exc:
        result = {"checked_at": now(), "verified": False, "note": str(exc)}
    atomic_json(LOCAL / "spark-access.json", result)
    print(result["note"])
    return 0 if result["verified"] else 2
