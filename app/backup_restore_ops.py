"""Post-restore steps: schema/migrations and queue reconciliation."""
from __future__ import annotations

from pathlib import Path

from fastapi import BackgroundTasks

from app.backup_pg import restore_from_path_to_thread, restore_from_upload_to_thread
from app.database import AsyncSessionLocal, engine, init_db, db_url
from app.settings_manager import reload_settings
from app.maintenance import exclusive_restore


async def reinitialize_after_restore(background_tasks: BackgroundTasks) -> None:
    """Run migrations and reload settings; schedule global queue recalculation."""
    await init_db()
    async with AsyncSessionLocal() as db:
        await reload_settings(db)
        from app.local_models import SendControl, DiagnosticPlan
        from app.local_send import local_install
        from sqlalchemy import update
        if local_install():
            control = await db.get(SendControl,1)
            if not control:
                control = SendControl(id=1)
                db.add(control)
            control.emergency_stopped = True
            control.campaigns_enabled = False
            control.reason = 'Данные восстановлены. Проверьте настройки и явно возобновите отправку.'
            await db.execute(update(DiagnosticPlan).values(enabled=False))
            await db.commit()

    from app.routers.schedule import run_recalculate_all_in_new_session

    background_tasks.add_task(run_recalculate_all_in_new_session)


async def restore_database_from_bytes(raw: bytes, background_tasks: BackgroundTasks) -> None:
    """Close the pool, pg_restore from upload bytes, then migrate and recalculate."""
    async with exclusive_restore():
        await engine.dispose()
        await restore_from_upload_to_thread(db_url, raw)
        await reinitialize_after_restore(background_tasks)


async def restore_database_from_path(dump_path: Path, background_tasks: BackgroundTasks) -> None:
    """Close the pool, pg_restore from a decrypted custom-format file, then migrate."""
    async with exclusive_restore():
        await engine.dispose()
        await restore_from_path_to_thread(db_url, dump_path)
        await reinitialize_after_restore(background_tasks)
