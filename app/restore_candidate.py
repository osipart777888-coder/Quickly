"""Prepare an isolated restore candidate in a fresh Python process."""
import asyncio
import os

async def prepare():
    from fastapi import BackgroundTasks
    from app import local_models  # register extensions before create_all
    from app.backup_pg import _verify_restored_database,sync_connection_uri
    from app.backup_restore_ops import reinitialize_after_restore
    from app.database import Base,engine
    from sqlalchemy import select
    uri=sync_connection_uri(os.environ['DATABASE_URL'])
    _verify_restored_database(uri)
    await reinitialize_after_restore(BackgroundTasks())
    # A swallowed/partial migration cannot pass merely because init_db returned.
    async with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            await connection.execute(select(table).limit(0))
        from sqlalchemy import inspect
        def check_schema(sync):
            inspector=inspect(sync)
            for table in Base.metadata.sorted_tables:
                expected={c.name for c in table.primary_key.columns}
                if expected!=set(inspector.get_pk_constraint(table.name)['constrained_columns']):
                    raise RuntimeError('Несовместимый первичный ключ таблицы '+table.name)
                columns={c['name']:c for c in inspector.get_columns(table.name)}
                for column in table.columns:
                    actual=columns[column.name]
                    if column.type._type_affinity is not actual['type']._type_affinity:
                        raise RuntimeError('Несовместимый тип столбца '+table.name+'.'+column.name)
                    wanted=getattr(column.type,'length',None)
                    present=getattr(actual['type'],'length',None)
                    if wanted and present and present<wanted:
                        raise RuntimeError('Недостаточная длина столбца '+table.name+'.'+column.name)
                    if not column.nullable and actual['nullable']:
                        raise RuntimeError('Отсутствует NOT NULL '+table.name+'.'+column.name)
                from sqlalchemy import UniqueConstraint,ForeignKeyConstraint
                unique={tuple(c['column_names']) for c in inspector.get_unique_constraints(table.name)}
                unique.update(tuple(i['column_names']) for i in inspector.get_indexes(table.name) if i.get('unique'))
                expected_unique={tuple(c.name for c in rule.columns) for rule in table.constraints if isinstance(rule,UniqueConstraint)}
                expected_unique.update(tuple(c.name for c in index.columns) for index in table.indexes if index.unique)
                if not expected_unique<=unique:
                    raise RuntimeError('Отсутствует ограничение уникальности '+table.name)
                foreign={(tuple(f['constrained_columns']),f['referred_table'],tuple(f['referred_columns'])) for f in inspector.get_foreign_keys(table.name)}
                expected_foreign={(tuple(c.name for c in rule.columns),rule.referred_table.name,tuple(e.column.name for e in rule.elements)) for rule in table.constraints if isinstance(rule,ForeignKeyConstraint)}
                if not expected_foreign<=foreign:
                    raise RuntimeError('Отсутствует внешний ключ '+table.name)
        await connection.run_sync(check_schema)
    _verify_restored_database(uri)
    await engine.dispose()

if __name__=='__main__':
    asyncio.run(prepare())
