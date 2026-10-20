import json

from src.common.database import get_db, close_db, init_db
from src.collection.store import Store
from src.collection.engine import CollectionSpec, expected_posts


async def test_upgrade_saved_schedules_once_without_changing_history(init_test_db, tmp_path, monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR', str(tmp_path))
    db = await get_db()
    await db.execute('DELETE FROM collection_migrations')
    base = {'keywords':list('abcdef'), 'platforms':['twitter','reddit'], 'count':50,
            'include_replies':True, 'timezone':'Asia/Shanghai'}
    for name, scope, kind in [('legacy','per_platform','search'), ('missing',None,'search'),
                              ('explicit','per_keyword','search'), ('total','total','search'),
                              ('profile','per_platform','url_fetch')]:
        params = dict(base)
        if scope: params['count_scope'] = scope
        await db.execute('INSERT INTO scheduled_tasks(id,task_type,params,created_at,updated_at) VALUES(?,?,?,?,?)',
                         (name,kind,json.dumps(params),'before','before'))
    await db.commit()
    store = Store()
    rid = store.create(CollectionSpec(**base,count_scope='per_platform').model_dump(mode='json'), owner='legacy')
    await close_db()
    db = await init_db(init_test_db)
    rows = {r['id']:json.loads(r['params']) for r in await (await db.execute('SELECT id,params FROM scheduled_tasks')).fetchall()}
    for name in ['legacy','missing','explicit']:
        assert expected_posts(CollectionSpec(**rows[name])) == 600
        assert rows[name]['include_replies'] is True and rows[name]['timezone'] == 'Asia/Shanghai'
    assert rows['legacy']['previous_count_scope'] == 'per_platform'
    assert rows['total']['count_scope'] == 'total' and rows['profile']['count_scope'] == 'per_platform'
    assert expected_posts(CollectionSpec(**store.get(rid)['spec'])) == 100
    # A later deliberate advanced selection is not overwritten on every startup.
    rows['legacy']['count_scope'] = 'per_platform'
    await db.execute('UPDATE scheduled_tasks SET params=? WHERE id=?',(json.dumps(rows['legacy']),'legacy'))
    await db.commit(); await close_db()
    db = await init_db(init_test_db)
    saved = await (await db.execute('SELECT params FROM scheduled_tasks WHERE id=?',('legacy',))).fetchone()
    assert json.loads(saved[0])['count_scope'] == 'per_platform'
