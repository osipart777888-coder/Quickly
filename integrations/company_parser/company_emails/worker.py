"""One browser job. The parent applies a time limit and imports flushed JSONL."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .catalog import ROOT

sys.path.insert(0, str(ROOT / "vendor"))


# Compatibility imports for existing integrations and tests.
from .twogis import CollectionError, TwoGisCollector as BrowserCollector


def run(job_file: Path, headless: bool = True, delay: float = 1):
    from .checkpoints import atomic_json, recover_jsonl
    task = json.loads(job_file.read_text(encoding="utf-8"))
    result_file = job_file.with_suffix(".result.json")
    data_file = job_file.with_suffix(".jsonl")
    source = task.get("source", "2gis")
    recovered = recover_jsonl(data_file) if source in ("yandex", "2gis", "spark", "rusprofile") else {}
    count = len(recovered)

    def progress(n, **info):
        nonlocal count
        count = n
        atomic_json(result_file, {"status": "running", "count": n, **info})

    result = {"status": "error", "count": 0}
    try:
        with data_file.open("a" if source in ("yandex", "2gis", "spark", "rusprofile") else "w", encoding="utf-8") as output:
            from .adapters import DirectoryCollector, NeedsAccess, RateLimited, YandexCollector
            from .browser import SourceBrowser
            from .runner import run_lock
            from .sources import LOCAL
            try:
                with run_lock(LOCAL / "profiles" / source):
                    if source == "spark":
                        from .spark_browser import SparkBrowser
                        factory = lambda: SparkBrowser(visible=True, require_existing=True)
                    elif source == "rusprofile":
                        from .rusprofile_browser import RusprofileBrowser
                        factory = lambda: RusprofileBrowser(visible=True, require_existing=True)
                    else:
                        factory = lambda: SourceBrowser(source, visible=not headless)
                    with factory() as browser:
                        if source == "2gis":
                            collector = BrowserCollector(browser, delay, job_file.with_name("2gis-state.json"), recovered)
                        elif source == "yandex":
                            collector = YandexCollector(browser, delay, job_file.with_name("yandex-state.json"),
                                                        recovered, load_yandex_cache(job_file.parents[2] / "contacts.sqlite"))
                        elif source == "spark":
                            from .spark import SparkCollector
                            collector = SparkCollector(browser, delay, job_file.with_name("spark-state.json"), recovered)
                        elif source == "rusprofile":
                            from .rusprofile import RusprofileCollector
                            collector = RusprofileCollector(browser, delay, job_file.with_name("rusprofile-state.json"), recovered)
                        else:
                            collector = DirectoryCollector(browser, source, delay)
                        result = collector.collect(task, output, progress)
            except NeedsAccess as exc:
                result = {"status": "needs_auth", "count": count, "errors": [str(exc)], "retry_later": isinstance(exc, RateLimited)}
    except KeyboardInterrupt:
        result = {"status": "interrupted", "count": count, "errors": ["Остановлено пользователем"]}
    except Exception as exc:
        result = {"status": "partial" if count else "error", "count": count,
                  "errors": [f"{type(exc).__name__}: {exc}"]}
    atomic_json(result_file, result)
    return 0 if result["status"] in ("done", "limited", "empty") else 2


def load_yandex_cache(database: Path) -> dict:
    import sqlite3
    from datetime import datetime, timedelta, timezone
    if not database.exists():
        return {}
    cache = {}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        for raw, in connection.execute("SELECT payload FROM companies WHERE id LIKE 'yandex:%' AND updated>?", (cutoff,)):
            item = json.loads(raw)
            identity = item.pop("id").split(":", 1)[1]
            cache[identity] = {"schema": "company-contact/v1", "external_id": identity, **item}
    finally:
        connection.close()
    return cache


if __name__ == "__main__":
    from .signals import install_shutdown_handlers
    install_shutdown_handlers()
    raise SystemExit(run(Path(sys.argv[1]), headless="--visible" not in sys.argv,
                         delay=float(sys.argv[sys.argv.index("--delay") + 1]) if "--delay" in sys.argv else 1))
