"""Durable next-due markers: restart/sleep reconciliation, coalesced catch-up once."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from tzlocal import get_localzone_name
from src.collection.schedule import trigger, describe

def loop_lock():
    loop=asyncio.get_running_loop()
    lock=getattr(loop,'_collection_schedule_lock',None)
    if lock is None:
        lock=asyncio.Lock()
        loop._collection_schedule_lock=lock
    return lock


def options(params):
    return (params.get('timezone') or get_localzone_name(),
            params.get('cron_dialect', 'apscheduler'), params.get('missed_policy', 'skip'))


async def init_state(db):
    await db.execute('''CREATE TABLE IF NOT EXISTS schedule_state (
        task_id TEXT PRIMARY KEY, signature TEXT, next_due TEXT,
        missed_count INTEGER DEFAULT 0, last_missed TEXT DEFAULT '')''')
    await db.commit()


async def register(db, config, expression, enabled=True):
    await init_state(db)
    tz, dialect, policy = options(config['params'])
    t = trigger(expression,tz,dialect)
    sig = json.dumps([expression,tz,dialect,policy,enabled])
    due = t.get_next_fire_time(None,datetime.now(timezone.utc))
    await db.execute('''INSERT INTO schedule_state(task_id,signature,next_due) VALUES(?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET signature=excluded.signature,next_due=excluded.next_due
        WHERE schedule_state.signature != excluded.signature''',
        (config['id'],sig,due.isoformat() if due and enabled else None))
    await db.commit()
    return t


async def dispatch(config, *, late_only=False):
    from src.common.database import get_db
    from src.api.main import _execute_scheduled_task
    db = await get_db()
    execute = False
    async with loop_lock():
        row = await (await db.execute('SELECT cron_expression,params,enabled FROM scheduled_tasks WHERE id=?',(config['id'],))).fetchone()
        if not row or not row['enabled']:
            return
        saved = await (await db.execute('SELECT next_due FROM schedule_state WHERE task_id=?',(config['id'],))).fetchone()
        if not saved or not saved[0]:
            return
        due = datetime.fromisoformat(saved[0])
        now = datetime.now(timezone.utc)
        lateness = (now-due).total_seconds()
        if lateness < 0 or (late_only and lateness < 60):
            return
        params = json.loads(row['params'])
        tz,dialect,policy = options(params)
        t = trigger(row['cron_expression'],tz,dialect)
        next_due = t.get_next_fire_time(None, now+timedelta(seconds=1))
        missed = int(lateness >= 60)
        # Count missed occurrences up to a bounded display limit, never replay a backlog.
        misses, point = 0, due
        while missed and point and point <= now and misses < 10000:
            misses += 1
            point = t.get_next_fire_time(point,point+timedelta(seconds=1))
        from src.api.main import _scheduled_runs, _scheduled_runs_lock
        with _scheduled_runs_lock:
            busy = config['id'] in _scheduled_runs
        if busy:
            misses = max(1,misses)
        await db.execute('UPDATE schedule_state SET next_due=?,missed_count=missed_count+?,last_missed=CASE WHEN ?>0 THEN ? ELSE last_missed END WHERE task_id=?',
            (next_due.isoformat() if next_due else None,misses,misses,due.isoformat(),config['id']))
        await db.commit()
        execute = not busy and (not missed or policy=='once')
    if execute:
        await _execute_scheduled_task(config)


async def reconcile():
    from src.common.database import get_db
    db = await get_db()
    await init_state(db)
    rows = await (await db.execute('SELECT * FROM scheduled_tasks WHERE enabled=1')).fetchall()
    for row in rows:
        config = {'id':row['id'],'name':row['name'],'task_type':row['task_type'],
                  'params':json.loads(row['params']), 'save_path':row['save_path'], 'workflows':json.loads(row['workflows'])}
        try:
            tz,dialect,_=options(config['params'])
            trigger(row['cron_expression'],tz,dialect)
        except (ValueError,KeyError):
            continue
        # Dispatch asynchronously; one slow crawl must not delay other schedules.
        asyncio.create_task(dispatch(config,late_only=True))


async def metadata(db, task):
    tz,dialect,policy = options(task['params'])
    try:
        info = describe(task['cron_expression'],tz,dialect)
    except (ValueError, KeyError) as exc:
        return {'schedule_error':str(exc)}
    if not task['enabled']:
        info['next_run_at']=None
    await init_state(db)
    row = await (await db.execute('SELECT missed_count,last_missed FROM schedule_state WHERE task_id=?',(task['id'],))).fetchone()
    return {**info,'missed_policy':policy,'missed_count':row[0] if row else 0,'last_missed':row[1] if row else ''}
