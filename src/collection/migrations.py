"""One-time upgrades for saved collection settings; run snapshots stay immutable."""
import json
import logging


async def migrate_keyword_targets(db):
    await db.execute('''CREATE TABLE IF NOT EXISTS collection_migrations (
        name TEXT PRIMARY KEY)''')
    name = 'v1.8.3_keyword_targets'
    if await (await db.execute('SELECT name FROM collection_migrations WHERE name=?', (name,))).fetchone():
        return
    rows = await (await db.execute("SELECT id,params FROM scheduled_tasks WHERE task_type='search'")).fetchall()
    for row in rows:
        try:
            params = json.loads(row['params'] or '{}')
            if not isinstance(params, dict):
                continue
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning('定时任务 %s 参数无效，跳过数量口径迁移', row['id'])
            continue
        if params.get('count_scope') not in (None, 'per_platform'):
            continue
        params['previous_count_scope'] = params.get('count_scope')
        params['count_scope'] = 'per_keyword'
        params['count_scope_migrated'] = '1.8.3'
        await db.execute('UPDATE scheduled_tasks SET params=? WHERE id=?',
                         (json.dumps(params, ensure_ascii=False), row['id']))
    await db.execute('INSERT INTO collection_migrations(name) VALUES(?)', (name,))
    # Caller commits settings and the migration marker together.
