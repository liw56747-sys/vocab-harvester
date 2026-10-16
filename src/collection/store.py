"""Short-lived SQLite connections: safe across crawler threads and process restarts.
Raw content is local only. Credentials are never part of a persisted run specification.
"""
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from src.common.config import _resolve_data_dir, get_settings


def now():
    return datetime.now(timezone.utc).isoformat()


def key(row):
    kind = row.get('type', 'post')
    ident = (row.get('comment_id') if kind == 'comment' else row.get('tweet_id') or row.get('post_id'))
    if not ident:
        ident = hashlib.sha256((str(row.get('post_id',row.get('parent_id',''))) + '\0' + str(row.get('author', row.get('commenter', ''))) + '\0' + row.get('content', '')).encode()).hexdigest()
    return f"{row.get('platform')}:{kind}:{ident}"


def merge(old, new):
    result = {key(r): dict(r) for r in old}
    for r in new:
        k = key(r)
        prev = result.get(k, {})
        item = {**prev, **r}
        def replies(value):
            return json.loads(value or '[]') if isinstance(value, str) else (value or [])
        if 'replies_data' in prev or 'replies_data' in r:
            union = {str(x.get('tweet_id') or x.get('content')): x for x in replies(prev.get('replies_data'))}
            union.update({str(x.get('tweet_id') or x.get('content')): x for x in replies(r.get('replies_data'))})
            item['replies_data'] = list(union.values())
            item['replies_count'] = len(union)
        result[k] = item
    return list(result.values())


class Store:
    def __init__(self, path=None):
        data_dir = os.environ.get('VOCAB_DATA_DIR') or get_settings().app.data_dir or _resolve_data_dir()
        self.path = Path(path or Path(data_dir) / 'collection.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # WAL allows readers during checkpoint writes; keep FULL commit durability.
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT,
                created TEXT, updated TEXT, error TEXT DEFAULT '', owner TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS units(run_id TEXT, unit_id TEXT, rows_json TEXT NOT NULL DEFAULT '[]',
                state TEXT DEFAULT '{}', PRIMARY KEY(run_id,unit_id));
            CREATE TABLE IF NOT EXISTS export_names(run_id TEXT PRIMARY KEY, stem TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watermarks(owner TEXT PRIMARY KEY, completed_at TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, spec, owner='', run_id=None):
        spec = {k: v for k, v in spec.items() if k not in {'cookies','ct0','auth_token','proxy','api_key'}}
        rid = run_id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)', (rid, json.dumps(spec, ensure_ascii=False), 'pending', now(), now(), '', owner))
        return rid

    def update_spec(self, rid, spec):
        with self.connect() as db:
            db.execute('UPDATE runs SET spec=? WHERE id=?',(json.dumps(spec,ensure_ascii=False),rid))

    def get(self, rid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
        if not row:
            raise ValueError('采集记录不存在')
        result = dict(row)
        result['spec'] = json.loads(result['spec'])
        return result

    def list(self, limit=30, owner=None, offset=0):
        with self.connect() as db:
            rows = db.execute('SELECT id,status,created,updated,error,owner FROM runs ' +
                ('WHERE owner=? ' if owner is not None else '') + 'ORDER BY created DESC LIMIT ? OFFSET ?',
                (owner, limit, offset) if owner is not None else (limit,offset)).fetchall()
        return [dict(r) for r in rows]

    def status(self, rid, status, error=''):
        with self.connect() as db:
            db.execute('UPDATE runs SET status=?,error=?,updated=? WHERE id=?', (status, error, now(), rid))

    def save(self, rid, unit, rows, state):
        with self.connect() as db:
            changed = db.execute('INSERT INTO units VALUES(?,?,?,?) ON CONFLICT(run_id,unit_id) DO UPDATE SET rows_json=excluded.rows_json,state=excluded.state WHERE units.rows_json != excluded.rows_json OR units.state != excluded.state',
                       (rid, unit, json.dumps(rows, ensure_ascii=False), json.dumps(state, ensure_ascii=False)))
            if changed.rowcount:
                db.execute('UPDATE runs SET updated=? WHERE id=?', (now(), rid))

    def units(self, rid):
        with self.connect() as db:
            return {r['unit_id']: (json.loads(r['rows_json']), json.loads(r['state']))
                    for r in db.execute('SELECT * FROM units WHERE run_id=?', (rid,))}

    def rows(self, rid):
        result = []
        for rows, _ in self.units(rid).values():
            result = merge(result, rows)
        return result

    def set_export_stem(self, rid, stem):
        with self.connect() as db:
            db.execute('INSERT INTO export_names VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET stem=excluded.stem',
                       (rid, stem))

    def export_stem(self, rid):
        with self.connect() as db:
            row = db.execute('SELECT stem FROM export_names WHERE run_id=?', (rid,)).fetchone()
            return row[0] if row else None

    def watermark(self, owner, value=None):
        with self.connect() as db:
            if value:
                db.execute('INSERT INTO watermarks VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET completed_at=MAX(watermarks.completed_at,excluded.completed_at)', (owner, value))
            row = db.execute('SELECT completed_at FROM watermarks WHERE owner=?', (owner,)).fetchone()
            return row[0] if row else None
