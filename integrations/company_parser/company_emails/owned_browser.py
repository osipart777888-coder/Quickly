"""Reuse an owned Chrome and tab so login/sessionStorage survive worker restarts."""
from __future__ import annotations

import json
import time

import psutil
import pychrome
import requests

from .adapters import NeedsAccess
from .browser import ManagedChrome, SourceBrowser
from .checkpoints import atomic_json

def session_info(session_file, profile):
    try:
        data = json.loads(session_file.read_text())
        process = psutil.Process(data["pid"])
        args = process.cmdline()
        if (abs(process.create_time() - data["created"]) > .1
                or data.get("profile") != str(profile.resolve())
                or f"--user-data-dir={profile.resolve()}" not in args
                or f"--remote-debugging-port={data['port']}" not in args):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None


def save_session(session_file, data):
    atomic_json(session_file, data)
    session_file.chmod(0o600)


class OwnedBrowser(SourceBrowser):
    def __init__(self, source, profile, session_file, visible=True, require_existing=False):
        super().__init__(source, visible=visible)
        self.profile, self.session_file = profile, session_file
        from .sources import SOURCES
        self.name = SOURCES[source]["name"]
        self.require_existing = require_existing
        self.session = None

    def session_info(self):
        return session_info(self.session_file, self.profile)

    def save_session(self, data):
        save_session(self.session_file, data)

    def start(self):
        data = self.session_info()
        if not data:
            if self.require_existing:
                raise NeedsAccess(f"Окно {self.name} закрыто. Откройте «Вход и выборка» и войдите, затем продолжите сбор.")
            self._chrome_browser = ManagedChrome(self._chrome_options, self.profile, persistent=True)
            process = self._chrome_browser.process
            data = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(),
                    "port": self._chrome_browser.remote_port, "profile": str(self.profile.resolve()), "tab_id": None}
            self.save_session(data)
        self.session = data
        self._dev_url = f"http://127.0.0.1:{data['port']}"
        self._chrome_interface = pychrome.Browser(url=self._dev_url)
        deadline = time.monotonic() + 15
        while True:
            try:
                tabs = self._chrome_interface.list_tab(timeout=1)
                break
            except requests.RequestException:
                if time.monotonic() >= deadline:
                    raise NeedsAccess(f"Нет соединения с отдельным браузером {self.name}. Закройте его и повторите «Вход и выборка».")
                time.sleep(.2)
        selected = next((tab for tab in tabs if tab.id == data.get("tab_id")), None)
        self._chrome_tab = selected or (tabs[0] if tabs else self._create_tab())
        self._chrome_tab.start()
        self._setup_tab()
        self.remember_tab()

    def remember_tab(self):
        if self.session and self._chrome_tab:
            self.session["tab_id"] = self._chrome_tab.id
            self.save_session(self.session)

    def stop(self):
        # Disconnect CDP only. Do not close the authenticated browser or its tab.
        self.remember_tab()
        if self._chrome_tab:
            self._chrome_tab.stop()
        self.clear_requests()


def close_session(session_file, profile):
    data = session_info(session_file, profile)
    if not data:
        session_file.unlink(missing_ok=True)
        return
    process = psutil.Process(data["pid"])
    children = process.children(recursive=True)
    process.terminate()
    try:
        process.wait(timeout=5)
    except psutil.TimeoutExpired:
        process.kill()
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    session_file.unlink(missing_ok=True)
