"""Isolated persistent browser for Spark; credentials stay in Chrome."""
from . import owned_browser
from .browser import ManagedChrome
from .owned_browser import psutil
from .sources import LOCAL

SESSION = LOCAL / "spark-browser.json"
PROFILE = LOCAL / "profiles/spark"


def session_info():
    return owned_browser.session_info(SESSION, PROFILE)


def save_session(data):
    owned_browser.save_session(SESSION, data)


class SparkBrowser(owned_browser.OwnedBrowser):
    def __init__(self, visible=True, require_existing=False):
        super().__init__("spark", PROFILE, SESSION, visible, require_existing)

    def session_info(self):
        return session_info()

    def save_session(self, data):
        save_session(data)


def close_session():
    owned_browser.close_session(SESSION, PROFILE)
