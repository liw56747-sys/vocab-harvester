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
    task_name       TEXT DEFAULT '',        -- 关联的定时任务名称
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

CREATE_MODEL_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS model_config (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    config_key      TEXT NOT NULL UNIQUE,
    config_value    TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL
);
"""

CREATE_SCHEDULED_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    task_type       TEXT NOT NULL DEFAULT 'search',  -- search | url_fetch
    cron_expression TEXT NOT NULL DEFAULT '0 8 * * *',
    enabled         INTEGER NOT NULL DEFAULT 1,
    -- 任务参数（JSON）
    params          TEXT NOT NULL DEFAULT '{}',
    -- 保存路径
    save_path       TEXT NOT NULL DEFAULT '',
    -- 工作流配置
    workflows       TEXT NOT NULL DEFAULT '[]',
    -- 运行状态
    last_run_at     TEXT DEFAULT '',
    last_run_status TEXT DEFAULT '',  -- success | failed | running
    last_error      TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

# 定时任务历史去重：记录已抓取过的帖子（按维度 + 时间窗跳过重复）
CREATE_SEEN_POSTS_TABLE = """
CREATE TABLE IF NOT EXISTS scheduled_seen_posts (
    post_id       TEXT NOT NULL,
    dimension     TEXT NOT NULL,   -- 去重维度："kw:<关键词>" 或 "user:<主页>"
    task_id       TEXT DEFAULT '',
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (post_id, dimension)
);
"""

# 平台 Cookie 服务端持久化：供后台定时任务（无前端）真实抓取使用
CREATE_PLATFORM_COOKIES_TABLE = """
CREATE TABLE IF NOT EXISTS platform_cookies (
    platform    TEXT PRIMARY KEY,   -- twitter | reddit
    cookie_json TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL
);
"""

CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_vocab_word ON vocabulary(word);
CREATE INDEX IF NOT EXISTS idx_vocab_status ON vocabulary(status);
CREATE INDEX IF NOT EXISTS idx_vocab_category ON vocabulary(category);
CREATE INDEX IF NOT EXISTS idx_vocab_candidate_type ON vocabulary(candidate_type);
CREATE INDEX IF NOT EXISTS idx_vocab_action ON vocabulary(action);
CREATE INDEX IF NOT EXISTS idx_seen_dimension ON scheduled_seen_posts(dimension, first_seen_at);
"""

# 已有数据库迁移：为旧表添加新列
MIGRATE_COLUMNS = [
    ("candidate_type", "TEXT DEFAULT ''"),
    ("source_type", "TEXT DEFAULT ''"),
    ("evidence", "TEXT DEFAULT ''"),
    ("reason", "TEXT DEFAULT ''"),
    ("action", "TEXT DEFAULT ''"),
    ("match_type", "TEXT DEFAULT ''"),
    ("task_name", "TEXT DEFAULT ''"),
]


async def init_db(db_path: str | Path | None = None) -> aiosqlite.Connection:
    """初始化数据库连接并建表（幂等：已有连接时直接返回）"""
    global _db_path, _connection

    if _connection is not None:
        return _connection

    if db_path is None:
        # 动态解析：打包后用用户数据目录，开发时用 ./data/
        import sys
        if getattr(sys, "frozen", False):
            db_path = Path.home() / ".vocab-harvester" / "vocab.db"
        else:
            db_path = Path("./data") / "vocab.db"

    _db_path = Path(db_path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    _connection = await aiosqlite.connect(str(_db_path))
    _connection.row_factory = aiosqlite.Row

    await _connection.execute(CREATE_VOCAB_TABLE)
    await _connection.execute(CREATE_CRAWL_LOG_TABLE)
    await _connection.execute(CREATE_MODEL_CONFIG_TABLE)
    await _connection.execute(CREATE_SCHEDULED_TASKS_TABLE)
    await _connection.execute(CREATE_SEEN_POSTS_TABLE)
    await _connection.execute(CREATE_PLATFORM_COOKIES_TABLE)
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
