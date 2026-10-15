"""FastAPI 后端：API 路由 + 静态文件服务"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import uuid
import threading
import time
from contextlib import asynccontextmanager
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.crawlers.comment_results import (
    search_export_rows, comment_warnings, search_count_result, search_count_warning,
)
from src.common.config import load_settings
from src.common.database import init_db, close_db
from src.common.models import CrawlQuery, ParsedPost, Platform, VocabStatus
from src.common.version import get_version, get_platform, check_for_update_async, get_update_info, download_update
from src.crawlers.platform_config import twitter_config as _TC, reddit_config as _RC
from src.orchestrator.pipeline import Pipeline
from src.vocabulary.manager import VocabManager


# ── 文件命名工具函数 ──────────────────────────────────────

def generate_export_filename(task_name: str, ext: str = "csv") -> str:
    """
    生成规范化的导出文件名。
    
    格式：任务名-日期-编号.扩展名
    例：Twitter抓取-20260708-001.csv
    
    Args:
        task_name: 任务名称
        ext: 文件扩展名（不含点号）
    
    Returns:
        规范化的文件名
    """
    import re
    
    # 净化任务名：移除操作系统不支持的特殊字符
    # 不支持的字符：\ / : * ? " < > |
    sanitized_name = re.sub(r'[\\/:*?"<>|]', '', task_name)
    # 移除前后空白
    sanitized_name = sanitized_name.strip()
    # 如果净化后为空，使用默认名称
    if not sanitized_name:
        sanitized_name = "数据导出"
    
    # 日期格式化为 YYYYMMDD
    date_str = datetime.now().strftime("%Y%m%d")
    
    # 编号使用精确到秒的时间戳（HHMMSS）
    time_str = datetime.now().strftime("%H%M%S")
    
    # 组装文件名
    filename = f"{sanitized_name}-{date_str}-{time_str}.{ext}"
    return filename


# ── v1.5.5: XLSX 生成器（供 batch-search / search / export 复用） ──

def _generate_xlsx_bytes(
    rows: list[dict],
    fields: list[str],
    sheet_title: str = "抓取结果",
) -> bytes:
    """
    从字段+行数据构建带样式的 xlsx 二进制内容。

    - 首行样式：粗体 + 浅色底色
    - 冻结首行
    - 自适应列宽（简化：按内容长度估算，上限 60）
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    import io as _io

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]  # Excel sheet 名字上限 31 字符

    # 表头
    ws.append(fields)
    header_fill = PatternFill(start_color="EEF2FF", end_color="EEF2FF", fill_type="solid")
    header_font = Font(bold=True, color="1E1B4B")
    for col_idx, _ in enumerate(fields, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", horizontal="left", wrap_text=False)

    # 数据行
    col_widths = [len(str(h or "")) + 2 for h in fields]
    for row in rows:
        vals = []
        for i, f in enumerate(fields):
            v = row.get(f, "")
            # 布尔转字符串防止 Excel 大写
            if isinstance(v, bool):
                v = "true" if v else "false"
            # 太长的内容截断（防止单元格膨胀）
            if isinstance(v, str) and len(v) > 32000:
                v = v[:32000] + "…"
            vals.append(v)
            # 计算列宽（截断到 60）
            slen = len(str(v)) if v is not None else 0
            if slen > col_widths[i]:
                col_widths[i] = min(slen + 2, 60)
        ws.append(vals)

    # 冻结首行 + 应用列宽
    ws.freeze_panes = "A2"
    for i, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(8, w)

    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def save_task_results_to_file(
    save_path: str,
    task_name: str,
    stats: dict,
    results: list[dict] | None = None,
    file_format: str = "csv",
) -> str | None:
    """
    将任务结果保存到指定路径。

    Args:
        save_path: 保存目录的绝对路径
        task_name: 任务名称
        stats: 任务统计信息
        results: 详细帖子数据（可选；为 None 时自动从 stats 中提取 crawled_posts）
        file_format: 导出格式，"csv" 或 "xlsx"

    Returns:
        保存的文件路径，如果保存失败返回 None
    """
    import os

    if not save_path:
        logger.warning("未指定保存路径，跳过文件保存")
        return None

    # 待导出的帖子明细：优先用显式传入的 results，否则用仅抓取模式的完整帖子，再退而用采样帖子
    posts = results or stats.get("crawled_posts") or stats.get("sampled_posts") or []
    fmt = (file_format or "csv").lower()
    if fmt not in ("csv", "xlsx"):
        fmt = "csv"

    # 统一的导出字段顺序（type=帖子/评论；评论行以 parent_id 关联所属帖子）
    fields = ["type", "platform", "post_id", "parent_id", "author", "content", "published_at", "likes", "retweets", "replies", "tags"]

    def _normalize(p: dict) -> dict:
        """将帖子数据展平为导出行（兼容 crawled_posts 与 sampled_posts 两种结构）"""
        metrics = p.get("metrics") or {}
        _type = p.get("type", "post")
        return {
            "type": "评论" if _type == "comment" else "帖子",
            "platform": p.get("platform", ""),
            "post_id": p.get("post_id", ""),
            "parent_id": p.get("parent_id", ""),
            "author": p.get("author", ""),
            "content": p.get("content", ""),
            "published_at": p.get("published_at", ""),
            "likes": p.get("likes", metrics.get("likes", 0)),
            "retweets": p.get("retweets", metrics.get("retweets", 0)),
            "replies": p.get("replies", metrics.get("replies", 0)),
            "tags": p.get("tags", ""),
        }

    rows = [_normalize(p) for p in posts]

    try:
        if not os.path.exists(save_path):
            os.makedirs(save_path, exist_ok=True)
            logger.info(f"已创建保存目录: {save_path}")

        filename = generate_export_filename(task_name, fmt)
        file_path = os.path.join(save_path, filename)

        if fmt == "xlsx":
            xlsx_bytes = await asyncio.to_thread(_generate_xlsx_bytes, rows, fields, sheet_title=task_name or "抓取结果")
            with open(file_path, "wb") as f:
                f.write(xlsx_bytes)
        else:
            # CSV：首行为表头，直接写入帖子明细（utf-8-sig 防止 Excel 中文乱码）
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(fields)
                for r in rows:
                    writer.writerow([r.get(h, "") for h in fields])

        logger.info(f"任务结果已保存: {file_path}（{len(rows)} 条，格式 {fmt}）")
        return file_path

    except Exception as e:
        logger.error(f"保存任务结果失败: {e}")
        return None


# ── Lifespan ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = load_settings()
        db_path = Path(settings.app.data_dir) / "vocab.db"
        logger.info(f"Initializing database at: {db_path.resolve()}")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        await init_db(db_path)
        logger.info("Database initialized successfully")
        # 启动时自动检查更新（后台线程，不阻塞启动）
        check_for_update_async()
        # 启动时加载已配置的定时任务（确保 cron 任务在重启后自动恢复）
        _reload_scheduler()
    except Exception as e:
        logger.error(f"Lifespan startup failed: {e}", exc_info=True)
        raise
    yield
    with _scheduled_runs_lock:
        active_runs = list(_scheduled_runs.values())
    for run in active_runs:
        run.cancel()
    pending_runs = [run.future for run in active_runs if run.future is not None and not run.future.done()]
    if pending_runs:
        await asyncio.wait(pending_runs, timeout=_SCHED_STOP_GRACE_SECONDS)
    # 关闭主循环自己的浏览器实例
    try:
        from src.crawlers.browser_manager import BrowserManager
        await BrowserManager.get().close()
    except Exception:
        pass
    # 停止定时任务调度器
    global _task_scheduler
    if _task_scheduler:
        try:
            _task_scheduler.shutdown(wait=False)
        except Exception:
            pass
    await close_db()


# ── App ───────────────────────────────────────────────────

# 这里的 version 使用 get_version()，如果读不到则在下方接口中做保底
app = FastAPI(title="vocab-harvester", version=get_version() or "1.1.6", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件目录
STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"

# v1.5.5: 挂载 static 目录，用于 /static/vendor/xlsx.mini.min.js 等前端资源
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── 请求/响应模型 ────────────────────────────────────────

class CrawlRequest(BaseModel):
    platforms: list[str] = ["weibo"]
    keywords: list[str] = []
    max_results: int = 10

class ReviewRequest(BaseModel):
    word: str
    action: str  # approve | reject
    category: str = ""

class BatchReviewItem(BaseModel):
    word: str
    category: str = ""

class BatchReviewRequest(BaseModel):
    items: list[BatchReviewItem]
    action: str  # approve | reject

class BatchDeleteRequest(BaseModel):
    items: list[BatchReviewItem]

class TwitterUrlFetchRequest(BaseModel):
    urls: list[str]  # Twitter/X 用户主页 URL 列表
    count: int = 50  # 每个用户抓取多少条推文
    include_replies: bool = False  # 是否同时抓取每条推文的评论
    block_resources: bool = False  # 是否屏蔽图片/CSS 加速加载
    sort_by: str = "live"  # 排序方式: "top"(热门) 或 "live"(最新)，用户主页默认最新
    ct0: str | None = None  # 浏览器 cookie（每用户独立）
    auth_token: str | None = None
    proxy: str | None = None

class TwitterSearchRequest(BaseModel):
    keyword: str  # 搜索关键词（支持 Twitter 高级搜索语法）
    count: int = 50  # 抓取条数
    include_replies: bool = False  # 是否同时抓取每条推文的评论
    sort_by: str = "top"  # 排序方式: "top"(热门) 或 "live"(最新)
    block_resources: bool = False  # 是否屏蔽图片/CSS 加速加载
    ct0: str | None = None  # 浏览器 cookie（每用户独立）
    auth_token: str | None = None
    proxy: str | None = None

class TwitterLoginRequest(BaseModel):
    ct0: str        # 浏览器 cookie: ct0 (CSRF token)
    auth_token: str  # 浏览器 cookie: auth_token (会话 token)
    proxy: str = ""  # 代理地址，如 http://127.0.0.1:7897

class PlatformCookieConfig(BaseModel):
    platform: str                  
    ct0: str | None = None         
    auth_token: str | None = None  
    reddit_session: str | None = None  
    reddit_token: str | None = None    
    edgebucket: str | None = None      
    redesign_optout: str | None = None 
    extra_cookies: dict[str, str] | None = None  
    proxy: str | None = None       

class CollectionOptions(BaseModel):
    count_scope: str = "per_platform"
    since: str | None = None
    until: str | None = None
    window_days: int = 7
    languages: list[str] = []
    alternate_sort: bool = False
    keyword_groups: list[list[str]] = []
    incremental: bool = False
    lookback_days: int = 3

class MultiPlatformSearchRequest(CollectionOptions):
    keyword: str
    count: int = 50
    platforms: list[str] = ["twitter"]   
    sort_by: str = "top"                 
    include_replies: bool = False
    block_resources: bool = False        
    cookies: list[PlatformCookieConfig] = []  

class BatchSearchRequest(CollectionOptions):
    keywords: list[str]
    count: int = 50
    platforms: list[str] = ["twitter"]
    sort_by: str = "top"
    include_replies: bool = False
    block_resources: bool = False
    cookies: list[PlatformCookieConfig] = []
    # v1.5.3 新增
    stagger_platforms: bool = True         # 平台错峰：先跑 Reddit（快）再跑 Twitter（慢）
    worker_concurrency: int = 3            # 关键词级并发（每平台）
    max_retries: int = 2                   # 单关键词失败时的最大重试次数
    # v1.5.4 新增
    batch_size: int = 0                    # 大规模抓取分批 size（>0 生效；0=不分批）
    batch_cooldown: float = 30.0           # 批次间冷却秒数
    twitter_count_cap_with_replies: int = 0  # 0=按用户设置；正数为显式选择的主帖上限


# ── API 路由 ─────────────────────────────────────────────

# 异步任务状态存储池（OrderedDict + TTL 自动清理，防止内存泄漏）
_BACKGROUND_TASKS: OrderedDict[str, dict] = OrderedDict()
_BG_TASKS_LOCK = threading.Lock()
_BG_TASKS_MAX = 500       # 最多保留 500 个已完成任务
_BG_EXPORT_CACHE_BYTES = 64 * 1024 * 1024
_BG_EXPORT_CACHE_COUNT = 3
_BG_TASKS_TTL = 3600      # 已完成任务最多保留 1 小时

# 已取消的任务 ID 集合（用于中断搜索任务）
_CANCELLED_TASKS: set[str] = set()
_CANCELLED_LOCK = threading.Lock()


def _is_task_cancelled(task_id: str) -> bool:
    """检查任务是否已被取消"""
    with _CANCELLED_LOCK:
        return task_id in _CANCELLED_TASKS


def _cancel_task(task_id: str) -> None:
    """标记任务为已取消"""
    with _CANCELLED_LOCK:
        _CANCELLED_TASKS.add(task_id)


def _remove_cancelled(task_id: str) -> None:
    """移除取消标记（任务完成后清理）"""
    with _CANCELLED_LOCK:
        _CANCELLED_TASKS.discard(task_id)


def _set_task_status(task_id: str, data: dict) -> None:
    """线程安全地写入任务状态，并清理过期条目"""
    with _BG_TASKS_LOCK:
        data["_ts"] = time.time()
        _BACKGROUND_TASKS[task_id] = data
        # 按完成时间清理：超出数量上限或 TTL 的旧任务
        while len(_BACKGROUND_TASKS) > _BG_TASKS_MAX:
            _BACKGROUND_TASKS.popitem(last=False)
        now = time.time()
        expired = [
            tid for tid, v in _BACKGROUND_TASKS.items()
            if v.get("status") != "running" and now - v.get("_ts", now) > _BG_TASKS_TTL
        ]
        for tid in expired:
            del _BACKGROUND_TASKS[tid]
        # Durable runs can rebuild downloads from disk. Do not retain 500 full workbooks.
        used_bytes = kept = 0
        for tid, entry in reversed(list(_BACKGROUND_TASKS.items())):
            output = entry.get("result") or {}
            if not output.get("run_id"):
                continue
            size = len(output.get("csv_data", "")) + len(output.get("xlsx_data", ""))
            if not size:
                continue
            if kept >= _BG_EXPORT_CACHE_COUNT or used_bytes + size > _BG_EXPORT_CACHE_BYTES:
                summary = {k:v for k,v in output.items() if k not in ("csv_data", "xlsx_data")}
                _BACKGROUND_TASKS[tid] = {**entry, "result": summary, "_export_on_demand": True}
            else:
                kept += 1
                used_bytes += size
    # 结果页只显示汇总；逐帖提示与底层错误写入诊断日志，便于后续排查。
    if data.get("status") != "running":
        result = data.get("result") or {}
        diagnostics = [data.get("error"), result.get("error"), *(result.get("errors") or [])]
        for detail in diagnostics:
            if detail:
                logger.warning("任务 %s 采集诊断: %s", task_id, detail)


def _get_task_status(task_id: str) -> dict | None:
    """线程安全地读取任务状态"""
    with _BG_TASKS_LOCK:
        return _BACKGROUND_TASKS.get(task_id)

@app.get("/api/task-status")
async def get_task_status(task_id: str):
    """供前端轮询异步任务状态的接口"""
    data = _get_task_status(task_id)
    if data is None:
        return {"status": "error", "error": "任务不存在或已失效"}
    if data.get("_export_on_demand"):
        from src.collection.api import saved_result
        return {**data, "result": await saved_result(data["result"]["run_id"])}
    return data


@app.post("/api/cancel-search")
async def cancel_search(task_id: str):
    """取消正在进行的搜索任务"""
    data = _get_task_status(task_id)
    if data is None:
        return {"status": "error", "error": "任务不存在或已失效"}
    if data.get("status") != "running":
        return {"status": "error", "error": "任务未在运行中"}
    
    _cancel_task(task_id)
    logger.info(f"搜索任务已标记为取消: {task_id}")
    return {"status": "ok", "message": "搜索已取消"}


# ── 搜索任务状态查询（供自动模式轮询） ──────────────────────

@app.get("/api/search-running")
async def search_running():
    """检查是否有正在运行的搜索任务"""
    with _BG_TASKS_LOCK:
        for data in _BACKGROUND_TASKS.values():
            if data.get("status") == "running":
                return {"running": True, "task_id": data.get("_task_id", "")}
    return {"running": False}


# ── 外部审核接口（默认未接入，不与采集自动关联） ───────────────

class AuditSubmission(BaseModel):
    posts: list[ParsedPost] = Field(min_length=1)
    context: dict = Field(default_factory=dict)


def _audit_adapter():
    adapter = getattr(app.state, "audit_adapter", None)
    if adapter is None:
        raise HTTPException(503, {"code": "audit_not_configured", "message": "审核服务尚未接入"})
    return adapter


@app.get("/api/audit/status")
async def audit_status():
    connected = getattr(app.state, "audit_adapter", None) is not None
    return {"status": "configured" if connected else "not_configured", "configured": connected,
            "contract_version": "1", "automatic_audit": False}


@app.post("/api/audit/submit", status_code=202)
async def submit_audit(req: AuditSubmission):
    task_id = await _audit_adapter().submit(req.posts, context=req.context)
    return {"status": "submitted", "task_id": task_id}


@app.get("/api/audit/tasks/{task_id}")
async def audit_result(task_id: str):
    result = await _audit_adapter().poll_result(task_id)
    return {"status": "pending" if result is None else "completed", "result": result}


@app.get("/api/model-config", include_in_schema=False)
@app.post("/api/model-config", include_in_schema=False)
@app.post("/api/analyze-last", include_in_schema=False)
@app.post("/api/import", include_in_schema=False)
async def retired_audit_endpoint():
    raise HTTPException(410, {"code": "legacy_audit_removed", "message": "旧版内置审核已移除，请接入 /api/audit 接口"})


# ── 定时任务 CRUD ──────────────────────────────────────────

class ScheduledTaskRequest(BaseModel):
    name: str = ""
    task_type: str = "search"  # search | url_fetch
    cron_expression: str = "0 8 * * *"
    enabled: bool = True
    params: dict = {}
    save_path: str = ""
    workflows: list[str] = []


@app.get("/api/scheduled-tasks")
async def list_scheduled_tasks():
    """获取所有定时任务配置"""
    try:
        from src.common.database import get_db
        db = await get_db()
        cursor = await db.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        tasks = []
        for row in rows:
            tasks.append({
                "id": row[0], "name": row[1], "task_type": row[2],
                "cron_expression": row[3], "enabled": bool(row[4]),
                "params": json.loads(row[5]) if row[5] else {},
                "save_path": row[6], "workflows": json.loads(row[7]) if row[7] else [],
                "last_run_at": row[8], "last_run_status": row[9], "last_error": row[10],
                "created_at": row[11], "updated_at": row[12],
            })
        from src.collection.scheduler import metadata
        for task in tasks:
            task.update(await metadata(db, task))
        return {"status": "ok", "tasks": tasks}
    except Exception as e:
        logger.error(f"获取定时任务失败: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/api/scheduled-tasks")
async def create_scheduled_task(req: ScheduledTaskRequest):
    """创建或更新定时任务"""
    try:
        from src.collection.schedule import trigger
        req.params["analyze"] = False
        req.params.pop("opinion_detail", None)
        req.params.pop("opinion_rules", None)
        req.workflows = []
        req.params.setdefault("cron_dialect", "unix")
        req.params.setdefault("timezone", "Asia/Shanghai")
        if req.params.get("missed_policy", "skip") not in ("skip", "once"):
            raise ValueError("漏跑策略必须为 skip 或 once")
        trigger(req.cron_expression, req.params["timezone"], req.params["cron_dialect"])
        from src.collection.engine import CollectionSpec
        CollectionSpec(**{**req.params, 'keywords':(req.params.get('urls') or req.params.get('keywords',[])) if req.task_type=='url_fetch' else req.params.get('keywords',[]),
            'task_type':req.task_type,'sort_by':req.params.get('sort','top')})
        from src.common.database import get_db
        db = await get_db()
        task_id = str(uuid.uuid4())[:12]
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO scheduled_tasks (id, name, task_type, cron_expression, enabled, params, save_path, workflows, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, req.name, req.task_type, req.cron_expression, 1 if req.enabled else 0,
             json.dumps(req.params, ensure_ascii=False), req.save_path,
             json.dumps(req.workflows, ensure_ascii=False), now, now)
        )
        await db.commit()
        # 重新加载调度器
        _reload_scheduler()
        return {"status": "ok", "task_id": task_id, "message": "任务已创建"}
    except Exception as e:
        logger.error(f"创建定时任务失败: {e}")
        return {"status": "error", "error": str(e)}


@app.put("/api/scheduled-tasks/{task_id}")
async def update_scheduled_task(task_id: str, req: ScheduledTaskRequest):
    """更新定时任务配置"""
    try:
        from src.collection.schedule import trigger
        req.params["analyze"] = False
        req.params.pop("opinion_detail", None)
        req.params.pop("opinion_rules", None)
        req.workflows = []
        req.params.setdefault("cron_dialect", "unix")
        req.params.setdefault("timezone", "Asia/Shanghai")
        if req.params.get("missed_policy", "skip") not in ("skip", "once"):
            raise ValueError("漏跑策略必须为 skip 或 once")
        trigger(req.cron_expression, req.params["timezone"], req.params["cron_dialect"])
        from src.collection.engine import CollectionSpec
        CollectionSpec(**{**req.params, 'keywords':(req.params.get('urls') or req.params.get('keywords',[])) if req.task_type=='url_fetch' else req.params.get('keywords',[]),
            'task_type':req.task_type,'sort_by':req.params.get('sort','top')})
        from src.common.database import get_db
        db = await get_db()
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE scheduled_tasks SET name=?, task_type=?, cron_expression=?, enabled=?, params=?, save_path=?, workflows=?, updated_at=? WHERE id=?",
            (req.name, req.task_type, req.cron_expression, 1 if req.enabled else 0,
             json.dumps(req.params, ensure_ascii=False), req.save_path,
             json.dumps(req.workflows, ensure_ascii=False), now, task_id)
        )
        await db.commit()
        _reload_scheduler()
        return {"status": "ok", "message": "任务已更新"}
    except Exception as e:
        logger.error(f"更新定时任务失败: {e}")
        return {"status": "error", "error": str(e)}


@app.delete("/api/scheduled-tasks/{task_id}")
async def delete_scheduled_task(task_id: str):
    """删除定时任务"""
    try:
        from src.common.database import get_db
        db = await get_db()
        await db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
        await db.commit()
        _reload_scheduler()
        return {"status": "ok", "message": "任务已删除"}
    except Exception as e:
        logger.error(f"删除定时任务失败: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/api/scheduled-tasks/{task_id}/run")
async def run_scheduled_task_now(task_id: str, collection_id: str | None = None, comments_only: bool = False):
    """立即执行一次定时任务"""
    try:
        from src.common.database import get_db
        db = await get_db()
        cursor = await db.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            return {"status": "error", "error": "任务不存在"}
        task = {
            "id": row[0], "name": row[1], "task_type": row[2],
            "params": json.loads(row[5]) if row[5] else {},
            "save_path": row[6], "workflows": json.loads(row[7]) if row[7] else [],
        }
        if collection_id:
            from src.collection.store import Store
            if Store().get(collection_id)['owner'] != task_id:
                raise ValueError("采集记录与定时任务不匹配")
            task['collection_id'] = collection_id
            task['comments_only'] = comments_only
        run = _claim_scheduled_run(task_id)
        if run is None:
            return {"status": "error", "message": "该任务正在运行或正在停止，请勿重复执行", "error": "该任务正在运行或正在停止，请勿重复执行"}
        asyncio.create_task(_execute_scheduled_task(task, _claimed=run))
        return {"status": "ok", "message": "任务已触发"}
    except Exception as e:
        logger.error(f"触发定时任务失败: {e}")
        return {"status": "error", "error": str(e)}


# ── 定时任务调度器 ──────────────────────────────────────

_task_scheduler = None

def _reload_scheduler():
    """重新加载定时任务调度器"""
    global _task_scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        if _task_scheduler is None:
            _task_scheduler = AsyncIOScheduler()
        # 异步加载任务
        asyncio.create_task(_load_scheduled_jobs())
    except Exception as e:
        logger.error(f"重载调度器失败: {e}")

async def _load_scheduled_jobs():
    """从数据库加载定时任务并注册到调度器"""
    global _task_scheduler
    if not _task_scheduler:
        return
    try:
        from src.common.database import get_db
        db = await get_db()
        # 只恢复上个进程留下的状态；重载调度器时不能覆盖本进程的在途任务。
        with _scheduled_runs_lock:
            active_ids = list(_scheduled_runs)
        active_filter = (" AND id NOT IN (" + ",".join("?" for _ in active_ids) + ")") if active_ids else ""
        _stale_cut = (datetime.now() - timedelta(minutes=5)).isoformat()
        await db.execute(
            "UPDATE scheduled_tasks SET last_run_status='failed', "
            "last_error='上次执行被中断（应用关闭或卡死），已自动重置' "
            "WHERE last_run_status='running' AND (last_run_at IS NULL OR last_run_at < ?)" + active_filter,
            (_stale_cut, *active_ids),
        )
        await db.commit()
        from src.collection.scheduler import init_state
        await init_state(db)
        await db.execute("UPDATE schedule_state SET signature='',next_due=NULL WHERE task_id IN (SELECT id FROM scheduled_tasks WHERE enabled=0)")
        await db.commit()
        cursor = await db.execute("SELECT id, name, cron_expression, params, save_path, workflows, task_type FROM scheduled_tasks WHERE enabled=1")
        rows = await cursor.fetchall()
        desired = {f"task_{row[0]}" for row in rows if len(row[2].split()) == 5}
        for job in _task_scheduler.get_jobs():
            if job.id.startswith("task_") and job.id not in desired:
                _task_scheduler.remove_job(job.id)
        for row in rows:
            task_id, task_name, cron_expr, params_json, save_path, workflows_json, task_type = row
            task_config = {
                "id": task_id, "name": task_name, "task_type": task_type or "search",
                "params": json.loads(params_json) if params_json else {},
                "save_path": save_path, "workflows": json.loads(workflows_json) if workflows_json else [],
            }
            from src.collection.scheduler import register, dispatch
            try:
                compiled = await register(db, task_config, cron_expr)
                _task_scheduler.add_job(dispatch, compiled, args=[task_config],
                    id=f"task_{task_id}", name=task_id, replace_existing=True,
                    misfire_grace_time=60, coalesce=True, max_instances=1)
            except (ValueError, KeyError) as exc:
                if _task_scheduler.get_job(f"task_{task_id}"):
                    _task_scheduler.remove_job(f"task_{task_id}")
                await db.execute("UPDATE scheduled_tasks SET last_error=? WHERE id=?",(f"定时配置错误: {exc}", task_id))
                await db.commit()
        from src.collection.scheduler import reconcile
        _task_scheduler.add_job(reconcile, "interval", seconds=60, id="schedule_reconcile", replace_existing=True)
        await reconcile()
        if not _task_scheduler.running:
            _task_scheduler.start()
        logger.info(f"调度器已加载 {len(rows)} 个定时任务")
    except Exception as e:
        logger.error(f"加载定时任务失败: {e}")


# 同一任务的所有入口共用占用记录；保留到抓取线程确实退出。
from src.crawlers.scheduled_run import ScheduledRun

_scheduled_runs: dict[str, ScheduledRun] = {}
_scheduled_runs_lock = threading.Lock()
_SCHED_POLL_SECONDS = 1.0
_SCHED_STOP_GRACE_SECONDS = 30.0


def _claim_scheduled_run(task_id):
    with _scheduled_runs_lock:
        if task_id in _scheduled_runs:
            return None
        run = ScheduledRun()
        _scheduled_runs[task_id] = run
        return run


def _release_scheduled_run(task_id, run):
    with _scheduled_runs_lock:
        if _scheduled_runs.get(task_id) is run:
            _scheduled_runs.pop(task_id)


def _close_crawl_loop(loop):
    async def cleanup():
        from src.crawlers.browser_manager import BrowserManager
        await BrowserManager.get().close()
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            done, unfinished = await asyncio.wait(pending, timeout=5)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if unfinished:
                logger.warning("抓取循环退出时仍有 %s 个未响应取消的协程", len(unfinished))
    try:
        loop.run_until_complete(cleanup())
    finally:
        loop.close()


def _scheduled_real_crawl(targets, keywords, count, sort_by, include_replies, block_resources, seen_ids=None, heartbeat=None) -> dict:
    """线程内创建/关闭独立浏览器；结果快照由主循环读取，取消在所属循环执行。"""
    from src.crawlers.real_crawler import crawl_twitter, crawl_reddit, friendly_error
    from src.crawlers.progress import progress_scope

    run = heartbeat if isinstance(heartbeat, ScheduledRun) else ScheduledRun()
    beat = heartbeat or run
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def crawl():
        if hasattr(run, 'collection_spec'):
            from src.collection.engine import collect
            from src.collection.store import Store
            from src.crawlers.real_crawler import _tweet_to_post, _tweet_reply_to_post, _reddit_to_post, _reddit_comment_to_post, _parse_replies
            last_signature = None
            def snapshot(rows):
                nonlocal last_signature
                groups = {}
                for row in rows:
                    platform = row['platform']
                    kw = row.get('keyword','')
                    posts = groups.setdefault(platform,[])
                    if platform == 'twitter':
                        posts.append(_tweet_to_post(row,kw))
                        posts.extend(_tweet_reply_to_post(r,str(row.get('tweet_id','')),kw) for r in _parse_replies(row.get('replies_data')))
                    else:
                        posts.append((_reddit_comment_to_post if row.get('type')=='comment' else _reddit_to_post)(row,kw))
                for platform, posts in groups.items():
                    run.save(platform,posts)
                signature = tuple((p.platform,p.post_id,p.content,p.raw_data.get('comments_status')) for group in groups.values() for p in group)
                if signature != last_signature:
                    beat()
                    last_signature = signature
            def platform_event(event, platform):
                if event == 'waiting':
                    run.waiting_for_slot = True
                elif event == 'acquired':
                    run.waiting_for_slot = False
                    beat()
                elif event == 'start':
                    run.start_platform(platform)
                elif event == 'done':
                    run.finish_platform(platform)
            try:
                rows, detail = await collect(Store(),run.collection_id,
                    {platform:{**cookies,'proxy':proxy} for platform,cookies,proxy in targets},
                    cancelled=run.cancelled.is_set,comments_only=run.comments_only,checkpoint=snapshot,seen_ids=seen_ids,
                    platform_event=platform_event)
                snapshot(rows)
                for platform,_,_ in targets:
                    run.finish_platform(platform)
                if detail['errors']:
                    run.errors['采集'] = '；'.join(detail['errors'])
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                run.errors['采集'] = friendly_error(exc)
            return

        for platform, cookies, proxy in targets:
            if run.cancelled.is_set():
                break
            run.start_platform(platform)
            logger.info("[定时运行 %s] 开始平台 %s", run.run_id, platform)
            try:
                with progress_scope(beat, lambda posts: run.save(platform, posts)):
                    if platform == "twitter":
                        posts = await crawl_twitter(keywords, count, cookies, proxy, sort_by,
                                                    include_replies, block_resources, seen_ids or set(), beat)
                    else:
                        posts = await crawl_reddit(keywords, count, cookies, proxy, sort_by,
                                                  include_replies, seen_ids or set(), beat)
                if run.cancelled.is_set():
                    # 底层可能吞掉 CancelledError 后返回局部结果；不要覆盖更完整的快照。
                    break
                run.finish_platform(platform, posts)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                run.finish_platform(platform, error=friendly_error(exc))
                logger.exception("[定时运行 %s] %s 失败", run.run_id, platform)

    task = loop.create_task(crawl())
    run.bind(loop, task)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        run.unbind()
        _close_crawl_loop(loop)
    return run.result([p for p, _, _ in targets], stopped=run.cancelled.is_set())


# ── 定时任务完成通知（供前端轮询后弹窗提示）──
_scheduled_notifications: list[dict] = []
_scheduled_notif_seq = 0
_SCHED_NOTIF_MAX = 50


def _push_scheduled_notification(task_id: str, name: str, status: str, total_posts: int = 0, error: str = "") -> None:
    """记录一次定时任务完成事件，供前端轮询后弹窗提示。"""
    global _scheduled_notif_seq
    _scheduled_notif_seq += 1
    _scheduled_notifications.append({
        "id": _scheduled_notif_seq,
        "task_id": task_id,
        "name": name,
        "status": status,            # success | empty | failed
        "total_posts": total_posts,
        "error": error or "",
        "finished_at": datetime.now().isoformat(),
    })
    if len(_scheduled_notifications) > _SCHED_NOTIF_MAX:
        del _scheduled_notifications[: len(_scheduled_notifications) - _SCHED_NOTIF_MAX]


async def _execute_scheduled_task(task_config: dict, *, _claimed=None):
    task_id = task_config.get("id", "unknown")
    run = _claimed or _claim_scheduled_run(task_id)
    if run is None:
        logger.warning("[定时任务 %s] 上次执行尚未退出，跳过重复触发", task_id)
        return
    try:
        # Queue on the API loop before creating dedicated crawler threads.
        loop = asyncio.get_running_loop()
        capacity = getattr(loop, "_scheduled_worker_capacity", None)
        if capacity is None:
            capacity = asyncio.Semaphore(4)
            loop._scheduled_worker_capacity = capacity
        async with capacity:
            if not run.cancelled.is_set():
                await _execute_scheduled_task_body(task_config, run)
    finally:
        if run.future is not None and not run.future.done():
            run.cancel()
            def finished(future):
                if not future.cancelled():
                    error = future.exception()
                    if error:
                        logger.error("[定时运行 %s] 后台退出异常: %s", run.run_id, error)
                _release_scheduled_run(task_id, run)
            run.future.add_done_callback(finished)
        else:
            _release_scheduled_run(task_id, run)


async def _execute_scheduled_task_body(task_config: dict, run: ScheduledRun):
    """执行单个定时任务：抓取 → 保存"""
    task_id = task_config.get("id", "unknown")
    task_name = task_config.get("name", "") or task_id
    logger.info(f"[定时任务 {task_id} / 运行 {run.run_id}] 开始执行，任务名: {task_name}")
    try:
        from src.common.database import get_db
        db = await get_db()
        now = datetime.now().isoformat()
        await db.execute("UPDATE scheduled_tasks SET last_run_at=?, last_run_status='running', last_error='' WHERE id=?",
                         (now, task_id))
        await db.commit()

        params = dict(task_config.get("params", {}))
        if task_config.get('collection_id'):
            from src.collection.store import Store
            saved_spec = Store().get(task_config['collection_id'])['spec']
            params.update(saved_spec)
            params['sort'] = saved_spec.get('sort_by','top')
            if saved_spec.get('task_type')=='url_fetch':
                params['urls'] = saved_spec['keywords']
        keywords = params.get("keywords", [])
        platform_names = params.get("platforms", ["twitter"])
        count = params.get("count", 50)
        sort_by = params.get("sort", "hot")
        include_replies = params.get("include_replies", False)

        # 历史去重维度（仅定时任务）：关键词维度保留30天，用户主页维度保留90天
        _task_type = task_config.get("task_type", "search")
        if _task_type == "url_fetch":
            _urls = params.get("urls", []) or keywords
            _dimension = "user:" + "|".join(sorted(str(u).strip() for u in _urls if str(u).strip()))
            _retention_days = 90
        else:
            _dimension = "kw:" + "|".join(sorted(k.strip() for k in keywords if k.strip()))
            _retention_days = 30
        _since_iso = (datetime.now() - timedelta(days=_retention_days)).isoformat()
        # 一次性取出该维度全部已见 post_id，传入抓取层用于丢弃历史重复 + 迭代补足到设定条数
        from src.vocabulary.storage import VocabStorage
        _storage = VocabStorage()
        _seen_ids = await _storage.get_seen_post_ids(_dimension, _since_iso)

        # 构建平台枚举
        platform_map = {"twitter": Platform.TWITTER, "reddit": Platform.REDDIT}
        block_resources = params.get("block_resources", False)

        # 从服务端读取各平台 Cookie（后台无前端，Cookie 必须持久化在库中）
        cookie_map = await _get_platform_cookies_db()
        targets = []            # (platform, cookies, proxy)
        skipped_platforms = []  # 缺 Cookie 的平台：跳过并记录
        for p in platform_names:
            pc = cookie_map.get(p, {})
            proxy = pc.get("proxy") or None
            if proxy and not proxy.startswith(("http://", "https://", "socks5://")):
                proxy = f"http://{proxy}"
            if p == "twitter":
                if pc.get("ct0") and pc.get("auth_token"):
                    targets.append(("twitter", {"ct0": pc["ct0"], "auth_token": pc["auth_token"]}, proxy))
                else:
                    skipped_platforms.append({"platform": "twitter", "reason": "Cookie 未配置"})
            elif p == "reddit":
                if pc.get("reddit_session"):
                    rc = {k: pc[k] for k in ("reddit_session", "reddit_token", "edgebucket", "redesign_optout") if pc.get(k)}
                    if pc.get("extra_cookies"):
                        rc.update(pc["extra_cookies"])
                    targets.append(("reddit", rc, proxy))
                else:
                    skipped_platforms.append({"platform": "reddit", "reason": "Cookie 未配置"})

        if skipped_platforms:
            logger.warning(f"[定时任务 {task_id}] 跳过未配置 Cookie 的平台: {skipped_platforms}")

        if not targets:
            msg = "所选平台均未配置 Cookie，请在『数据抓取』页配置并保存 Cookie（保存时会自动同步到服务端）"
            await db.execute("UPDATE scheduled_tasks SET last_run_status='failed', last_error=? WHERE id=?", (msg, task_id))
            await db.commit()
            logger.warning(f"[定时任务 {task_id}] {msg}")
            _push_scheduled_notification(task_id, task_name, "failed", 0, msg)
            return

        query = CrawlQuery(
            platforms=[platform_map[p] for (p, _, _) in targets],
            keywords=keywords or ["技术", "科技"],
            max_results=count,
            extra={"sort": sort_by, "include_replies": include_replies},
        )
        logger.info(f"[定时任务 {task_id}] 开始真实抓取，平台: {[p for (p, _, _) in targets]}, 关键词: {query.keywords}")

        from src.collection.engine import CollectionSpec
        from src.collection.store import Store
        _collection_store = Store()
        if task_config.get('collection_id'):
            run.collection_id = task_config['collection_id']
            _spec = CollectionSpec(**_collection_store.get(run.collection_id)['spec'])
            for unit, (saved_rows, saved_state) in _collection_store.units(run.collection_id).items():
                saved_state.pop('search_finished', None)
                if not saved_state.get('error'):
                    saved_state['partition'] = 0
                _collection_store.save(run.collection_id,unit,saved_rows,saved_state)
        else:
            _spec = CollectionSpec(**{**params, 'keywords': (params.get('urls') or keywords) if _task_type == 'url_fetch' else keywords,
                'task_type':_task_type, 'sort_by':sort_by, 'count_scope':params.get('count_scope','per_keyword')})
            if _spec.incremental and not _spec.since:
                from datetime import timezone
                watermark = _collection_store.watermark(task_id)
                if watermark:
                    _spec.since = datetime.fromisoformat(watermark).date() - timedelta(days=_spec.lookback_days)
                    _spec.until = datetime.now(timezone.utc).date() + timedelta(days=1)
            _spec = CollectionSpec(**_spec.model_dump(mode='json'))
            run.collection_id = _collection_store.create(_spec.model_dump(mode='json'),owner=task_id)
        run.collection_spec = _spec
        run.comments_only = task_config.get('comments_only',False)

        import concurrent.futures as _cf
        loop = asyncio.get_running_loop()
        stall_limit = max(1, int(os.environ.get("VOCAB_SCHED_STALL_LIMIT", "2400")))
        executor = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"sched-{run.run_id}")
        stopped = False
        try:
            run.future = loop.run_in_executor(
                executor, _scheduled_real_crawl, targets, (keywords or []), count,
                sort_by, include_replies, block_resources, _seen_ids, run,
            )
            while not run.future.done():
                await asyncio.wait({run.future}, timeout=_SCHED_POLL_SECONDS)
                if run.future.done():
                    break
                if not getattr(run,"waiting_for_slot",False) and run.idle_seconds() > stall_limit:
                    stopped = True
                    run.cancel()
                    logger.warning("[定时运行 %s] %s 秒无有效进展，请求停止", run.run_id, stall_limit)
                    await asyncio.wait({run.future}, timeout=_SCHED_STOP_GRACE_SECONDS)
                    break
            if run.future.done():
                _crawl_result = run.future.result()
            else:
                _crawl_result = run.result([p for p, _, _ in targets], stopped=True)
            if stopped:
                message = ("抓取已停止，已保留已采集数据" if run.future.done()
                           else "停止尚未确认，已保存当前快照；后台退出前禁止再次执行此任务")
                _crawl_result.setdefault("errors", {})["任务"] = message
        finally:
            executor.shutdown(wait=False)
        posts_by_platform = _crawl_result.get("posts", {})
        crawl_errors = _crawl_result.get("errors", {})  # {platform: 友好原因}

        # 记录本次新抓到的帖子 id（仅"帖子"，评论不参与去重），供后续执行去重
        _new_ids = [
            pp.post_id
            for _plist in posts_by_platform.values()
            for pp in _plist
            if (pp.raw_data or {}).get("type") == "post" and pp.post_id
        ]
        if _new_ids:
            await _storage.record_seen_posts(_new_ids, _dimension, task_id, datetime.now().isoformat())

        # 旧任务即使保留 analyze/model 配置，也只进行采集和导出。
        from src.crawlers.real_crawler import PrefetchedCrawler
        _crawlers = {p: PrefetchedCrawler(p, posts_by_platform.get(p, [])) for (p, _, _) in targets}
        pipeline = Pipeline(crawlers=_crawlers)
        stats = await pipeline.run(query, task_name=task_name, analyze=False, dedup_ctx=None)
        stats["skipped_platforms"] = skipped_platforms

        # 保存结果到指定路径
        save_path = task_config.get("save_path", "")
        save_format = params.get("save_format", "csv")
        if save_path:
            saved_file = await save_task_results_to_file(
                save_path=save_path,
                task_name=task_name,
                stats=stats,
                results=None,  # 从 stats.crawled_posts / sampled_posts 自动提取帖子明细
                file_format=save_format,
            )
            if saved_file:
                logger.info(f"[定时任务 {task_id}] 结果已保存: {saved_file}")
            else:
                crawl_errors["导出"] = "自动导出失败；原始数据仍在采集记录中，可重新下载"
        else:
            logger.info(f"[定时任务 {task_id}] 未设置自动导出路径，可从采集记录下载")

        # 综合状态与失败原因：若本次未拓回任何数据且存在抳错/跳过，则标记 failed 并记录原因
        if stats.get("error"):
            crawl_errors["处理"] = stats["error"]
        _reasons = [f"{p}：{r}" for p, r in (crawl_errors or {}).items()]
        for _sp in skipped_platforms:
            _reasons.append(f"{_sp['platform']}：{_sp['reason']}")
        _final_status = stats.get("status", "unknown")
        _last_error = ""
        if stats.get("total_posts", 0) == 0 and _reasons:
            _final_status = "failed"
            _last_error = "；".join(_reasons)
        elif _reasons:
            _final_status = "partial"
            _last_error = "（部分结果）" + "；".join(_reasons)

        await db.execute("UPDATE scheduled_tasks SET last_run_status=?, last_error=? WHERE id=?",
                         (_final_status, _last_error, task_id))
        await db.commit()
        logger.info(f"[定时任务 {task_id}] 执行完成: {stats.get('status')}, "
                     f"帖子: {stats.get('total_posts', 0)}, 关键词: {stats.get('total_keywords', 0)}")
        _push_scheduled_notification(task_id, task_name, _final_status, stats.get("total_posts", 0), _last_error)
    except Exception as e:
        logger.exception("[定时任务 %s] 执行失败: %s", task_id, e)
        try:
            from src.common.database import get_db
            db = await get_db()
            await db.execute("UPDATE scheduled_tasks SET last_run_status='failed', last_error=? WHERE id=?",
                             (str(e), task_id))
            await db.commit()
        except Exception:
            pass
        try:
            _push_scheduled_notification(task_id, task_name, "failed", 0, str(e))
        except Exception:
            pass


@app.get("/api/scheduled-notifications")
async def get_scheduled_notifications(after: int = 0):
    """返回 id 大于 after 的定时任务完成通知，供前端轮询弹窗。"""
    items = [n for n in _scheduled_notifications if n["id"] > after]
    return {"status": "ok", "notifications": items, "latest_id": _scheduled_notif_seq}


@app.get("/")
async def index():
    """返回前端页面"""
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>vocab-harvester</h1><p>前端文件未找到</p>")


@app.get("/api/stats")
async def get_stats():
    """词库统计概览"""
    manager = VocabManager()
    stats = await manager.get_stats()
    return stats


@app.post("/api/crawl")
async def trigger_crawl(req: CrawlRequest):
    """触发采集流水线（后台线程执行，通过 task_id 轮询结果）"""
    platforms = []
    for p in req.platforms:
        try:
            platforms.append(Platform(p))
        except ValueError:
            raise HTTPException(400, f"未知平台: {p}，可选: {[p.value for p in Platform]}")

    keywords = req.keywords or ["技术", "科技"]
    query = CrawlQuery(platforms=platforms, keywords=keywords, max_results=req.max_results)

    task_id = str(uuid.uuid4())
    _set_task_status(task_id, {"status": "running", "result": None})

    def _thread_target():
        import asyncio as _asyncio
        import traceback as _tb
        _loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(_loop)

        async def _run():
            try:
                from src.common.database import init_db as _init_db, close_db as _close_db
                from src.common.config import load_settings as _load_settings
                _settings = _load_settings()
                _db_path = Path(_settings.app.data_dir) / "vocab.db"
                _db_path.parent.mkdir(parents=True, exist_ok=True)
                await _init_db(_db_path)

                pipeline = Pipeline.from_config()
                stats = await pipeline.run(query, task_name="手动搜索")
                _set_task_status(task_id, {"status": "success", "result": stats})
                await _close_db()
            except Exception as e:
                _tb.print_exc()
                _set_task_status(task_id, {"status": "error", "error": str(e)})

        try:
            _loop.run_until_complete(_run())
        finally:
            _close_crawl_loop(_loop)

    threading.Thread(target=_thread_target, daemon=True).start()
    return {"status": "started", "task_id": task_id}


@app.get("/api/vocabulary")
async def query_vocabulary(
    search: str | None = None,
    category: str | None = None,
    status: str | None = None,
    platform: str | None = None,
    action: str | None = None,
    candidate_type: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    task_name: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """查询词库"""
    manager = VocabManager()
    vocab_status = VocabStatus(status) if status else None
    entries = await manager.query(
        keyword=search,
        category=category,
        status=vocab_status,
        platform=platform,
        action=action,
        candidate_type=candidate_type,
        score_min=score_min,
        score_max=score_max,
        task_name=task_name,
        limit=limit,
        offset=offset,
    )

    total = await manager.storage.count(
        keyword=search,
        category=category,
        status=vocab_status,
        platform=platform,
        action=action,
        candidate_type=candidate_type,
        score_min=score_min,
        score_max=score_max,
        task_name=task_name,
    )
    return {"items": entries, "total": total, "limit": limit, "offset": offset}


@app.get("/api/vocabulary/filter-options")
async def vocabulary_filter_options():
    """获取词库筛选器选项（分类、候选类型、任务名）"""
    manager = VocabManager()
    return await manager.get_filter_options()


@app.post("/api/vocabulary/review")
async def review_entry(req: ReviewRequest):
    """审核词条"""
    manager = VocabManager()
    if req.action == "approve":
        ok = await manager.approve(req.word, req.category)
    elif req.action == "reject":
        ok = await manager.reject(req.word, req.category)
    else:
        raise HTTPException(400, f"未知操作: {req.action}")

    if not ok:
        raise HTTPException(404, f"词条不存在: {req.word}")
    return {"ok": True, "word": req.word, "action": req.action}


@app.post("/api/vocabulary/batch-review")
async def batch_review(req: BatchReviewRequest):
    """批量审核词条"""
    manager = VocabManager()
    success = failed = 0
    for item in req.items:
        try:
            if req.action == "approve":
                ok = await manager.approve(item.word, item.category)
            elif req.action == "reject":
                ok = await manager.reject(item.word, item.category)
            else:
                failed += 1
                continue
            if ok: success += 1
            else: failed += 1
        except Exception:
            failed += 1
    return {"ok": True, "action": req.action, "success": success, "failed": failed, "total": len(req.items)}


@app.post("/api/vocabulary/batch-delete")
async def batch_delete(req: BatchDeleteRequest):
    """批量删除词条"""
    manager = VocabManager()
    success = failed = 0
    for item in req.items:
        try:
            ok = await manager.delete(item.word, item.category)
            if ok: success += 1
            else: failed += 1
        except Exception:
            failed += 1
    return {"ok": True, "success": success, "failed": failed, "total": len(req.items)}


class CsvDownloadRequest(BaseModel):
    csv_data: str
    filename: str = "data.csv"

@app.post("/api/download-csv")
async def download_csv(req: CsvDownloadRequest):
    """将 base64 CSV 数据作为文件下载返回"""
    import base64
    try:
        raw = base64.b64decode(req.csv_data)
    except Exception:
        raise HTTPException(400, "无效的 base64 数据")
    return Response(
        content=raw,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{req.filename}"'},
    )


class XlsxDownloadRequest(BaseModel):
    xlsx_data: str
    filename: str = "data.xlsx"

@app.post("/api/download-xlsx")
async def download_xlsx(req: XlsxDownloadRequest):
    """v1.5.5: 将 base64 XLSX 数据作为文件下载返回"""
    import base64
    try:
        raw = base64.b64decode(req.xlsx_data)
    except Exception:
        raise HTTPException(400, "无效的 base64 数据")
    return Response(
        content=raw,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{req.filename}"'},
    )


@app.get("/api/vocabulary/export")
async def export_vocabulary(format: str = "json", status: str | None = None):
    """导出词库"""
    manager = VocabManager()
    vocab_status = VocabStatus(status) if status else None

    if format == "json":
        content = await manager.export_json(vocab_status)
        return JSONResponse(json.loads(content))
    elif format == "csv":
        content = await manager.export_csv(vocab_status)
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="vocabulary_export.csv"'},
        )
    elif format == "txt":
        content = await manager.export_txt(vocab_status)
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="vocabulary_export.txt"'},
        )
    elif format == "xlsx":
        # v1.5.5: 词库导出为 Excel
        entries = await manager.storage.export_all(vocab_status)
        if not entries:
            raise HTTPException(404, "词库为空，无数据可导出")
        fields = list(entries[0].keys())
        rows = []
        for entry in entries:
            row = dict(entry)
            for key in ("platforms", "context_samples"):
                if key in row:
                    row[key] = json.dumps(row[key], ensure_ascii=False)
            rows.append(row)
        xlsx_bytes = await asyncio.to_thread(_generate_xlsx_bytes, rows, fields, "词库导出")
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="vocabulary_export.xlsx"'},
        )
    else:
        raise HTTPException(400, f"不支持的格式: {format}")


def _parse_date(val) -> datetime:
    if not val: return datetime.now()
    if isinstance(val, (int, float)): return datetime.fromtimestamp(val)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try: return datetime.strptime(str(val), fmt)
        except ValueError: continue
    return datetime.now()


# ── 核心抓取接口：采用硬隔离多线程 ──

@app.post("/api/twitter-fetch")
async def twitter_fetch(req: TwitterUrlFetchRequest):
    from src.collection.api import start
    from src.collection.engine import CollectionSpec
    from src.crawlers.twitter_url import extract_username
    users = [extract_username(u) for u in req.urls]
    if not users or not all(users):
        raise HTTPException(400, "请输入有效的 X 用户主页链接")
    try:
        spec = CollectionSpec(keywords=users, platforms=["twitter"], count=req.count,
            count_scope="per_keyword", task_type="url_fetch", include_replies=req.include_replies,
            block_resources=req.block_resources, sort_by="live")
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    return await start(spec, [{"platform":"twitter", "ct0":req.ct0, "auth_token":req.auth_token, "proxy":req.proxy}])


@app.post("/api/search")
async def multi_platform_search(req: MultiPlatformSearchRequest):
    from src.collection.api import start
    from src.collection.engine import CollectionSpec
    try:
        spec = CollectionSpec(**{**req.model_dump(exclude={"cookies", "keyword"}), "keywords":[req.keyword]})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return await start(spec, req.cookies)


@app.post("/api/batch-search")
async def batch_search(req: BatchSearchRequest):
    from src.collection.api import start
    from src.collection.engine import CollectionSpec
    try:
        spec = CollectionSpec(**req.model_dump(exclude={"cookies"}))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return await start(spec, req.cookies)


# ── 重点：版本号强制保底，防止返回为空 ──
@app.get("/api/version")
async def api_version():
    """当前版本号和平台"""
    v = get_version()
    # 如果没读到 VERSION 文件，强制使用 1.1.6
    if not v or str(v).strip() == "":
        v = "1.1.6"
    return {"version": v, "platform": get_platform()}


@app.post("/api/check-update")
async def api_check_update():
    """触发后台更新检查"""
    check_for_update_async()
    import time
    for _ in range(60):
        info = get_update_info()
        if info is not None:
            return info
        await asyncio.sleep(0.5)
    return {"status": "checking", "message": "正在检查更新..."}


@app.get("/api/update-info")
async def api_update_info():
    info = get_update_info()
    if info is None:
        return {"status": "not_checked", "message": "尚未检查更新"}
    return info


# ── 一键更新 ──────────────────────────────────────────────

_update_download_state = {
    "status": "idle",  # idle / downloading / done / error / installing
    "progress": 0,     # 0-100
    "error": "",
    "file_path": "",
}


@app.post("/api/download-update")
async def api_download_update():
    """下载新版本安装包"""
    global _update_download_state
    
    info = get_update_info()
    if not info or not info.get("download_url"):
        return {"status": "error", "error": "没有可用的更新"}
    
    if _update_download_state["status"] == "downloading":
        return {"status": "downloading", "progress": _update_download_state["progress"]}
    
    download_url = info["download_url"]
    latest_version = info["latest_version"]
    plat = get_platform()
    
    # 确定文件名和保存路径
    if plat == "macos":
        filename = f"vocab-harvester-{latest_version}.dmg"
    elif plat == "windows":
        filename = f"vocab-harvester-{latest_version}-setup.exe"
    else:
        filename = f"vocab-harvester-{latest_version}.tar.gz"
    
    # 保存到用户下载目录
    import os
    download_dir = Path.home() / "Downloads"
    dest_path = download_dir / filename
    
    _update_download_state = {"status": "downloading", "progress": 0, "error": "", "file_path": ""}
    
    def _do_download():
        global _update_download_state
        try:
            def on_progress(downloaded, total):
                if total > 0:
                    _update_download_state["progress"] = int(downloaded * 100 / total)
            
            success = download_update(download_url, dest_path, progress_callback=on_progress)
            if success:
                _update_download_state["status"] = "done"
                _update_download_state["progress"] = 100
                _update_download_state["file_path"] = str(dest_path)
            else:
                _update_download_state["status"] = "error"
                _update_download_state["error"] = "下载失败"
        except Exception as e:
            _update_download_state["status"] = "error"
            _update_download_state["error"] = str(e)
    
    # 后台线程下载
    t = threading.Thread(target=_do_download, daemon=True)
    t.start()
    
    return {"status": "downloading", "progress": 0}


@app.get("/api/download-progress")
async def api_download_progress():
    """获取下载进度"""
    return _update_download_state


@app.post("/api/install-update")
async def api_install_update():
    """安装已下载的安装包"""
    file_path = _update_download_state.get("file_path", "")
    if not file_path or not Path(file_path).exists():
        return {"status": "error", "error": "安装包不存在"}
    
    plat = get_platform()
    try:
        import subprocess
        if plat == "macos":
            # macOS: 打开 DMG
            subprocess.Popen(["open", file_path])
            _update_download_state["status"] = "installing"
            return {"status": "installing", "message": "已打开 DMG，请拖拽应用到 Applications"}
        elif plat == "windows":
            # Windows: 运行安装程序
            subprocess.Popen([file_path], shell=True)
            _update_download_state["status"] = "installing"
            return {"status": "installing", "message": "安装程序已启动"}
        else:
            return {"status": "error", "error": "不支持的平台"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


_chromium_install_state = {
    "status": "unknown",  
    "progress": "",
    "error": "",
}


@app.get("/api/chromium-status")
async def api_chromium_status():
    import app as _app_module
    if not getattr(_app_module, "_NEEDS_CHROMIUM_INSTALL", False):
        if _chromium_install_state["status"] == "installed":
            return {"status": "installed"}
        return {"status": "not_needed"}
    return {
        "status": _chromium_install_state["status"],
        "progress": _chromium_install_state["progress"],
        "error": _chromium_install_state["error"],
    }


@app.post("/api/install-chromium")
async def api_install_chromium():
    import app as _app_module

    if not getattr(_app_module, "_NEEDS_CHROMIUM_INSTALL", False):
        return {"status": "not_needed"}

    if _chromium_install_state["status"] == "installing":
        return {"status": "installing", "progress": _chromium_install_state["progress"]}

    if _chromium_install_state["status"] == "installed":
        return {"status": "installed"}

    _chromium_install_state["status"] = "installing"
    _chromium_install_state["progress"] = "starting"
    _chromium_install_state["error"] = ""

    def _do_install():
        import subprocess
        import sys
        _app_module._log("Starting Chromium installation...")
        _chromium_install_state["progress"] = "downloading"

        # 构建子进程环境：优先使用打包在 app 内的 Playwright 模块，
        # 避免系统 Playwright 版本不一致导致下载错误的浏览器版本
        install_env = os.environ.copy()
        _meipass = getattr(sys, "_MEIPASS", None)
        if _meipass:
            # 将打包的 site-packages 加入 PYTHONPATH，确保子进程加载 bundled Playwright
            existing = install_env.get("PYTHONPATH", "")
            install_env["PYTHONPATH"] = f"{_meipass}{os.pathsep}{existing}" if existing else _meipass
            _app_module._log(f"Using bundled Playwright from {_meipass}")

        # 爬虫只使用 headless=True 模式，只需 chromium-headless-shell（92 MB）
        # 不需要完整 Chromium（341 MB），避免下载时间过长
        browser_packages = ["chromium-headless-shell"]
        python_prefixes = [
            ["python3", "-m", "playwright"],
            ["playwright"],
        ]

        all_ok = True
        for pkg in browser_packages:
            installed = False
            for prefix in python_prefixes:
                cmd = prefix + ["install", pkg]
                try:
                    _app_module._log(f"Running: {' '.join(cmd)}")
                    _chromium_install_state["progress"] = f"downloading {pkg}"
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=300, env=install_env
                    )
                    if result.returncode == 0:
                        _app_module._log(f"{pkg} installed successfully")
                        installed = True
                        break
                    else:
                        _app_module._log(f"Command failed: {' '.join(cmd)}: {result.stderr}")
                except FileNotFoundError:
                    continue
                except subprocess.TimeoutExpired:
                    _app_module._log(f"Command timed out: {' '.join(cmd)}")
                    continue
                except Exception as e:
                    _app_module._log(f"Install error for {pkg}: {e}")
            if not installed:
                all_ok = False
                _app_module._log(f"Failed to install {pkg}")

        if all_ok:
            _chromium_install_state["status"] = "installed"
            _chromium_install_state["progress"] = "complete"
            _app_module._NEEDS_CHROMIUM_INSTALL = False
            _app_module._log("Chromium headless-shell installation succeeded")
        else:
            _chromium_install_state["status"] = "error"
            _chromium_install_state["error"] = "Installation failed. Please run 'playwright install chromium-headless-shell' in Terminal."
            _app_module._log("Chromium headless-shell installation failed")

    threading.Thread(target=_do_install, daemon=True).start()

    return {"status": "installing", "progress": "starting"}


@app.post("/api/twitter-login")
async def twitter_login(req: TwitterLoginRequest):
    from src.crawlers.twitter_url import TwitterCookieFetcher
    proxy = req.proxy.strip() if req.proxy else None
    fetcher = TwitterCookieFetcher(proxy=proxy)
    try:
        fetcher.save_cookies(req.ct0, req.auth_token, proxy=proxy)
        return {"ok": True, "message": "Cookie 已保存"}
    except Exception as e:
        raise HTTPException(400, f"保存失败: {str(e)}")


@app.get("/api/twitter-login-status")
async def twitter_login_status():
    from src.crawlers.twitter_url import TwitterCookieFetcher
    return {"logged_in": TwitterCookieFetcher().is_logged_in()}


# ── 平台 Cookie 服务端持久化（供后台定时任务真实抓取使用）──

async def _save_platform_cookies_db(platform: str, cookies: dict) -> None:
    """将指定平台的 Cookie 以 JSON 存入 platform_cookies 表（upsert）。"""
    from src.common.database import get_db
    db = await get_db()
    now = datetime.now().isoformat()
    await db.execute(
        "INSERT INTO platform_cookies (platform, cookie_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(platform) DO UPDATE SET cookie_json=excluded.cookie_json, updated_at=excluded.updated_at",
        (platform, json.dumps(cookies, ensure_ascii=False), now),
    )
    await db.commit()


async def _get_platform_cookies_db() -> dict:
    """读取所有平台的持久化 Cookie，返回 {platform: {…}}。"""
    from src.common.database import get_db
    db = await get_db()
    cursor = await db.execute("SELECT platform, cookie_json FROM platform_cookies")
    rows = await cursor.fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            out[row[0]] = json.loads(row[1]) if row[1] else {}
        except (json.JSONDecodeError, TypeError):
            out[row[0]] = {}
    return out


async def _auto_persist_cookies(twitter: dict | None = None, reddit: dict | None = None) -> None:
    """手动搜索/抓取时自动把正在使用的 Cookie 同步到服务端，确保定时任务与手动共用同一份。

    只在字段有效时写入，避免用空值覆盖已有配置。异常静默忽略（不影响抓取）。
    """
    try:
        if twitter and twitter.get("ct0") and twitter.get("auth_token"):
            await _save_platform_cookies_db("twitter", {
                "ct0": twitter.get("ct0", ""), "auth_token": twitter.get("auth_token", ""),
                "proxy": twitter.get("proxy", ""),
            })
        if reddit and reddit.get("reddit_session"):
            await _save_platform_cookies_db("reddit", {
                "reddit_session": reddit.get("reddit_session", ""),
                "reddit_token": reddit.get("reddit_token", ""),
                "edgebucket": reddit.get("edgebucket", ""),
                "redesign_optout": reddit.get("redesign_optout", ""),
                "extra_cookies": reddit.get("extra_cookies", {}),
                "proxy": reddit.get("proxy", ""),
            })
    except Exception as e:
        logger.warning(f"自动同步 Cookie 到服务端失败（不影响本次抓取）: {e}")


class PlatformCookieSaveRequest(BaseModel):
    platform: str                       # twitter | reddit
    ct0: str | None = None
    auth_token: str | None = None
    reddit_session: str | None = None
    reddit_token: str | None = None
    edgebucket: str | None = None
    redesign_optout: str | None = None
    extra_cookies: dict[str, str] | None = None
    proxy: str | None = None


@app.post("/api/platform-cookies")
async def save_platform_cookies(req: PlatformCookieSaveRequest):
    """保存平台 Cookie 到服务端（供定时任务使用）。"""
    platform = (req.platform or "").strip().lower()
    if platform not in ("twitter", "reddit"):
        raise HTTPException(400, "platform 必须为 twitter 或 reddit")
    cookies = {
        "ct0": req.ct0 or "", "auth_token": req.auth_token or "",
        "reddit_session": req.reddit_session or "", "reddit_token": req.reddit_token or "",
        "edgebucket": req.edgebucket or "", "redesign_optout": req.redesign_optout or "",
        "extra_cookies": req.extra_cookies or {}, "proxy": req.proxy or "",
    }
    await _save_platform_cookies_db(platform, cookies)
    return {"status": "ok", "message": f"{platform} Cookie 已保存到服务端"}


@app.get("/api/platform-cookies")
async def get_platform_cookies():
    """返回已持久化的平台 Cookie 配置状态。"""
    data = await _get_platform_cookies_db()
    status = {
        p: {"configured": bool(
            (c.get("ct0") and c.get("auth_token")) if p == "twitter" else c.get("reddit_session")
        ), "proxy": c.get("proxy", "")}
        for p, c in data.items()
    }
    return {"status": "ok", "cookies": data, "summary": status}


@app.post("/api/choose-folder")
async def choose_folder():
    """在 executor 线程中打开文件夹选择对话框，避免阻塞事件循环"""
    import asyncio

    def _pick():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected_path = filedialog.askdirectory(title="选择文件夹")
        root.destroy()
        return selected_path or ""

    selected = await asyncio.to_thread(_pick)
    return {"path": selected}


@app.get("/api/platforms")
async def get_platforms():
    return [p.value for p in Platform]


# ── 代理 Preflight（v1.5.3 新增）────────────────────────

class ProxyPreflightRequest(BaseModel):
    proxy: str = ""     # 代理 URL，如 http://127.0.0.1:7890
    target: str = "https://www.google.com/generate_204"  # 探测目标


@app.post("/api/preflight-proxy")
async def preflight_proxy(req: ProxyPreflightRequest):
    """
    在批量搜索前探测代理连通性，快速失败避免陷入 30 分钟超时。
    3 秒未响应视为失败。
    """
    import httpx as _hx
    proxy = req.proxy.strip()
    if proxy and not proxy.startswith(("http://", "https://", "socks5://")):
        proxy = f"http://{proxy}"
    try:
        async with _hx.AsyncClient(
            timeout=3.0,
            proxy=proxy or None,
            verify=False,
        ) as client:
            resp = await client.head(req.target)
            return {"status": "ok", "code": resp.status_code, "proxy": proxy}
    except _hx.ProxyError as e:
        return {"status": "error", "error": f"代理连接失败: {e}"}
    except _hx.ConnectError as e:
        return {"status": "error", "error": f"目标不可达: {e}"}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

from src.collection.api import router as collection_router
app.include_router(collection_router)
