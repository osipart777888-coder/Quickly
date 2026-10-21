"""Quiesce HTTP work and scheduled workers before replacing database contents."""
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
from starlette.responses import JSONResponse

_restoring = False
_active = {}

def is_restoring():
    return _restoring


@asynccontextmanager
async def active_work():
    task = asyncio.current_task()
    _active[task] = _active.get(task,0)+1
    try:
        yield
    finally:
        remaining = _active.get(task,1)-1
        if remaining:
            _active[task] = remaining
        else:
            _active.pop(task,None)


def background_work(function):
    @wraps(function)
    async def run(*args,**kwargs):
        if _restoring:
            return
        async with active_work():
            return await function(*args,**kwargs)
    return run


class MaintenanceMiddleware:
    def __init__(self,app):
        self.app = app

    async def __call__(self,scope,receive,send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        if _restoring:
            return await JSONResponse({'detail':'Выполняется восстановление. Повторите запрос после завершения.'},
                status_code=503,headers={'Retry-After':'5'})(scope,receive,send)
        if scope.get('path','').endswith('/restore/execute') or scope.get('path','').endswith('/restore-setup/execute'):
            return await self.app(scope,receive,send)
        async with active_work():
            await self.app(scope,receive,send)


@asynccontextmanager
async def exclusive_restore(timeout=120):
    global _restoring
    from app.backup_pg import BackupToolError
    from app.scheduler import get_scheduler
    if _restoring:
        raise BackupToolError('Другое восстановление уже выполняется')
    _restoring = True
    scheduler = get_scheduler()
    resume = bool(scheduler and scheduler.running)
    if resume:
        scheduler.pause()
    try:
        deadline = asyncio.get_running_loop().time()+timeout
        current = asyncio.current_task()
        while any(t is not current and not t.done() for t in list(_active)):
            if asyncio.get_running_loop().time() >= deadline:
                raise BackupToolError('Не завершились текущие операции. База не заменена; повторите позже.')
            await asyncio.sleep(.05)
        yield
    finally:
        _restoring = False
        if resume and scheduler.running:
            scheduler.resume()


def create_background_task(coroutine):
    """Register before scheduling, including sleeps and nested notification tasks."""
    if _restoring:
        coroutine.close()
        return None
    task=asyncio.create_task(coroutine)
    _active[task]=_active.get(task,0)+1
    task.add_done_callback(lambda done:_active.pop(done,None))
    return task
