"""Validate user-visible calendar meaning and durable missed-run policy."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
import pytest
from src.collection.schedule import trigger, describe
from src.collection import scheduler


@pytest.mark.parametrize('expression,expected',[
 ('0 15 * * 3','2026-10-14T15:00:00+08:00'),
 ('0 15 * * wed','2026-10-14T15:00:00+08:00'),
 ('0 15 * * 0','2026-10-18T15:00:00+08:00'),
 ('0 15 * * 7','2026-10-18T15:00:00+08:00'),
 ('0 15 * * 1-5','2026-10-14T15:00:00+08:00'),
 ('0 15 * * 0,6','2026-10-17T15:00:00+08:00'),
 ('0 15 * * */2','2026-10-15T15:00:00+08:00'),
])
def test_standard_cron_weekday(expression,expected):
    now=datetime(2026,10,14,tzinfo=ZoneInfo('Asia/Shanghai'))
    assert describe(expression,now=now)['next_run_at']==expected


def test_legacy_schedule_does_not_silently_move_a_day():
    now=datetime(2026,10,14,tzinfo=ZoneInfo('Asia/Shanghai'))
    meta=describe('0 15 * * 3',dialect='apscheduler',now=now)
    assert meta['next_run_at']=='2026-10-15T15:00:00+08:00'
    assert meta['legacy_weekdays'] and '周四' in meta['description']


def test_standard_day_and_weekday_use_or_and_timezone_is_explicit():
    now=datetime(2026,10,14,tzinfo=ZoneInfo('Asia/Shanghai'))
    assert trigger('0 15 1 * 3').get_next_fire_time(None,now).day==14
    ny=describe('0 15 * * wed',timezone='America/New_York',now=now)
    assert ny['next_run_at'].endswith('-04:00')


@pytest.mark.parametrize('expr',['0 25 * * *','0 15 * * 8','0 15 * * 5-1','0 15 * * */0','0 15 * *'])
def test_invalid_schedule_rejected(expr):
    with pytest.raises(ValueError):trigger(expr)


@pytest.mark.parametrize('policy,executions',[('skip',0),('once',1)])
async def test_restart_or_wake_records_misses_and_catches_up_at_most_once(init_test_db,monkeypatch,policy,executions):
    from src.common.database import get_db
    import src.api.main as api
    db=await get_db()
    params={'timezone':'Asia/Shanghai','cron_dialect':'unix','missed_policy':policy}
    config={'id':'calendar','params':params}
    now=datetime.now(timezone.utc)
    await db.execute('INSERT INTO scheduled_tasks(id,params,cron_expression,created_at,updated_at) VALUES(?,?,?,?,?)',
                     ('calendar',json.dumps(params),'* * * * *',now.isoformat(),now.isoformat()))
    await db.commit()
    await scheduler.register(db,config,'* * * * *')
    await db.execute('UPDATE schedule_state SET next_due=? WHERE task_id=?',((now-timedelta(minutes=3)).isoformat(),'calendar'))
    await db.commit()
    run=AsyncMock();monkeypatch.setattr(api,'_execute_scheduled_task',run)
    await scheduler.dispatch(config,late_only=True)
    await scheduler.dispatch(config,late_only=True)
    assert run.await_count==executions
    state=await (await db.execute('SELECT missed_count,last_missed,next_due FROM schedule_state')).fetchone()
    assert state['missed_count']>=3 and state['last_missed']
    assert datetime.fromisoformat(state['next_due'])>now


async def test_disabled_schedule_cannot_catch_up(init_test_db,monkeypatch):
    from src.common.database import get_db
    import src.api.main as api
    db=await get_db();stamp=datetime.now(timezone.utc).isoformat()
    await db.execute('INSERT INTO scheduled_tasks(id,enabled,created_at,updated_at) VALUES(?,?,?,?)',('off',0,stamp,stamp));await db.commit()
    await scheduler.register(db,{'id':'off','params':{}},'* * * * *')
    run=AsyncMock();monkeypatch.setattr(api,'_execute_scheduled_task',run)
    await scheduler.dispatch({'id':'off'})
    run.assert_not_called()


def test_windows_timezone_data_without_system_zone_database():
    import os,subprocess,sys
    code="from zoneinfo import ZoneInfo; from datetime import datetime; assert str(datetime(2026,10,15,tzinfo=ZoneInfo('Asia/Shanghai')).utcoffset())=='8:00:00'"
    subprocess.run([sys.executable,'-c',code],env={**os.environ,'PYTHONTZPATH':''},check=True)
