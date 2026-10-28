"""Dedicated Chrome profiles; no access to the user's regular browser profile."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from .catalog import ROOT
from .sources import LOCAL, SOURCES, load_config, save_config, source_url

sys.path.insert(0, str(ROOT / "vendor"))
from parser_2gis.chrome import ChromeOptions, ChromeRemote
from parser_2gis.chrome.utils import free_port, locate_chrome_path


class ManagedChrome:
    def __init__(self, options, profile: Path | None, persistent=False):
        binary = str(options.binary_path or locate_chrome_path() or "")
        if not binary:
            raise ValueError("Не найден Google Chrome")
        self.temporary = profile is None
        self.profile = profile or Path(tempfile.mkdtemp(prefix="company-contacts-"))
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile.chmod(0o700)
        close_stale_browser(self.profile)
        self.remote_port = free_port()
        command = [binary, f"--user-data-dir={self.profile.resolve()}",
                   f"--remote-debugging-port={self.remote_port}", "--remote-debugging-address=127.0.0.1",
                   "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
                   "--disable-background-networking", "--disable-sync"]
        if options.headless:
            command.append("--headless=new")
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=persistent)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.temporary:
            shutil.rmtree(self.profile, ignore_errors=True)


def close_stale_browser(profile: Path):
    # Called under this app's profile lock; recover only its dedicated debugging browser.
    marker = f"--user-data-dir={profile.resolve()}"
    stale = []
    for process in psutil.process_iter(["cmdline"]):
        try:
            command = process.info["cmdline"] or []
            if marker in command and any(arg.startswith("--remote-debugging-port=") for arg in command):
                stale.extend(process.children(recursive=True))
                stale.append(process)
        except psutil.Error:
            continue
    for process in stale:
        try:
            process.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(stale, timeout=3)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass


class SourceBrowser(ChromeRemote):
    def __init__(self, source: str, visible=False):
        super().__init__(ChromeOptions(headless=not visible, memory_limit=2048), [])
        self.source = source
        self._chrome_tab = None
        self._chrome_browser = None

    def start(self):
        self._chrome_browser = ManagedChrome(self._chrome_options, LOCAL / "profiles" / self.source)
        self._dev_url = f"http://127.0.0.1:{self._chrome_browser.remote_port}"
        try:
            self._connect_interface()
            self._setup_tab()
            self._init_tab_monitor()
        except BaseException:
            self._chrome_browser.close()
            raise

    def load(self, url: str, timeout=35):
        # Contacts don't need to wait for slow adverts/images and window.load.
        dom_ready = threading.Event()
        committed = threading.Event()
        self._chrome_tab.Page.domContentEventFired = lambda **kw: dom_ready.set()
        def frame_navigated(frame, **kw):
            if not frame.get("parentId"):
                committed.set()
        self._chrome_tab.Page.frameNavigated = frame_navigated
        response = self._chrome_tab.Page.navigate(url=url, _timeout=timeout)
        if response.get("errorText"):
            raise ValueError(f'Ошибка загрузки страницы: {response["errorText"]}')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if dom_ready.wait(.15):
                return
            if committed.is_set():
                try:
                    if self.execute_script("document.readyState==='interactive'||document.readyState==='complete'"):
                        return
                except Exception:
                    pass  # Runtime context is briefly replaced during navigation.
        raise ValueError("Страница не загрузилась за отведённое время")

    def snapshot(self) -> dict:
        result = self.execute_script(r"""(()=>{
            const visible=e=>!!e&&(e.getClientRects().length>0)&&getComputedStyle(e).visibility!=='hidden';
            const main=document.querySelector('main,[role=main]')||document.body;
            const walker=document.createTreeWalker(main,NodeFilter.SHOW_TEXT);
            const text=[];let node;
            while(node=walker.nextNode()){
                const p=node.parentElement;
                if(visible(p)&&!p.closest('header,footer,nav,script,style,noscript,.footer,.header')&&node.nodeValue.trim())
                    text.push(node.nodeValue.trim());
            }
            return {url:location.href,title:document.title,text:text.join('\n'),
                bodyText:document.body?.innerText||'',h1:document.querySelector('h1')?.innerText||'',
                links:Array.from(document.querySelectorAll('a[href]')).filter(visible).map(a=>({href:a.href,text:a.innerText,
                    cls:a.className,rel:a.rel,aria:a.getAttribute('aria-label')||'',
                    inChrome:!!a.closest('header,footer,nav,.footer,.header'),
                    inContacts:!!a.closest('[class*=contact],[id*=contact],[itemprop=url]')})),
                ld:Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s=>s.textContent),
                fields:Array.from(document.querySelectorAll('[itemprop]')).filter(visible).map(e=>({name:e.getAttribute('itemprop'),
                    text:e.innerText||e.getAttribute('content')||'',href:e.getAttribute('href')||''})),
                address:document.querySelector('.business-contacts-view__address-link,.orgpage-header-view__address,[itemprop=address]')?.innerText||'',
                phoneText:document.querySelector('.orgpage-phones-view__content,.card-phones-view')?.innerText||'',
                categories:Array.from(document.querySelectorAll('.orgpage-categories-info-view__link')).map(e=>e.innerText)};
        })()""")
        if not isinstance(result, dict):
            raise ValueError("Не удалось прочитать видимую страницу браузера")
        return result


def configure(source: str, initial_url: str | None = None):
    if source == "rusprofile":
        from .rusprofile_session import configure as configure_rusprofile
        return configure_rusprofile(initial_url)
    if source == "spark":
        from .spark_session import configure as configure_spark
        return configure_spark(initial_url)
    from .runner import run_lock

    config = load_config(source)
    url = initial_url or (config.get("urls") or [SOURCES[source]["home"]])[0]
    print(f'Открываю отдельное окно {SOURCES[source]["name"]}. Войдите на сайте самостоятельно.')
    if SOURCES[source]["mode"] == "selection":
        print("Выберите нужную сферу/ОКВЭД и географию. Для юридических компаний: ОКВЭД 69.10, вся Россия.")
        print("Оставьте открытой страницу списка компаний или отдельную карточку.")
    with run_lock(LOCAL / "profiles" / source):
        with SourceBrowser(source, visible=True) as browser:
            browser.load(source_url(source, url))
            input("Когда вход и выборка готовы, вернитесь сюда и нажмите Enter… ")
            if SOURCES[source]["mode"] == "selection":
                # Chrome's current tab URL is sufficient when setup stays in this tab.
                current = browser.execute_script("location.href")
                print("Для сохранения можно вставить адрес открытой выборки из браузера.")
                selected = input(f"Ссылка [Enter — текущая вкладка]: ").strip() or current
                parts = urlsplit(source_url(source, selected))
                if parts.path in ("", "/") and not parts.query and not parts.fragment:
                    raise ValueError("Открыта главная страница. Сначала откройте список компаний или карточку и сохраните её ссылку.")
                name = input("Название выборки [Юридические компании, ОКВЭД 69.10]: ").strip() or "Юридические компании, ОКВЭД 69.10"
                save_config(source, [selected], name)
                print("Выборка сохранена. Её фактические фильтры определяются настройками сайта.")
    print("Профиль сохранён локально. Пароль в программу или чат вводить не требуется.")
