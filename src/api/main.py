"""FastAPI 后端：API 路由 + 静态文件服务"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import uuid
import threading
import time
from contextlib import asynccontextmanager
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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


async def save_task_results_to_file(
    save_path: str,
    task_name: str,
    stats: dict,
    results: list[dict] | None = None
) -> str | None:
    """
    将任务结果保存到指定路径。
    
    Args:
        save_path: 保存目录的绝对路径
        task_name: 任务名称
        stats: 任务统计信息
        results: 详细结果数据（可选）
    
    Returns:
        保存的文件路径，如果保存失败返回 None
    """
    import os
    
    if not save_path:
        logger.warning("未指定保存路径，跳过文件保存")
        return None
    
    try:
        # 检查目录是否存在，不存在则创建
        if not os.path.exists(save_path):
            os.makedirs(save_path, exist_ok=True)
            logger.info(f"已创建保存目录: {save_path}")
        
        # 生成文件名
        filename = generate_export_filename(task_name, "csv")
        file_path = os.path.join(save_path, filename)
        
        # 保存为 CSV 格式
        with open(file_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            
            # 写入统计信息头部
            writer.writerow(["任务统计信息"])
            writer.writerow(["任务名称", task_name])
            writer.writerow(["执行时间", datetime.now().isoformat()])
            writer.writerow(["状态", stats.get("status", "unknown")])
            writer.writerow(["总帖子数", stats.get("total_posts", 0)])
            writer.writerow(["总关键词数", stats.get("total_keywords", 0)])
            writer.writerow([])  # 空行
            
            # 写入平台详情
            writer.writerow(["平台详情"])
            writer.writerow(["平台", "帖子数", "关键词数"])
            for platform_name, platform_stats in stats.get("platforms", {}).items():
                writer.writerow([
                    platform_name,
                    platform_stats.get("posts", 0),
                    platform_stats.get("keywords", 0)
                ])
            
            # 如果有详细结果，写入详细数据
            if results:
                writer.writerow([])  # 空行
                writer.writerow(["详细数据"])
                if results:
                    # 写入表头
                    headers = list(results[0].keys())
                    writer.writerow(headers)
                    # 写入数据行
                    for row in results:
                        writer.writerow([row.get(h, "") for h in headers])
        
        logger.info(f"任务结果已保存: {file_path}")
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
    except Exception as e:
        logger.error(f"Lifespan startup failed: {e}", exc_info=True)
        raise
    yield
    # 关闭共享浏览器实例
    try:
        from src.crawlers.browser_manager import BrowserManager
        await BrowserManager.get().close()
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

class MultiPlatformSearchRequest(BaseModel):
    keyword: str
    count: int = 50
    platforms: list[str] = ["twitter"]   
    sort_by: str = "top"                 
    include_replies: bool = False
    block_resources: bool = False        
    cookies: list[PlatformCookieConfig] = []  

class BatchSearchRequest(BaseModel):
    keywords: list[str]
    count: int = 50
    platforms: list[str] = ["twitter"]
    sort_by: str = "top"
    include_replies: bool = False
    block_resources: bool = False
    cookies: list[PlatformCookieConfig] = []


# ── API 路由 ─────────────────────────────────────────────

# 异步任务状态存储池（OrderedDict + TTL 自动清理，防止内存泄漏）
_BACKGROUND_TASKS: OrderedDict[str, dict] = OrderedDict()
_BG_TASKS_LOCK = threading.Lock()
_BG_TASKS_MAX = 500       # 最多保留 500 个已完成任务
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


# ── 模型配置持久化 ──────────────────────────────────────────

class ModelConfigData(BaseModel):
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    model_backup_base_url: str = ""
    model_backup_api_key: str = ""
    model_backup_name: str = ""


@app.get("/api/model-config")
async def get_model_config():
    """从数据库读取模型配置"""
    try:
        from src.common.database import get_db
        db = await get_db()
        cursor = await db.execute("SELECT config_key, config_value FROM model_config")
        rows = await cursor.fetchall()
        config = {row[0]: row[1] for row in rows}
        return {"status": "ok", "config": config}
    except Exception as e:
        logger.error(f"读取模型配置失败: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/api/model-config")
async def save_model_config(req: ModelConfigData):
    """保存模型配置到数据库"""
    try:
        from src.common.database import get_db
        db = await get_db()
        now = datetime.now().isoformat()
        fields = {
            "model_base_url": req.model_base_url,
            "model_api_key": req.model_api_key,
            "model_name": req.model_name,
            "model_backup_base_url": req.model_backup_base_url,
            "model_backup_api_key": req.model_backup_api_key,
            "model_backup_name": req.model_backup_name,
        }
        for key, value in fields.items():
            await db.execute(
                "INSERT INTO model_config (config_key, config_value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value, updated_at=excluded.updated_at",
                (key, value, now)
            )
        await db.commit()
        return {"status": "ok", "message": "配置已保存"}
    except Exception as e:
        logger.error(f"保存模型配置失败: {e}")
        return {"status": "error", "error": str(e)}


# ── 分析上次抓取数据（自动模式） ──────────────────────────────

# 缓存最近一次搜索的结果数据（task_id -> csv_data）
_LAST_SEARCH_DATA: dict[str, str] = {}
_LAST_SEARCH_LOCK = threading.Lock()


@app.post("/api/analyze-last")
async def analyze_last_data(
    workflows: str = Form(default="[]"),
    model_api_key: str = Form(default=""),
    model_base_url: str = Form(default=""),
    model_name: str = Form(default=""),
    model_backup_base_url: str = Form(default=""),
    model_backup_api_key: str = Form(default=""),
    model_backup_name: str = Form(default=""),
    opinion_detail: str = Form(default=""),
    opinion_rules: str = Form(default=""),
):
    """分析最近一次搜索抓取的数据（无需上传文件）"""
    import csv as _csv
    import traceback as _tb
    try:
        with _LAST_SEARCH_LOCK:
            if not _LAST_SEARCH_DATA:
                raise HTTPException(400, "没有可分析的抓取数据，请先完成一次搜索")
            # 取最新的一份
            latest_key = list(_LAST_SEARCH_DATA.keys())[-1]
            csv_data = _LAST_SEARCH_DATA[latest_key]

        # 解析 CSV 为帖子列表
        reader = _csv.DictReader(io.StringIO(csv_data))
        posts: list[ParsedPost] = []
        for row in reader:
            content = row.get("content", "") or ""
            if not content.strip():
                continue
            posts.append(ParsedPost(
                platform=row.get("platform", "unknown"),
                post_id=row.get("post_id", str(uuid.uuid4())[:12]),
                content=content,
                author=row.get("author", ""),
                published_at=_parse_date(row.get("created_at", "")),
                metrics={},
                tags=[],
                raw_data={"type": row.get("type", "post")},
            ))

        if not posts:
            raise HTTPException(400, "抓取数据中没有有效内容")

        # 解析工作流参数
        try:
            workflow_list = json.loads(workflows)
        except Exception:
            workflow_list = []

        if not (model_api_key and model_base_url):
            pipeline = Pipeline.from_config()
        else:
            pipeline = Pipeline.from_config_with_model(
                base_url=model_base_url, api_key=model_api_key, model=model_name,
                backup_model=model_backup_name,
                backup_base_url=model_backup_base_url or model_base_url,
                backup_api_key=model_backup_api_key or model_api_key,
            )

        stats = await pipeline.process_posts(
            posts, source="auto-analyze", task_name="自动分析",
            opinion_detail=opinion_detail, opinion_rules=opinion_rules,
        )
        return stats
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"自动分析异常: {e}\n{_tb.format_exc()}")
        raise HTTPException(500, f"分析过程出错: {str(e)}")


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
        return {"status": "ok", "tasks": tasks}
    except Exception as e:
        logger.error(f"获取定时任务失败: {e}")
        return {"status": "error", "error": str(e)}


@app.post("/api/scheduled-tasks")
async def create_scheduled_task(req: ScheduledTaskRequest):
    """创建或更新定时任务"""
    try:
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
async def run_scheduled_task_now(task_id: str):
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
        # 异步执行
        asyncio.create_task(_execute_scheduled_task(task))
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
        if _task_scheduler:
            _task_scheduler.shutdown(wait=False)
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
        cursor = await db.execute("SELECT id, name, cron_expression, params, save_path, workflows FROM scheduled_tasks WHERE enabled=1")
        rows = await cursor.fetchall()
        for row in rows:
            task_id, task_name, cron_expr, params_json, save_path, workflows_json = row
            task_config = {
                "id": task_id, "name": task_name,
                "params": json.loads(params_json) if params_json else {},
                "save_path": save_path, "workflows": json.loads(workflows_json) if workflows_json else [],
            }
            # 解析 cron
            parts = cron_expr.split()
            if len(parts) == 5:
                _task_scheduler.add_job(
                    _execute_scheduled_task, "cron",
                    minute=parts[0], hour=parts[1], day=parts[2],
                    month=parts[3], day_of_week=parts[4],
                    args=[task_config], id=f"task_{task_id}",
                    name=task_id, replace_existing=True,
                )
        _task_scheduler.start()
        logger.info(f"调度器已加载 {len(rows)} 个定时任务")
    except Exception as e:
        logger.error(f"加载定时任务失败: {e}")

async def _execute_scheduled_task(task_config: dict):
    """执行单个定时任务：抓取 → 保存 → 分析"""
    task_id = task_config.get("id", "unknown")
    task_name = task_config.get("name", "") or task_id
    logger.info(f"[定时任务 {task_id}] 开始执行，任务名: {task_name}")
    try:
        from src.common.database import get_db
        db = await get_db()
        now = datetime.now().isoformat()
        await db.execute("UPDATE scheduled_tasks SET last_run_at=?, last_run_status='running', last_error='' WHERE id=?",
                         (now, task_id))
        await db.commit()

        params = task_config.get("params", {})
        keywords = params.get("keywords", [])
        platform_names = params.get("platforms", ["twitter"])
        count = params.get("count", 50)
        sort_by = params.get("sort", "hot")
        include_replies = params.get("include_replies", False)
        opinion_detail = params.get("opinion_detail", "")
        opinion_rules = params.get("opinion_rules", "")

        # 构建平台枚举
        platform_map = {"twitter": Platform.TWITTER, "reddit": Platform.REDDIT}
        platform_list = [platform_map[p] for p in platform_names if p in platform_map]
        if not platform_list:
            platform_list = [Platform.TWITTER]

        query = CrawlQuery(
            platforms=platform_list,
            keywords=keywords or ["技术", "科技"],
            max_results=count,
            extra={"sort": sort_by, "include_replies": include_replies},
        )

        logger.info(f"[定时任务 {task_id}] 开始抓取，关键词: {query.keywords}")
        pipeline = Pipeline.from_config()
        stats = await pipeline.run(
            query, task_name=task_name,
            opinion_detail=opinion_detail, opinion_rules=opinion_rules,
        )

        # 保存结果到指定路径
        save_path = task_config.get("save_path", "")
        if save_path:
            saved_file = await save_task_results_to_file(
                save_path=save_path,
                task_name=task_name,
                stats=stats,
                results=None  # 暂时不保存详细结果，只保存统计信息
            )
            if saved_file:
                logger.info(f"[定时任务 {task_id}] 结果已保存: {saved_file}")

        await db.execute("UPDATE scheduled_tasks SET last_run_status=?, last_error='' WHERE id=?",
                         (stats.get("status", "unknown"), task_id))
        await db.commit()
        logger.info(f"[定时任务 {task_id}] 执行完成: {stats.get('status')}, "
                     f"帖子: {stats.get('total_posts', 0)}, 关键词: {stats.get('total_keywords', 0)}")
    except Exception as e:
        logger.error(f"[定时任务 {task_id}] 执行失败: {e}")
        try:
            from src.common.database import get_db
            db = await get_db()
            await db.execute("UPDATE scheduled_tasks SET last_run_status='failed', last_error=? WHERE id=?",
                             (str(e), task_id))
            await db.commit()
        except Exception:
            pass


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
    """触发采集流水线"""
    platforms = []
    for p in req.platforms:
        try:
            platforms.append(Platform(p))
        except ValueError:
            raise HTTPException(400, f"未知平台: {p}，可选: {[p.value for p in Platform]}")

    keywords = req.keywords or ["技术", "科技"]
    query = CrawlQuery(platforms=platforms, keywords=keywords, max_results=req.max_results)

    pipeline = Pipeline.from_config()
    stats = await pipeline.run(query, task_name="手动搜索")
    return stats


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
        return HTMLResponse(f"<pre>{content}</pre>")
    elif format == "txt":
        content = await manager.export_txt(vocab_status)
        return HTMLResponse(f"<pre>{content}</pre>")
    else:
        raise HTTPException(400, f"不支持的格式: {format}")


@app.post("/api/import")
async def import_data(
    file: UploadFile = File(...), platform: str = "unknown",
    model_api_key: str = Form(default=""), model_base_url: str = Form(default=""),
    model_name: str = Form(default=""), model_backup: str = Form(default=""),
    model_backup_base_url: str = Form(default=""), model_backup_api_key: str = Form(default=""),
    model_backup_name: str = Form(default=""), import_mode: str = Form(default="manual"),
    type_post: str = Form(default="true"), type_comment: str = Form(default="true"),
    include_author: str = Form(default="false"),
    opinion_detail: str = Form(default=""), opinion_rules: str = Form(default=""),
):
    """导入数据文件（JSON/CSV），走工作流提取关键词"""
    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in ("json", "csv"):
        raise HTTPException(400, f"不支持的文件格式: {ext}，请上传 .json 或 .csv 文件")

    content = await file.read()

    posts: list[ParsedPost] = []
    try:
        if ext == "json":
            data = json.loads(content.decode("utf-8-sig"))
            if isinstance(data, dict):
                data = data.get("data", data.get("items", data.get("posts", [data])))
            if not isinstance(data, list):
                data = [data]
            for i, item in enumerate(data):
                if isinstance(item, str): item = {"content": item}
                post_type = item.get("type", "post")
                posts.append(ParsedPost(
                    platform=item.get("platform", platform),
                    post_id=str(item.get("id", item.get("post_id", str(uuid.uuid4())[:12]))),
                    content=str(item.get("content", item.get("text", item.get("body", "")))),
                    author=str(item.get("author", item.get("user", item.get("username", "unknown")))),
                    published_at=_parse_date(item.get("published_at", item.get("created_at", item.get("date", "")))),
                    metrics=item.get("metrics", {}),
                    tags=item.get("tags", []),
                    raw_data={"type": post_type},
                ))

        elif ext == "csv":
            text = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            for i, row in enumerate(reader):
                post_type = row.get("type", "post")
                posts.append(ParsedPost(
                    platform=row.get("platform", platform),
                    post_id=str(row.get("id", row.get("post_id", str(uuid.uuid4())[:12]))),
                    content=str(row.get("content", row.get("text", row.get("body", "")))),
                    author=str(row.get("author", row.get("user", row.get("username", "unknown")))),
                    published_at=_parse_date(row.get("published_at", row.get("created_at", row.get("date", "")))),
                    metrics={},
                    tags=[],
                    raw_data={"type": post_type},
                ))
    except Exception as e:
        raise HTTPException(400, f"文件解析失败: {str(e)}")

    if not posts:
        raise HTTPException(400, "文件中没有找到有效数据")

    want_post = type_post.lower() == "true"
    want_comment = type_comment.lower() == "true"
    filtered_posts = []
    for p in posts:
        ptype = p.raw_data.get("type", "post")
        if ptype == "post" and want_post: filtered_posts.append(p)
        elif ptype == "comment" and want_comment: filtered_posts.append(p)
        elif ptype not in ("post", "comment"): filtered_posts.append(p)
    posts = filtered_posts

    posts = [p for p in posts if p.content.strip()]
    if not posts: raise HTTPException(400, "文件中没有有效的帖子内容")

    if include_author.lower() != "true":
        for p in posts: p.author = ""

    if import_mode.lower() == "manual" or not (model_api_key and model_base_url):
        pipeline = Pipeline.from_config()
    else:
        pipeline = Pipeline.from_config_with_model(
            base_url=model_base_url, api_key=model_api_key, model=model_name,
            backup_model=model_backup_name or model_backup,
            backup_base_url=model_backup_base_url or model_base_url,
            backup_api_key=model_backup_api_key or model_api_key,
        )

    stats = await pipeline.process_posts(
        posts, source=f"import:{filename}", task_name=f"导入:{filename}",
        opinion_detail=opinion_detail, opinion_rules=opinion_rules,
    )
    return stats

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
    """物理隔离线程池版：用户主页抓取"""
    if not req.urls:
        raise HTTPException(400, "请提供至少一个 Twitter/X 用户主页链接")

    task_id = str(uuid.uuid4())
    _set_task_status(task_id, {"status": "running", "result": None})

    def _thread_target():
        import asyncio
        import base64
        import traceback as tb
        from src.crawlers.twitter_url import TwitterCookieFetcher, extract_username

        # 在新线程中开启独立的事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            try:
                fetcher = TwitterCookieFetcher(proxy=req.proxy or None, block_resources=req.block_resources)
                if req.ct0 and req.auth_token:
                    cookies = {"ct0": req.ct0.strip(), "auth_token": req.auth_token.strip()}
                else:
                    cookies = fetcher.get_cookies()

                if not cookies:
                    _set_task_status(task_id, {"status": "error", "error": "未配置 Cookie，请在页面中填写"})
                    return

                usernames: list[str] = []
                for url in req.urls:
                    name = extract_username(url.strip())
                    if name and name not in usernames:
                        usernames.append(name)

                if not usernames:
                    _set_task_status(task_id, {"status": "error", "error": "未能提取有效用户名"})
                    return

                tweets, csv_string = await fetcher.fetch_user_tweets(usernames, count=req.count, include_replies=req.include_replies, cookies=cookies)
                
                if not tweets:
                    _set_task_status(task_id, {
                        "status": "success",
                        "result": {"status": "empty", "total_posts": 0, "sampled_posts": [], "csv_data": "", "error": "未抓取到任何推文"}
                    })
                    return

                sampled = []
                for t in tweets[:50]:
                    sampled.append({
                        "platform": "twitter", "post_id": t["tweet_id"], "author": t["author"],
                        "content": t["content"][:200] if len(t["content"]) > 200 else t["content"],
                        "published_at": t["created_at"], "metrics": {"likes": t["likes"], "retweets": t["retweets"], "replies": t["replies"]},
                        "has_media": t.get("has_media", False), "media_type": t.get("media_type", "none"),
                        "media_urls": t.get("media_urls", ""), "replies_count": t.get("replies_count", 0),
                    })

                csv_b64 = base64.b64encode(csv_string.encode("utf-8-sig")).decode("ascii")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                _set_task_status(task_id, {
                    "status": "success",
                    "result": {"status": "success", "total_posts": len(tweets), "sampled_posts": sampled, "csv_data": csv_b64, "csv_filename": f"tweets_{timestamp}.csv"}
                })
            except Exception as e:
                tb.print_exc()
                _set_task_status(task_id, {"status": "error", "error": str(e)})

        loop.run_until_complete(_run())
        loop.close()

    threading.Thread(target=_thread_target, daemon=True).start()
    return {"status": "started", "task_id": task_id}


@app.post("/api/search")
async def multi_platform_search(req: MultiPlatformSearchRequest):
    """物理隔离线程池版：统一多平台搜索"""
    if not req.keyword.strip():
        raise HTTPException(400, "请输入搜索关键词")
    if not req.platforms:
        raise HTTPException(400, "请至少选择一个搜索平台")

    task_id = str(uuid.uuid4())
    _set_task_status(task_id, {"status": "running", "result": None})

    def _thread_target():
        import asyncio
        import base64
        import traceback as tb
        import io
        import csv
        from src.crawlers.twitter_url import TwitterCookieFetcher
        from src.crawlers.reddit_crawler import RedditCookieFetcher

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            try:
                cookie_map: dict[str, dict] = {}
                for cfg in req.cookies:
                    cookie_map[cfg.platform] = {
                        "ct0": cfg.ct0 or "", "auth_token": cfg.auth_token or "",
                        "reddit_session": cfg.reddit_session or "", "reddit_token": cfg.reddit_token or "",
                        "edgebucket": cfg.edgebucket or "", "redesign_optout": cfg.redesign_optout or "",
                        "extra_cookies": cfg.extra_cookies or {}, "proxy": cfg.proxy or "",
                    }

                tasks = []
                platform_names = []
                skipped_platforms: list[dict] = []

                for platform in req.platforms:
                    pc = cookie_map.get(platform, {})
                    proxy = pc.get("proxy") or None
                    if proxy and not proxy.startswith(("http://", "https://", "socks5://")): proxy = f"http://{proxy}"

                    if platform == "twitter":
                        if not pc.get("ct0") or not pc.get("auth_token"):
                            skipped_platforms.append({"platform": "twitter", "reason": "Cookie 未配置"})
                            continue
                        fetcher = TwitterCookieFetcher(proxy=proxy, block_resources=req.block_resources)
                        cookies = {"ct0": pc["ct0"], "auth_token": pc["auth_token"]}
                        tasks.append(fetcher.search_tweets(req.keyword.strip(), count=req.count, include_replies=req.include_replies, cookies=cookies, sort_by=req.sort_by, task_id=task_id))
                        platform_names.append("twitter")

                    elif platform == "reddit":
                        if not pc.get("reddit_session"):
                            skipped_platforms.append({"platform": "reddit", "reason": "Cookie 未配置"})
                            continue
                        fetcher = RedditCookieFetcher(proxy=proxy)
                        cookies = {k: pc[k] for k in ("reddit_session", "reddit_token", "edgebucket", "redesign_optout") if pc.get(k)}
                        if pc.get("extra_cookies"): cookies.update(pc["extra_cookies"])
                        tasks.append(fetcher.search_posts(req.keyword.strip(), count=req.count, cookies=cookies, include_replies=req.include_replies, sort="hot" if req.sort_by == "top" else "new", task_id=task_id))
                        platform_names.append("reddit")

                if not tasks:
                    _set_task_status(task_id, {"status": "error", "error": "所选平台均未配置 Cookie，请先填写"})
                    return

                # 按平台分别计算超时（X:30分钟/R:20分钟），仅作为兜底保护
                search_timeout = (
                    (_TC.single_keyword_timeout if "twitter" in platform_names else 0)
                    + (_RC.single_keyword_timeout if "reddit" in platform_names else 0)
                )
                search_timeout = max(search_timeout, 120)  # 最低 2 分钟保底
                
                # 使用可中断的循环代替 asyncio.gather（支持取消操作）
                was_cancelled = False
                results = [None] * len(tasks)
                task_to_idx = {}
                pending_tasks = set()
                for i, t in enumerate(tasks):
                    atask = asyncio.create_task(t)
                    task_to_idx[atask] = i
                    pending_tasks.add(atask)
                
                while pending_tasks:
                    # 检查取消标记
                    if _is_task_cancelled(task_id):
                        was_cancelled = True
                        logger.info(f"搜索任务被取消，正在终止 {len(pending_tasks)} 个进行中子任务: {task_id}")
                        for task in pending_tasks:
                            task.cancel()
                        # 等待已取消任务清理（给 3 秒）
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*pending_tasks, return_exceptions=True),
                                timeout=3.0
                            )
                        except asyncio.TimeoutError:
                            pass
                        break
                    
                    # 等待任意一个任务完成（最多等 2 秒后再次检查取消标记）
                    done, pending_tasks = await asyncio.wait(
                        pending_tasks,
                        timeout=2.0,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for completed_task in done:
                        idx = task_to_idx.get(completed_task)
                        if idx is not None:
                            try:
                                results[idx] = completed_task.result()
                            except asyncio.CancelledError:
                                logger.info(f"子任务 {idx} 已被取消")
                                results[idx] = None
                            except Exception as e:
                                logger.error(f"子任务 {idx} 异常: {e}")
                                results[idx] = e
                
                if was_cancelled:
                    # 取消时保存已获取的数据
                    logger.info(f"搜索任务已取消，保存已获取的数据: {task_id}")
                
                if not results and not was_cancelled:
                    _set_task_status(task_id, {"status": "error", "error": f"搜索超时（{search_timeout}秒），请减少关键词数量或稍后重试"})
                    return

                all_posts: list[dict] = []
                errors: list[str] = []
                platform_counts: dict[str, int] = {}

                for plat, result in zip(platform_names, results):
                    if isinstance(result, Exception):
                        error_msg = f"{plat}: {result}"
                        errors.append(error_msg)
                        logger.error(f"平台 {plat} 抓取出错: {result}\n{tb.format_exc()}")
                        tb.print_exc()
                        continue
                    posts, csv_string = result
                    if not posts:
                        logger.warning(f"平台 {plat} 返回空结果")
                    else:
                        logger.info(f"平台 {plat} 返回 {len(posts)} 条结果")
                    platform_counts[plat] = sum(1 for p in posts if p.get("type", "post") != "comment")
                    for p in posts: p["platform"] = plat
                    all_posts.extend(posts)

                if not all_posts:
                    error_msg = "、".join(errors) if errors else "所有平台均未返回结果"
                    _set_task_status(task_id, {"status": "success", "result": {"status": "empty", "total_posts": 0, "sampled_posts": [], "csv_data": "", "csv_filename": "", "error": f"搜索无结果。{error_msg}", "platform_counts": platform_counts, "skipped_platforms": skipped_platforms}})
                    return

                sampled = []
                for t in all_posts:
                    if t.get("type") == "comment": continue
                    platform = t.get("platform", "unknown")
                    if platform == "twitter":
                        sampled.append({"platform": "twitter", "post_id": t.get("tweet_id", ""), "author": t.get("author_name", "") or t.get("author", ""), "content": t.get("content", "")[:200], "published_at": t.get("created_at", ""), "metrics": {"likes": t.get("likes", 0), "retweets": t.get("retweets", 0), "replies": t.get("replies", 0)}, "has_media": t.get("has_media", False), "media_type": t.get("media_type", "none"), "media_urls": t.get("media_urls", ""), "replies_count": t.get("replies_count", 0)})
                    elif platform == "reddit":
                        sampled.append({"platform": "reddit", "post_id": t.get("post_id", ""), "author": t.get("author", ""), "content": (t.get("title", "") + "\n" + t.get("content", ""))[:200].strip(), "published_at": t.get("created_at", ""), "metrics": {"score": t.get("score", 0), "comments": t.get("num_comments", 0)}, "has_media": t.get("has_media", False), "media_type": t.get("media_type", "none"), "media_urls": t.get("media_urls", ""), "replies_count": t.get("comments_fetched", 0)})
                    if len(sampled) >= 500: break

                csv_buf = io.StringIO()
                csv_buf.write("\ufeff")
                all_csv_fields = ["platform", "type", "post_id", "parent_id", "author", "commenter", "content", "created_at", "url", "likes", "retweets", "replies", "score", "num_comments", "has_media", "media_type", "media_urls"]
                writer = csv.DictWriter(csv_buf, fieldnames=all_csv_fields, extrasaction="ignore")
                writer.writeheader()
                for t in all_posts:
                    plat, row_type = t.get("platform", ""), t.get("type", "post")
                    if plat == "twitter":
                        row = dict(t)
                        row["post_id"] = t.get("tweet_id", "")
                        row.setdefault("parent_id", "")
                        row.setdefault("commenter", "")
                        row["author"] = t.get("author_name", "") or t.get("author", "")
                        writer.writerow(row)
                        replies_raw = t.get("replies_data", "[]")
                        if replies_raw and replies_raw != "[]":
                            try:
                                import json as _json
                                replies = _json.loads(replies_raw) if isinstance(replies_raw, str) else replies_raw
                                for reply in replies:
                                    writer.writerow({"platform": "twitter", "type": "comment", "post_id": reply.get("tweet_id", ""), "parent_id": t.get("tweet_id", ""), "author": reply.get("display_name", ""), "commenter": reply.get("display_name", ""), "content": reply.get("content", ""), "created_at": reply.get("created_at", ""), "has_media": reply.get("has_media", False), "media_type": reply.get("media_type", "none"), "media_urls": reply.get("media_urls", "")})
                            except Exception: pass
                    elif plat == "reddit":
                        row = dict(t)
                        if row_type == "post": row["content"] = (t.get("title", "") + "\n" + t.get("content", "")).strip()
                        writer.writerow(row)
                    else:
                        writer.writerow(t)

                csv_b64 = base64.b64encode(csv_buf.getvalue().encode("utf-8-sig")).decode("ascii")
                # 缓存搜索结果供自动分析使用
                with _LAST_SEARCH_LOCK:
                    _LAST_SEARCH_DATA[task_id] = csv_buf.getvalue()
                    # 只保留最近 3 份
                    while len(_LAST_SEARCH_DATA) > 3:
                        _LAST_SEARCH_DATA.pop(next(iter(_LAST_SEARCH_DATA)))
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                result_status = "cancelled" if was_cancelled else "success"
                result_msg = "搜索已取消，数据已保存" if was_cancelled else None
                _set_task_status(task_id, {"status": "success", "result": {"status": result_status, "total_posts": sum(1 for p in all_posts if p.get("type", "post") != "comment"), "total_rows": len(all_posts), "sampled_posts": sampled, "csv_data": csv_b64, "csv_filename": f"multi_search_{timestamp}.csv", "platform_counts": platform_counts, "skipped_platforms": skipped_platforms, "errors": errors, "message": result_msg}})
            except Exception as e:
                tb.print_exc()
                _set_task_status(task_id, {"status": "error", "error": str(e)})
            finally:
                _remove_cancelled(task_id)

        loop.run_until_complete(_run())
        loop.close()

    threading.Thread(target=_thread_target, daemon=True).start()
    return {"status": "started", "task_id": task_id}


@app.post("/api/batch-search")
async def batch_search(req: BatchSearchRequest):
    """物理隔离线程池版：批量关键词搜索"""
    if not req.keywords: raise HTTPException(400, "请提供至少一个关键词")
    if len(req.keywords) > 10: raise HTTPException(400, f"关键词数量过多（{len(req.keywords)} 个），单次最多 10 个")

    task_id = str(uuid.uuid4())
    _set_task_status(task_id, {"status": "running", "result": None})

    def _thread_target():
        import asyncio
        import base64
        import traceback as tb
        import json as _json
        import io
        import csv
        from src.crawlers.twitter_url import TwitterCookieFetcher
        from src.crawlers.reddit_crawler import RedditCookieFetcher

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            try:
                cookie_map: dict[str, dict] = {}
                for cfg in req.cookies:
                    cookie_map[cfg.platform] = {"ct0": cfg.ct0 or "", "auth_token": cfg.auth_token or "", "reddit_session": cfg.reddit_session or "", "reddit_token": cfg.reddit_token or "", "edgebucket": cfg.edgebucket or "", "redesign_optout": cfg.redesign_optout or "", "extra_cookies": cfg.extra_cookies or {}, "proxy": cfg.proxy or ""}

                all_rows: list[dict] = []; keyword_results: list[dict] = []; errors: list[str] = []; platform_counts: dict[str, int] = {}; skipped_platforms: list[dict] = []

                for platform in req.platforms:
                    pc = cookie_map.get(platform, {})
                    if platform == "twitter" and (not pc.get("ct0") or not pc.get("auth_token")): skipped_platforms.append({"platform": "twitter", "reason": "Cookie 未配置"})
                    elif platform == "reddit" and not pc.get("reddit_session"): skipped_platforms.append({"platform": "reddit", "reason": "Cookie 未配置"})

                total_keywords = len(req.keywords)
                sem = asyncio.Semaphore(3)

                async def _search_one_keyword(kw: str, idx: int):
                    async with sem:
                        tasks = []; plat_names = []
                        for platform in req.platforms:
                            pc = cookie_map.get(platform, {})
                            proxy = pc.get("proxy") or None
                            if proxy and not proxy.startswith(("http://", "https://", "socks5://")): proxy = f"http://{proxy}"

                            if platform == "twitter" and pc.get("ct0") and pc.get("auth_token"):
                                fetcher = TwitterCookieFetcher(proxy=proxy, block_resources=req.block_resources)
                                cookies = {"ct0": pc["ct0"], "auth_token": pc["auth_token"]}
                                tasks.append(fetcher.search_tweets(kw, count=req.count, include_replies=req.include_replies, cookies=cookies, sort_by=req.sort_by))
                                plat_names.append("twitter")

                            elif platform == "reddit" and pc.get("reddit_session"):
                                fetcher = RedditCookieFetcher(proxy=proxy)
                                cookies = {k: pc[k] for k in ("reddit_session", "reddit_token", "edgebucket", "redesign_optout") if pc.get(k)}
                                if pc.get("extra_cookies"): cookies.update(pc["extra_cookies"])
                                tasks.append(fetcher.search_posts(kw, count=req.count, cookies=cookies, include_replies=req.include_replies, sort="hot" if req.sort_by == "top" else "new"))
                                plat_names.append("reddit")

                        if not tasks: return [], {"keyword": kw, "post_count": 0, "total_rows": 0}, [f"「{kw}」: 无可用平台 Cookie"]
                        # 根据涉及的平台累加超时时间（双平台时给足两平台时间）
                        kw_timeout = (
                            (_TC.single_keyword_timeout if "twitter" in plat_names else 0)
                            + (_RC.single_keyword_timeout if "reddit" in plat_names else 0)
                        )
                        kw_timeout = max(kw_timeout, 60)  # 最低 60 秒保底
                        try:
                            results = await asyncio.wait_for(
                                asyncio.gather(*tasks, return_exceptions=True),
                                timeout=kw_timeout
                            )
                        except asyncio.TimeoutError:
                            return [], {"keyword": kw, "post_count": 0, "total_rows": 0}, [f"「{kw}」: 搜索超时（{kw_timeout}秒）"]
                        kw_rows, kw_errors, kw_post_count = [], [], 0
                        for plat, result in zip(plat_names, results):
                            if isinstance(result, Exception):
                                kw_errors.append(f"「{kw}」{plat}: {result}")
                                continue
                            posts, csv_string = result
                            for p in posts:
                                p["platform"] = plat; p["keyword"] = kw
                            kw_rows.extend(posts)
                            kw_post_count += sum(1 for p in posts if p.get("type", "post") != "comment")
                        return kw_rows, {"keyword": kw, "post_count": kw_post_count, "total_rows": len([x for x in results if not isinstance(x, Exception) and x[0]]) if results else 0}, kw_errors

                valid_keywords = [(kw.strip(), i) for i, kw in enumerate(req.keywords) if kw.strip()]
                # 批量搜索整体超时保护：按平台分别累加，分别封顶（X:60分钟/R:40分钟）
                twitter_kw_count = sum(1 for kw, _ in valid_keywords if "twitter" in req.platforms)
                reddit_kw_count = sum(1 for kw, _ in valid_keywords if "reddit" in req.platforms)
                twitter_batch_cap = _TC.batch_max_timeout  # 3600秒 = 60分钟
                reddit_batch_cap = _RC.batch_max_timeout    # 2400秒 = 40分钟
                twitter_timeout = min(twitter_kw_count * _TC.single_keyword_timeout, twitter_batch_cap) if twitter_kw_count > 0 else 0
                reddit_timeout = min(reddit_kw_count * _RC.single_keyword_timeout, reddit_batch_cap) if reddit_kw_count > 0 else 0
                batch_timeout = max(twitter_timeout + reddit_timeout, 120)  # 最低 2 分钟保底
                all_kw_results = []
                was_cancelled = False
                try:
                    # 使用 asyncio.wait 以便在取消时能立即返回已完成的结果
                    batch_tasks = [asyncio.create_task(_search_one_keyword(kw, idx)) for kw, idx in valid_keywords]
                    done_batch, pending_batch = await asyncio.wait(
                        batch_tasks,
                        timeout=batch_timeout
                    )
                    # 取消未完成的任务
                    for t in pending_batch:
                        t.cancel()
                    if pending_batch:
                        await asyncio.gather(*pending_batch, return_exceptions=True)
                    
                    # 检查是否被用户取消
                    was_cancelled = _is_task_cancelled(task_id)
                    if was_cancelled:
                        logger.info(f"批量搜索任务已取消，保存已获取的数据: {task_id}")
                    
                    # 收集已完成的结果
                    for t in done_batch:
                        try:
                            all_kw_results.append(t.result())
                        except Exception as e:
                            all_kw_results.append(e)
                    
                    if not was_cancelled and not all_kw_results and pending_batch:
                        errors.append(f"批量搜索超时（{batch_timeout}秒），部分关键词可能未完成")
                except asyncio.TimeoutError:
                    # 批量超时也要返回已获取的部分数据，而不是直接报错
                    logger.warning(f"批量搜索超时（{batch_timeout}秒），返回已获取的部分数据")
                    errors.append(f"批量搜索超时（{batch_timeout}秒），部分关键词可能未完成")

                for result in all_kw_results:
                    if isinstance(result, Exception):
                        errors.append(f"关键词任务异常: {result}")
                        logger.error(f"关键词任务异常: {result}", exc_info=result)
                        continue
                    if not isinstance(result, tuple) or len(result) != 3:
                        errors.append(f"关键词任务返回格式错误: {type(result)}")
                        continue
                    kw_rows, kw_result, kw_errors = result
                    all_rows.extend(kw_rows); keyword_results.append(kw_result); errors.extend(kw_errors)
                    for p in kw_rows:
                        plat = p.get("platform", "")
                        if p.get("type", "post") != "comment": platform_counts[plat] = platform_counts.get(plat, 0) + 1

                if not all_rows:
                    _set_task_status(task_id, {"status": "success", "result": {"status": "empty", "total_posts": 0, "total_rows": 0, "keyword_results": keyword_results, "sampled_posts": [], "csv_data": "", "csv_filename": "", "error": f"批量搜索无结果。{'、'.join(errors)}", "skipped_platforms": skipped_platforms}})
                    return

                sampled = []
                for t in all_rows:
                    if t.get("type") == "comment": continue
                    plat = t.get("platform", "unknown")
                    if plat == "twitter": sampled.append({"platform": "twitter", "post_id": t.get("tweet_id", ""), "author": t.get("author_name", "") or t.get("author", ""), "content": t.get("content", "")[:200], "published_at": t.get("created_at", ""), "keyword": t.get("keyword", ""), "metrics": {"likes": t.get("likes", 0), "retweets": t.get("retweets", 0), "replies": t.get("replies", 0)}, "has_media": t.get("has_media", False), "media_type": t.get("media_type", "none"), "media_urls": t.get("media_urls", ""), "replies_count": t.get("replies_count", 0)})
                    elif plat == "reddit": sampled.append({"platform": "reddit", "post_id": t.get("post_id", ""), "author": t.get("author", ""), "content": (t.get("title", "") + "\n" + t.get("content", ""))[:200].strip(), "published_at": t.get("created_at", ""), "keyword": t.get("keyword", ""), "metrics": {"score": t.get("score", 0), "comments": t.get("num_comments", 0)}, "has_media": t.get("has_media", False), "media_type": t.get("media_type", "none"), "media_urls": t.get("media_urls", ""), "replies_count": t.get("comments_fetched", 0)})
                    if len(sampled) >= 500: break

                csv_buf = io.StringIO(); csv_buf.write("\ufeff")
                all_csv_fields = ["keyword", "platform", "type", "post_id", "parent_id", "author", "commenter", "content", "created_at", "url", "likes", "retweets", "replies", "score", "num_comments", "has_media", "media_type", "media_urls"]
                writer = csv.DictWriter(csv_buf, fieldnames=all_csv_fields, extrasaction="ignore"); writer.writeheader()
                for t in all_rows:
                    plat, row_type = t.get("platform", ""), t.get("type", "post")
                    if plat == "twitter":
                        row = dict(t); row["post_id"] = t.get("tweet_id", ""); row.setdefault("parent_id", ""); row.setdefault("commenter", ""); row["author"] = t.get("author_name", "") or t.get("author", ""); writer.writerow(row)
                        replies_raw = t.get("replies_data", "[]")
                        if replies_raw and replies_raw != "[]":
                            try:
                                replies = _json.loads(replies_raw) if isinstance(replies_raw, str) else replies_raw
                                for reply in replies:
                                    writer.writerow({"keyword": t.get("keyword", ""), "platform": "twitter", "type": "comment", "post_id": reply.get("tweet_id", ""), "parent_id": t.get("tweet_id", ""), "author": reply.get("display_name", ""), "commenter": reply.get("display_name", ""), "content": reply.get("content", ""), "created_at": reply.get("created_at", ""), "has_media": reply.get("has_media", False), "media_type": reply.get("media_type", "none"), "media_urls": reply.get("media_urls", "")})
                            except Exception: pass
                    elif plat == "reddit":
                        row = dict(t)
                        if row_type == "post":
                            row["content"] = (t.get("title", "") + "\n" + t.get("content", "")).strip()
                        writer.writerow(row)
                    else: writer.writerow(t)

                csv_b64 = base64.b64encode(csv_buf.getvalue().encode("utf-8-sig")).decode("ascii")
                # 缓存批量搜索结果供自动分析使用
                with _LAST_SEARCH_LOCK:
                    _LAST_SEARCH_DATA[task_id] = csv_buf.getvalue()
                    while len(_LAST_SEARCH_DATA) > 3:
                        _LAST_SEARCH_DATA.pop(next(iter(_LAST_SEARCH_DATA)))
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                result_status = "cancelled" if was_cancelled else "success"
                result_msg = "搜索已取消，数据已保存" if was_cancelled else None
                _set_task_status(task_id, {"status": "success", "result": {"status": result_status, "total_posts": sum(1 for p in all_rows if p.get("type", "post") != "comment"), "total_rows": len(all_rows), "keyword_results": keyword_results, "sampled_posts": sampled, "csv_data": csv_b64, "csv_filename": f"batch_search_{timestamp}.csv", "skipped_platforms": skipped_platforms, "errors": errors, "message": result_msg}})
            except Exception as e:
                tb.print_exc()
                _set_task_status(task_id, {"status": "error", "error": str(e)})
            finally:
                _remove_cancelled(task_id)

        loop.run_until_complete(_run())
        loop.close()

    threading.Thread(target=_thread_target, daemon=True).start()
    return {"status": "started", "task_id": task_id}


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
        _app_module._log("Starting Chromium installation...")
        _chromium_install_state["progress"] = "downloading"

        commands = [
            ["python3", "-m", "playwright", "install", "chromium"],
            ["playwright", "install", "chromium"],
        ]

        for cmd in commands:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300
                )
                if result.returncode == 0:
                    _chromium_install_state["status"] = "installed"
                    _chromium_install_state["progress"] = "complete"
                    _app_module._NEEDS_CHROMIUM_INSTALL = False
                    _app_module._log("Chromium installation succeeded")
                    return
                else:
                    _app_module._log(f"Command failed: {' '.join(cmd)}: {result.stderr}")
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                _app_module._log(f"Command timed out: {' '.join(cmd)}")
                continue
            except Exception as e:
                _app_module._log(f"Install error: {e}")

        _chromium_install_state["status"] = "error"
        _chromium_install_state["error"] = "Installation failed. Please run 'playwright install chromium' in Terminal."
        _app_module._log("Chromium installation failed")

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
