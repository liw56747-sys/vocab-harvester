"""数据库管理：SQLite 异步连接与表初始化"""

from __future__ import annotations

import aiosqlite
from pathlib import Path

_db_path: Path | None = None
_connection: aiosqlite.Connection | None = None

# ── 建表 SQL ──────────────────────────────────────────────

CREATE_VOCAB_TABLE = """
CREATE TABLE IF NOT EXISTS vocabulary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    word            TEXT NOT NULL,
    category        TEXT DEFAULT '',
    frequency       INTEGER DEFAULT 1,
    score           REAL DEFAULT 0.0,
    platforms       TEXT DEFAULT '[]',      -- JSON 数组
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    context_samples TEXT DEFAULT '[]',      -- JSON 数组
    status          TEXT DEFAULT 'pending',
    candidate_type  TEXT DEFAULT '',        -- event_word/entity_variant/slogan/...
    source_type     TEXT DEFAULT '',        -- post/comment/nickname/bio/...
    evidence        TEXT DEFAULT '',        -- 原文证据
    reason          TEXT DEFAULT '',        -- 提取原因
    action          TEXT DEFAULT '',        -- add_temp_kb/observe/need_human_review/reject
    match_type      TEXT DEFAULT '',        -- exact/contains/regex/context_only/...
    UNIQUE(word, category)
);
"""

CREATE_CRAWL_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS crawl_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    query_keywords TEXT NOT NULL,       -- JSON 数组
    post_count  INTEGER DEFAULT 0,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT DEFAULT 'running', -- running | success | failed
    error_msg   TEXT DEFAULT ''
);
"""

CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_vocab_word ON vocabulary(word);
CREATE INDEX IF NOT EXISTS idx_vocab_status ON vocabulary(status);
CREATE INDEX IF NOT EXISTS idx_vocab_category ON vocabulary(category);
CREATE INDEX IF NOT EXISTS idx_vocab_candidate_type ON vocabulary(candidate_type);
CREATE INDEX IF NOT EXISTS idx_vocab_action ON vocabulary(action);
"""

# 已有数据库迁移：为旧表添加新列
MIGRATE_COLUMNS = [
    ("candidate_type", "TEXT DEFAULT ''"),
    ("source_type", "TEXT DEFAULT ''"),
    ("evidence", "TEXT DEFAULT ''"),
    ("reason", "TEXT DEFAULT ''"),
    ("action", "TEXT DEFAULT ''"),
    ("match_type", "TEXT DEFAULT ''"),
]


async def init_db(db_path: str | Path = "./data/vocab.db") -> aiosqlite.Connection:
    """初始化数据库连接并建表"""
    global _db_path, _connection

    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    _connection = await aiosqlite.connect(str(_db_path))
    _connection.row_factory = aiosqlite.Row

    await _connection.execute(CREATE_VOCAB_TABLE)
    await _connection.execute(CREATE_CRAWL_LOG_TABLE)
    await _connection.executescript(CREATE_INDEX)

    # 迁移：为旧表添加新列（忽略已存在的列）
    for col_name, col_type in MIGRATE_COLUMNS:
        try:
            await _connection.execute(
                f"ALTER TABLE vocabulary ADD COLUMN {col_name} {col_type}"
            )
        except aiosqlite.OperationalError:
            pass  # 列已存在

    await _connection.commit()

    return _connection


async def get_db() -> aiosqlite.Connection:
    """获取数据库连接"""
    global _connection
    if _connection is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _connection


async def close_db() -> None:
    """关闭数据库连接"""
    global _connection
    if _connection:
        await _connection.close()
        _connection = None
