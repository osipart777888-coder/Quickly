"""Manual sign-in in an owned Chrome profile; no password handling in the program."""
from __future__ import annotations

import requests
from urllib.parse import urlsplit

from .adapters import NeedsAccess, card_url
from .rusprofile_browser import RusprofileBrowser
from .checkpoints import atomic_json
from .runner import run_lock
from .sources import LOCAL, SOURCES, load_config, save_config, source_url
from .rusprofile import RusprofileLayoutError, probe
from .storage import now


def selection_tab(browser):
    pages = requests.get(browser._dev_url + "/json", timeout=5).json()
    candidates = []
    for page in pages:
        if page.get("type") != "page":
            continue
        try:
            url = source_url("rusprofile", page.get("url", ""))
        except ValueError:
            continue
        parts = urlsplit(url)
        if any(word in (parts.path + parts.fragment).lower() for word in ("login", "signin", "oauth", "authorize", "/auth")):
            continue
        if parts.path in ("", "/") and not parts.fragment and not parts.query:
            continue
        score = 3 if any(word in (parts.path + parts.fragment).lower() for word in ("search", "list", "selection", "codes")) else 2 if card_url("rusprofile", url) else 0
        if score:
            candidates.append((score, page))
    if not candidates:
        return browser.execute_script("location.href")
    page = max(candidates, key=lambda candidate: candidate[0])[1]
    print("Найдена вкладка Rusprofile: " + page.get("title", ""))
    if page["id"] != browser._chrome_tab.id:
        selected = next(tab for tab in browser._chrome_interface.list_tab(timeout=5) if tab.id == page["id"])
        browser._chrome_tab.stop()
        browser._chrome_tab = selected
        selected.start()
        browser._setup_tab()
    return page["url"]


def configure(initial_url=None):
    config = load_config("rusprofile")
    url = initial_url or (config.get("urls") or [SOURCES["rusprofile"]["home"]])[0]
    print("Открываю отдельный Chrome для Rusprofile. Введите логин, пароль и код подтверждения только на сайте.")
    print("После входа выберите сферу/ОКВЭД и регион в Rusprofile, затем откройте список компаний.")
    print("Нажмите «Сохранить поиск» в Rusprofile и откройте этот поиск, чтобы возвращаться к выбранным фильтрам.")
    print("Для юридических услуг: ОКВЭД 69.10; регион — нужный город или вся Россия.")
    with run_lock(LOCAL / "profiles/rusprofile"):
        with RusprofileBrowser() as browser:
            browser.load(source_url("rusprofile", url))
            input("Когда выборка открыта, вернитесь в терминал и нажмите Enter… ")
            current = selection_tab(browser)
            selected = input("Ссылка выборки [Enter — открытая вкладка Rusprofile]: ").strip() or current
            selected = source_url("rusprofile", selected)
            parts = urlsplit(selected)
            if parts.path in ("", "/") and not parts.query and not parts.fragment:
                raise ValueError("Открыта главная страница. Войдите и откройте выборку компаний.")
            name = input("Название выборки [Юридические услуги, ОКВЭД 69.10]: ").strip() or "Юридические услуги, ОКВЭД 69.10"
            try:
                result = {"checked_at": now(), **probe(browser, selected)}
            except (NeedsAccess, RusprofileLayoutError) as exc:
                result = {"checked_at": now(), "verified": False, "note": str(exc)}
            if result.get("selection_ready"):
                save_config("rusprofile", [selected], name, options={"access_check": result, "selection_fingerprint": result.get("selection_fingerprint", [])})
            atomic_json(LOCAL / "rusprofile-access.json", result)
            print("Выборка и профиль сохранены локально." if result.get("selection_ready") else "Профиль сохранён; выборку пока не удалось подтвердить.")
            print(result["note"])
            return 0 if result["verified"] else 2


def check():
    config = load_config("rusprofile")
    urls = config.get("urls", [])
    if not urls:
        raise ValueError("Сначала выполните «Вход и выборка» для Rusprofile")
    try:
        with run_lock(LOCAL / "profiles/rusprofile"):
            with RusprofileBrowser() as browser:
                result = {"checked_at": now(), **probe(browser, urls[0])}
    except (NeedsAccess, RusprofileLayoutError) as exc:
        result = {"checked_at": now(), "verified": False, "note": str(exc)}
    atomic_json(LOCAL / "rusprofile-access.json", result)
    options = dict(config.get("options", {}))
    options["access_check"] = result
    save_config("rusprofile", urls, config.get("selection_name", "Выборка Rusprofile"), options=options)
    print(result["note"])
    return 0 if result["verified"] else 2
