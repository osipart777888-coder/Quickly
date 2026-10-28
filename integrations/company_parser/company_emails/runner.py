from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psutil

from .catalog import ROOT
from .storage import Store, now, contact_url
from .websites import WebsiteCrawler


@contextmanager
def run_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            data = json.loads(lock.read_text())
            process = psutil.Process(data["pid"])
            alive = abs(process.create_time() - data["created"]) < .1
        except (ValueError, KeyError, psutil.Error):
            alive = time.time() - lock.stat().st_mtime < 10
        if alive:
            raise ValueError("Сбор в этой папке уже запущен. Используйте другую папку или дождитесь завершения.")
        lock.unlink(missing_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as file:
        json.dump({"pid": os.getpid(), "created": psutil.Process().create_time()}, file)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def stop_process(process, descendants):
    if process.poll() is None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()
    for child in descendants.values():
        try:
            child.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(list(descendants.values()), timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def execute_task(task: dict, directory: Path, timeout: float, visible: bool, delay: float, on_progress=None) -> dict:
    job_dir = directory / "jobs" / task["id"]
    job_dir.mkdir(parents=True, exist_ok=True)
    job_file = job_dir / "task.json"
    job_file.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    result_file = job_file.with_suffix(".result.json")
    result_file.unlink(missing_ok=True)
    temporary = (job_dir / "tmp").resolve()
    temporary.mkdir(exist_ok=True)
    env = dict(os.environ, TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
    command = [sys.executable, "-u", "-m", "company_emails.worker", str(job_file.resolve()), "--delay", str(delay)]
    if visible:
        command.append("--visible")
    interrupted = False
    timed_out = False
    descendants = {}
    with (job_dir / "browser.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, env=env,
                                   start_new_session=os.name == "posix")
        started = time.monotonic()
        last_print = started
        last_update = 0
        try:
            while process.poll() is None:
                try:
                    for child in psutil.Process(process.pid).children(recursive=True):
                        descendants[(child.pid, child.create_time())] = child
                except psutil.Error:
                    pass
                if time.monotonic() - started > timeout:
                    timed_out = True
                    break
                if on_progress and time.monotonic() - last_update > 2:
                    try:
                        live = json.loads(result_file.read_text())
                    except (OSError, ValueError):
                        live = {}
                    on_progress(live)
                    last_update = time.monotonic()
                if time.monotonic() - last_print > 20:
                    try:
                        info = json.loads(result_file.read_text())
                        count = info["count"]
                    except (OSError, ValueError, KeyError):
                        count = 0
                        info = {}
                    phase = {"discovery": "поиск ссылок", "page_saved": "страница сохранена", "card": "чтение карточки", "card_saved": "карточка сохранена", "card_failed": "ошибка карточки", "retry": "повтор после ошибки"}.get(info.get("phase"), "сбор")
                    print(f"  {phase}: {count} карточек; найдено ссылок {info.get('discovered', 0)}; прошло {int(time.monotonic() - started)} с", flush=True)
                    last_print = time.monotonic()
                time.sleep(.25)
        except KeyboardInterrupt:
            interrupted = True
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            stop_process(process, descendants)
            shutil.rmtree(temporary, ignore_errors=True)
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {"status": "error", "count": 0, "errors": ["Процесс не вернул результат; см. browser.log"]}
    if interrupted or timed_out:
        result["status"] = "interrupted" if interrupted else "timeout"
        result["errors"] = ["Остановлено пользователем" if interrupted else f"Превышен лимит {timeout:g} секунд"]
    elif process.returncode and result.get("status") in ("done", "limited", "empty", "running"):
        result["status"] = "partial" if result.get("count") else "error"
        result["errors"] = [f"Процесс завершился с кодом {process.returncode}"]
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["data_file"] = job_file.with_suffix(".jsonl")
    return result


def collect(store: Store, tasks: list[dict], timeout: float = 900, visible: bool = False,
            delay: float = 1, retry_failed: bool = False, on_task=None) -> bool:
    store.add_tasks(tasks)
    stopped = False
    for number, task in enumerate(tasks, 1):
        row = store.db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()
        status = row[0]
        if status in ("done", "limited") or (status in ("empty", "error", "partial", "timeout", "needs_auth") and not retry_failed):
            print(f'[{number}/{len(tasks)}] {task["city"]} · {task["query"]}: уже обработано ({status})', flush=True)
            if on_task and status in ("done", "limited", "partial"):
                on_task(task)
            continue
        print(f'[{number}/{len(tasks)}] {task["city"]} · {task["query"]}', flush=True)
        # Recover flushed rows from a worker whose parent was killed before import.
        previous = store.directory / "jobs" / task["id"] / "task.jsonl"
        if previous.exists():
            store.import_items(task, previous)
        store.mark(task["id"], "running")
        offset = 0
        def update(live):
            nonlocal offset
            offset, _ = store.import_tail(task, previous, offset)
            store.mark(task["id"], "running", store.task_count(task["id"]), details=live)
        result = execute_task(task, store.directory, timeout, visible, delay, on_progress=update)
        store.import_items(task, result["data_file"])
        count = store.task_count(task["id"])
        errors = "; ".join(result.get("errors", []))
        if result.get("count", 0) > count:
            result["status"] = "partial"
            errors += "; Часть карточек не прошла проверку при сохранении"
        details = {key: val for key, val in result.items() if key != "data_file"}
        store.mark(task["id"], result["status"], count, errors, details)
        print(f'  {result["status"]}: {count} карточек' + (f"; {errors}" if errors else ""), flush=True)
        if result["status"] == "interrupted":
            stopped = True
            break
        if result["status"] == "needs_auth":
            if result.get("retry_later"):
                print("Сервис ограничил частоту запросов. Подождите, затем выберите продолжение в меню или --resume-plan --retry-failed.", flush=True)
            else:
                print("Нужна проверка доступа. Откройте браузер для проверки доступа в меню и затем выберите продолжение.", flush=True)
            stopped = True
            break
        if result["status"] == "error" and count == 0:
            print("Сбор приостановлен после ошибки. Проверьте журнал и используйте --retry-failed.", flush=True)
            stopped = True
            break
        if on_task and count:
            on_task(task)
    return not stopped


def enrich(store: Store, max_pages: int = 4, site_limit: int = 0, refresh: bool = False, crawler=None,
           task_id=None, skip_urls=None):
    crawler = crawler or WebsiteCrawler(max_pages=max_pages)
    sites: dict[str, list[str]] = {}
    query = ("SELECT c.id,c.payload FROM companies c JOIN observations o ON o.company_id=c.id WHERE o.task_id=? ORDER BY c.id"
             if task_id else "SELECT id,payload FROM companies ORDER BY id")
    for row in store.db.execute(query, (task_id,) if task_id else ()):
        for candidate in json.loads(row["payload"])["websites"]:
            site = contact_url(candidate)
            if not site:
                continue
            if skip_urls is not None and site in skip_urls:
                cached = store.db.execute("SELECT result FROM sites WHERE url=?", (site,)).fetchone()
                if cached:
                    with store.db:
                        store.add_emails(row["id"], json.loads(cached[0])["findings"], "website")
                continue
            sites.setdefault(site, []).append(row["id"])
    selected = list(sites.items())[:site_limit or None]
    try:
        for number, (site, ids) in enumerate(selected, 1):
            cached = store.db.execute("SELECT result,updated FROM sites WHERE url=?", (site,)).fetchone()
            result = json.loads(cached["result"]) if cached else None
            recent = cached and datetime.fromisoformat(cached["updated"]) > datetime.now(timezone.utc) - timedelta(days=7)
            if refresh or not recent or result.get("status") != "ok":
                print(f"Сайт [{number}/{len(selected)}]: {site}", flush=True)
                result = crawler.crawl(site)
                with store.db:
                    store.db.execute("INSERT OR REPLACE INTO sites VALUES(?,?,?)",
                                     (site, json.dumps(result, ensure_ascii=False), now()))
            with store.db:
                for identity in ids:
                    store.add_emails(identity, result["findings"], "website")
            print(f'  {result["status"]}: {len({f[0] for f in result["findings"]})} email; {len(result["pages"])} страниц', flush=True)
            if skip_urls is not None:
                skip_urls.add(site)
    finally:
        if hasattr(crawler.client, "close"):
            crawler.client.close()
