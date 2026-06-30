"""FastAPI 后端：API 路由 + 静态文件服务"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import uuid
from contextlib import asynccontextmanager
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
from src.orchestrator.pipeline import Pipeline
from src.vocabulary.manager import VocabManager


# ── Lifespan ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db_path = Path(settings.app.data_dir) / "vocab.db"
    await init_db(db_path)
    yield
    # 关闭共享浏览器实例
    from src.crawlers.browser_manager import BrowserManager
    await BrowserManager.get().close()
    await close_db()


# ── App ───────────────────────────────────────────────────

app = FastAPI(title="vocab-harvester", version=get_version(), lifespan=lifespan)

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


class ExportRequest(BaseModel):
    format: str = "json"  # json | csv | txt
    status: str | None = None


# ── API 路由 ─────────────────────────────────────────────

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
    stats = await pipeline.run(query)
    return stats


@app.get("/api/vocabulary")
async def query_vocabulary(
    search: str | None = None,
    category: str | None = None,
    status: str | None = None,
    platform: str | None = None,
    action: str | None = None,
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
        limit=limit,
        offset=offset,
    )

    # 前端按 action 过滤
    if action:
        entries = [e for e in entries if e.get("action") == action]

    total = await manager.storage.count(vocab_status)
    return {"items": entries, "total": total, "limit": limit, "offset": offset}


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
    success = 0
    failed = 0
    for item in req.items:
        try:
            if req.action == "approve":
                ok = await manager.approve(item.word, item.category)
            elif req.action == "reject":
                ok = await manager.reject(item.word, item.category)
            else:
                failed += 1
                continue
            if ok:
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"ok": True, "action": req.action, "success": success, "failed": failed, "total": len(req.items)}


@app.post("/api/vocabulary/batch-delete")
async def batch_delete(req: BatchDeleteRequest):
    """批量删除词条"""
    manager = VocabManager()
    success = 0
    failed = 0
    for item in req.items:
        try:
            ok = await manager.delete(item.word, item.category)
            if ok:
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"ok": True, "success": success, "failed": failed, "total": len(req.items)}


class CsvDownloadRequest(BaseModel):
    csv_data: str       # base64-encoded CSV
    filename: str = "data.csv"


@app.post("/api/download-csv")
async def download_csv(req: CsvDownloadRequest):
    """将 base64 CSV 数据作为文件下载返回（兼容 PyWebView）"""
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
async def export_vocabulary(
    format: str = "json",
    status: str | None = None,
):
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
    file: UploadFile = File(...),
    platform: str = "unknown",
    model_api_key: str = Form(default=""),
    model_base_url: str = Form(default=""),
    model_name: str = Form(default=""),
    model_backup: str = Form(default=""),
    model_backup_base_url: str = Form(default=""),
    model_backup_api_key: str = Form(default=""),
    model_backup_name: str = Form(default=""),
    import_mode: str = Form(default="manual"),
    type_post: str = Form(default="true"),
    type_comment: str = Form(default="true"),
    include_author: str = Form(default="false"),
):
    """导入数据文件（JSON/CSV），走工作流提取关键词"""
    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in ("json", "csv"):
        raise HTTPException(400, f"不支持的文件格式: {ext}，请上传 .json 或 .csv 文件")

    content = await file.read()

    # 解析文件
    posts: list[ParsedPost] = []
    try:
        if ext == "json":
            data = json.loads(content.decode("utf-8-sig"))
            if isinstance(data, dict):
                data = data.get("data", data.get("items", data.get("posts", [data])))
            if not isinstance(data, list):
                data = [data]
            for i, item in enumerate(data):
                if isinstance(item, str):
                    item = {"content": item}
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

    # 按 type 筛选
    want_post = type_post.lower() == "true"
    want_comment = type_comment.lower() == "true"
    filtered_posts = []
    for p in posts:
        ptype = p.raw_data.get("type", "post")
        if ptype == "post" and want_post:
            filtered_posts.append(p)
        elif ptype == "comment" and want_comment:
            filtered_posts.append(p)
        elif ptype not in ("post", "comment"):
            filtered_posts.append(p)  # 未知类型保留
    posts = filtered_posts

    # 过滤掉空内容
    posts = [p for p in posts if p.content.strip()]
    if not posts:
        raise HTTPException(400, "文件中没有有效的帖子内容")

    # 如果不包含 author，清空 author 字段
    if include_author.lower() != "true":
        for p in posts:
            p.author = ""

    # 手动模式：仅存储，不进行 AI 分析
    if import_mode.lower() == "manual" or not (model_api_key and model_base_url):
        pipeline = Pipeline.from_config()
        logger.info(f"导入模式: {import_mode}，使用默认适配器（仅存储）")
    else:
        # 自动模式：使用模型配置进行 AI 分析
        pipeline = Pipeline.from_config_with_model(
            base_url=model_base_url,
            api_key=model_api_key,
            model=model_name,
            backup_model=model_backup_name or model_backup,
            backup_base_url=model_backup_base_url or model_base_url,
            backup_api_key=model_backup_api_key or model_api_key,
        )
        logger.info(f"自动模式: {model_base_url}, model={model_name}")

    stats = await pipeline.process_posts(posts, source=f"import:{filename}")

    return stats


def _parse_date(val) -> datetime:
    """尝试解析日期"""
    if not val:
        return datetime.now()
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(val), fmt)
        except ValueError:
            continue
    return datetime.now()


@app.post("/api/twitter-fetch")
async def twitter_fetch(req: TwitterUrlFetchRequest):
    """通过 Playwright 浏览器自动化从用户主页 URL 抓取推文，返回 CSV 数据供前端下载（不自动走工作流）"""
    import base64
    import traceback as tb
    from src.crawlers.twitter_url import TwitterCookieFetcher, extract_username

    if not req.urls:
        raise HTTPException(400, "请提供至少一个 Twitter/X 用户主页链接")

    fetcher = TwitterCookieFetcher(proxy=req.proxy or None, block_resources=req.block_resources)

    # 优先使用请求体中的 Cookie（每用户独立），回退到服务端配置
    if req.ct0 and req.auth_token:
        cookies = {"ct0": req.ct0.strip(), "auth_token": req.auth_token.strip()}
    else:
        cookies = fetcher.get_cookies()

    if not cookies:
        raise HTTPException(400, "未配置 Cookie，请在页面中填写你的 Twitter Cookie")

    # 从 URL 列表中提取用户名
    usernames: list[str] = []
    for url in req.urls:
        name = extract_username(url.strip())
        if name and name not in usernames:
            usernames.append(name)

    if not usernames:
        raise HTTPException(400, "未能从输入中提取到有效的用户名")

    try:
        tweets, csv_string = await fetcher.fetch_user_tweets(usernames, count=req.count, include_replies=req.include_replies, cookies=cookies)
    except Exception as e:
        tb.print_exc()
        raise HTTPException(500, f"抓取失败: {type(e).__name__}: {str(e)}")

    if not tweets:
        return {
            "status": "empty",
            "total_posts": 0,
            "sampled_posts": [],
            "csv_data": "",
            "error": "未抓取到任何推文（可能用户不存在、账号受限或 cookie 已过期）",
        }

    # 构造返回数据（帖子预览）
    sampled = []
    for t in tweets[:50]:
        sampled.append({
            "platform": "twitter",
            "post_id": t["tweet_id"],
            "author": t["author"],
            "content": t["content"][:200] if len(t["content"]) > 200 else t["content"],
            "published_at": t["created_at"],
            "metrics": {
                "likes": t["likes"],
                "retweets": t["retweets"],
                "replies": t["replies"],
            },
            "has_media": t.get("has_media", False),
            "media_type": t.get("media_type", "none"),
            "media_urls": t.get("media_urls", ""),
            "replies_count": t.get("replies_count", 0),
        })

    # CSV 内容 base64 编码，前端解码后触发下载
    csv_b64 = base64.b64encode(csv_string.encode("utf-8-sig")).decode("ascii")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return {
        "status": "success",
        "total_posts": len(tweets),
        "sampled_posts": sampled,
        "csv_data": csv_b64,
        "csv_filename": f"tweets_{timestamp}.csv",
    }


@app.post("/api/twitter-search")
async def twitter_search(req: TwitterSearchRequest):
    """通过关键词搜索 Twitter 推文（实时搜索，按最新排序），返回 CSV 数据供前端下载"""
    import base64
    import traceback as tb
    from src.crawlers.twitter_url import TwitterCookieFetcher

    if not req.keyword.strip():
        raise HTTPException(400, "请输入搜索关键词")

    fetcher = TwitterCookieFetcher(proxy=req.proxy or None, block_resources=req.block_resources)

    # 优先使用请求体中的 Cookie（每用户独立），回退到服务端配置
    if req.ct0 and req.auth_token:
        cookies = {"ct0": req.ct0.strip(), "auth_token": req.auth_token.strip()}
    else:
        cookies = fetcher.get_cookies()

    if not cookies:
        raise HTTPException(400, "未配置 Cookie，请在页面中填写你的 Twitter Cookie")

    try:
        tweets, csv_string = await fetcher.search_tweets(req.keyword.strip(), count=req.count, include_replies=req.include_replies, cookies=cookies, sort_by=req.sort_by)
    except Exception as e:
        tb.print_exc()
        raise HTTPException(500, f"搜索失败: {type(e).__name__}: {str(e)}")

    if not tweets:
        return {
            "status": "empty",
            "total_posts": 0,
            "sampled_posts": [],
            "csv_data": "",
            "csv_filename": "",
            "error": f"未搜索到「{req.keyword}」相关推文。可能原因：关键词过短（建议 2 字以上）、Cookie 已过期、或 Twitter 确实无匹配结果。",
        }

    sampled = []
    for t in tweets[:50]:
        sampled.append({
            "platform": "twitter",
            "post_id": t["tweet_id"],
            "author": t.get("author_name", "") or t.get("author", ""),
            "content": t["content"][:200] if len(t["content"]) > 200 else t["content"],
            "published_at": t["created_at"],
            "metrics": {
                "likes": t["likes"],
                "retweets": t["retweets"],
                "replies": t["replies"],
            },
            "has_media": t.get("has_media", False),
            "media_type": t.get("media_type", "none"),
            "media_urls": t.get("media_urls", ""),
            "replies_count": t.get("replies_count", 0),
        })

    # CSV 内容 base64 编码，前端解码后触发下载
    csv_b64 = base64.b64encode(csv_string.encode("utf-8-sig")).decode("ascii")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return {
        "status": "success",
        "total_posts": len(tweets),
        "sampled_posts": sampled,
        "csv_data": csv_b64,
        "csv_filename": f"twitter_search_{timestamp}.csv",
    }


@app.post("/api/twitter-login")
async def twitter_login(req: TwitterLoginRequest):
    """保存浏览器导出的 Twitter cookie（ct0 + auth_token）"""
    from src.crawlers.twitter_url import TwitterCookieFetcher

    proxy = req.proxy.strip() if req.proxy else None
    fetcher = TwitterCookieFetcher(proxy=proxy)
    try:
        fetcher.save_cookies(req.ct0, req.auth_token, proxy=proxy)
        return {"ok": True, "message": "Cookie 已保存，可以开始抓取"}
    except Exception as e:
        detail = str(e) if str(e) else f"{type(e).__name__}: {repr(e)}"
        raise HTTPException(400, f"保存失败: {detail}")


@app.get("/api/twitter-login-status")
async def twitter_login_status():
    """检查 Twitter 是否已登录（cookie 是否存在）"""
    from src.crawlers.twitter_url import TwitterCookieFetcher
    fetcher = TwitterCookieFetcher()
    return {"logged_in": fetcher.is_logged_in()}


# ── 统一多平台搜索 ────────────────────────────────────────

class PlatformCookieConfig(BaseModel):
    """每个平台的 Cookie 配置"""
    platform: str                  # twitter | reddit
    ct0: str | None = None         # Twitter: ct0
    auth_token: str | None = None  # Twitter: auth_token
    reddit_session: str | None = None  # Reddit: reddit_session cookie
    reddit_token: str | None = None    # Reddit: reddit_token (可选)
    edgebucket: str | None = None      # Reddit: edgebucket
    redesign_optout: str | None = None # Reddit: redesign_optout
    extra_cookies: dict[str, str] | None = None  # 其他额外 Cookie
    proxy: str | None = None       # 代理地址


class MultiPlatformSearchRequest(BaseModel):
    keyword: str
    count: int = 50
    platforms: list[str] = ["twitter"]   # 要搜索的平台列表
    sort_by: str = "top"                 # Twitter: top | live
    include_replies: bool = False
    block_resources: bool = False        # 是否屏蔽图片/CSS 加速加载
    cookies: list[PlatformCookieConfig] = []  # 各平台 Cookie


@app.post("/api/search")
async def multi_platform_search(req: MultiPlatformSearchRequest):
    """统一多平台搜索：并行搜索多个平台，合并结果返回"""
    import base64
    import traceback as tb

    if not req.keyword.strip():
        raise HTTPException(400, "请输入搜索关键词")

    if not req.platforms:
        raise HTTPException(400, "请至少选择一个搜索平台")

    # 构建平台 -> cookie 映射
    cookie_map: dict[str, dict] = {}
    for cfg in req.cookies:
        cookie_map[cfg.platform] = {
            "ct0": cfg.ct0 or "",
            "auth_token": cfg.auth_token or "",
            "reddit_session": cfg.reddit_session or "",
            "reddit_token": cfg.reddit_token or "",
            "edgebucket": cfg.edgebucket or "",
            "redesign_optout": cfg.redesign_optout or "",
            "extra_cookies": cfg.extra_cookies or {},
            "proxy": cfg.proxy or "",
        }

    # 并行搜索各平台
    tasks = []
    platform_names = []
    skipped_platforms: list[dict] = []

    for platform in req.platforms:
        pc = cookie_map.get(platform, {})
        proxy = pc.get("proxy") or None
        if proxy and not proxy.startswith(("http://", "https://", "socks5://")):
            proxy = f"http://{proxy}"

        if platform == "twitter":
            if not pc.get("ct0") or not pc.get("auth_token"):
                skipped_platforms.append({"platform": "twitter", "reason": "Cookie 未配置"})
                continue
            from src.crawlers.twitter_url import TwitterCookieFetcher
            fetcher = TwitterCookieFetcher(proxy=proxy, block_resources=req.block_resources)
            cookies = {"ct0": pc["ct0"], "auth_token": pc["auth_token"]}
            tasks.append(fetcher.search_tweets(
                req.keyword.strip(), count=req.count,
                include_replies=req.include_replies,
                cookies=cookies, sort_by=req.sort_by,
            ))
            platform_names.append("twitter")

        elif platform == "reddit":
            if not pc.get("reddit_session"):
                skipped_platforms.append({"platform": "reddit", "reason": "Cookie 未配置"})
                continue
            from src.crawlers.reddit_crawler import RedditCookieFetcher
            fetcher = RedditCookieFetcher(proxy=proxy)
            cookies = {}
            for key in ("reddit_session", "reddit_token", "edgebucket", "redesign_optout"):
                if pc.get(key):
                    cookies[key] = pc[key]
            # Merge any extra cookies
            if pc.get("extra_cookies"):
                cookies.update(pc["extra_cookies"])
            tasks.append(fetcher.search_posts(
                req.keyword.strip(), count=req.count, cookies=cookies,
                include_replies=req.include_replies,
                sort="hot" if req.sort_by == "top" else "new",
            ))
            platform_names.append("reddit")

    if not tasks:
        raise HTTPException(400, "所选平台均未配置 Cookie，请先填写对应平台的 Cookie")

    # 并行执行所有搜索
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 合并结果
    all_posts: list[dict] = []
    errors: list[str] = []
    platform_counts: dict[str, int] = {}

    for plat, result in zip(platform_names, results):
        if isinstance(result, Exception):
            errors.append(f"{plat}: {result}")
            logger.error(f"搜索 {plat} 失败: {result}")
            tb.print_exc()
            continue

        posts, csv_string = result
        # 只统计帖子行数（Reddit 返回的列表包含评论行）
        platform_counts[plat] = sum(1 for p in posts if p.get("type", "post") != "comment")
        for p in posts:
            p["platform"] = plat
        all_posts.extend(posts)

    if not all_posts:
        error_msg = "、".join(errors) if errors else "所有平台均未返回结果"
        return {
            "status": "empty",
            "total_posts": 0,
            "sampled_posts": [],
            "csv_data": "",
            "csv_filename": "",
            "error": f"搜索无结果。{error_msg}",
            "platform_counts": platform_counts,
            "skipped_platforms": skipped_platforms,
        }

    # 构造返回数据（只显示 post，不显示 comment，最多 60 条）
    sampled = []
    for t in all_posts:
        if t.get("type") == "comment":
            continue
        platform = t.get("platform", "unknown")
        if platform == "twitter":
            sampled.append({
                "platform": "twitter",
                "post_id": t.get("tweet_id", ""),
                "author": t.get("author_name", "") or t.get("author", ""),
                "content": t.get("content", "")[:200],
                "published_at": t.get("created_at", ""),
                "metrics": {
                    "likes": t.get("likes", 0),
                    "retweets": t.get("retweets", 0),
                    "replies": t.get("replies", 0),
                },
                "has_media": t.get("has_media", False),
                "media_type": t.get("media_type", "none"),
                "media_urls": t.get("media_urls", ""),
                "replies_count": t.get("replies_count", 0),
            })
        elif platform == "reddit":
            sampled.append({
                "platform": "reddit",
                "post_id": t.get("post_id", ""),
                "author": t.get("author", ""),
                "content": (t.get("title", "") + "\n" + t.get("content", ""))[:200].strip(),
                "published_at": t.get("created_at", ""),
                "metrics": {
                    "score": t.get("score", 0),
                    "comments": t.get("num_comments", 0),
                },
                "has_media": t.get("has_media", False),
                "media_type": t.get("media_type", "none"),
                "media_urls": t.get("media_urls", ""),
                "replies_count": t.get("comments_fetched", 0),
            })

        if len(sampled) >= 500:
            break

    # 生成合并 CSV（含评论/回复行）
    csv_buf = io.StringIO()
    csv_buf.write("\ufeff")
    all_csv_fields = [
        "platform", "type", "post_id", "parent_id", "author", "commenter",
        "content", "created_at", "url",
        "likes", "retweets", "replies", "score", "num_comments",
        "has_media", "media_type", "media_urls",
    ]
    writer = csv.DictWriter(csv_buf, fieldnames=all_csv_fields, extrasaction="ignore")
    writer.writeheader()
    for t in all_posts:
        platform = t.get("platform", "")
        row_type = t.get("type", "post")

        if platform == "twitter":
            # 写入推文行
            row = dict(t)
            row["post_id"] = t.get("tweet_id", "")
            row.setdefault("parent_id", "")
            row.setdefault("commenter", "")
            # author 列使用显示名称
            row["author"] = t.get("author_name", "") or t.get("author", "")
            writer.writerow(row)
            # 展开 replies_data 为评论行
            replies_raw = t.get("replies_data", "[]")
            if replies_raw and replies_raw != "[]":
                try:
                    import json as _json
                    replies = _json.loads(replies_raw) if isinstance(replies_raw, str) else replies_raw
                    for reply in replies:
                        display_name = reply.get("display_name", "")
                        writer.writerow({
                            "platform": "twitter",
                            "type": "comment",
                            "post_id": reply.get("tweet_id", ""),
                            "parent_id": t.get("tweet_id", ""),
                            "author": display_name,
                            "commenter": display_name,
                            "content": reply.get("content", ""),
                            "created_at": reply.get("created_at", ""),
                            "has_media": reply.get("has_media", False),
                            "media_type": reply.get("media_type", "none"),
                            "media_urls": reply.get("media_urls", ""),
                        })
                except Exception:
                    pass
        elif platform == "reddit":
            # Reddit posts 列表已包含 comment 行
            row = dict(t)
            if row_type == "post":
                row["content"] = (t.get("title", "") + "\n" + t.get("content", "")).strip()
            writer.writerow(row)
        else:
            writer.writerow(t)

    csv_b64 = base64.b64encode(csv_buf.getvalue().encode("utf-8-sig")).decode("ascii")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return {
        "status": "success",
        "total_posts": sum(1 for p in all_posts if p.get("type", "post") != "comment"),
        "total_rows": len(all_posts),
        "sampled_posts": sampled,
        "csv_data": csv_b64,
        "csv_filename": f"multi_search_{timestamp}.csv",
        "platform_counts": platform_counts,
        "skipped_platforms": skipped_platforms,
        "errors": errors,
    }


class BatchSearchRequest(BaseModel):
    keywords: list[str]
    count: int = 50
    platforms: list[str] = ["twitter"]
    sort_by: str = "top"
    include_replies: bool = False
    block_resources: bool = False        # 是否屏蔽图片/CSS 加速加载
    cookies: list[PlatformCookieConfig] = []


@app.post("/api/batch-search")
async def batch_search(req: BatchSearchRequest):
    """批量关键词搜索：逐关键词串行搜索，合并所有结果返回一个 CSV"""
    import base64
    import traceback as tb
    import json as _json

    if not req.keywords:
        raise HTTPException(400, "请提供至少一个关键词")
    if len(req.keywords) > 10:
        raise HTTPException(400, f"关键词数量过多（{len(req.keywords)} 个），单次最多 10 个")

    # 构建 cookie_map（与 /api/search 相同逻辑）
    cookie_map: dict[str, dict] = {}
    for cfg in req.cookies:
        cookie_map[cfg.platform] = {
            "ct0": cfg.ct0 or "",
            "auth_token": cfg.auth_token or "",
            "reddit_session": cfg.reddit_session or "",
            "reddit_token": cfg.reddit_token or "",
            "edgebucket": cfg.edgebucket or "",
            "redesign_optout": cfg.redesign_optout or "",
            "extra_cookies": cfg.extra_cookies or {},
            "proxy": cfg.proxy or "",
        }

    all_rows: list[dict] = []
    keyword_results: list[dict] = []   # 每轮的结果统计
    errors: list[str] = []
    platform_counts: dict[str, int] = {}
    skipped_platforms: list[dict] = []

    # 先检查哪些平台 cookie 未配置
    for platform in req.platforms:
        pc = cookie_map.get(platform, {})
        if platform == "twitter":
            if not pc.get("ct0") or not pc.get("auth_token"):
                skipped_platforms.append({"platform": "twitter", "reason": "Cookie 未配置"})
        elif platform == "reddit":
            if not pc.get("reddit_session"):
                skipped_platforms.append({"platform": "reddit", "reason": "Cookie 未配置"})

    total_keywords = len(req.keywords)

    # ── 并发关键词搜索（Semaphore 限制 3 个并发） ──
    sem = asyncio.Semaphore(3)

    async def _search_one_keyword(kw: str, idx: int):
        """搜索单个关键词的所有平台，返回 (rows, kw_result, kw_errors)"""
        async with sem:
            logger.info(f"[批量搜索 {idx+1}/{total_keywords}] 关键词: {kw}")

            tasks = []
            plat_names = []

            for platform in req.platforms:
                pc = cookie_map.get(platform, {})
                proxy = pc.get("proxy") or None
                if proxy and not proxy.startswith(("http://", "https://", "socks5://")):
                    proxy = f"http://{proxy}"

                if platform == "twitter":
                    if not pc.get("ct0") or not pc.get("auth_token"):
                        continue
                    from src.crawlers.twitter_url import TwitterCookieFetcher
                    fetcher = TwitterCookieFetcher(proxy=proxy, block_resources=req.block_resources)
                    cookies = {"ct0": pc["ct0"], "auth_token": pc["auth_token"]}
                    tasks.append(fetcher.search_tweets(
                        kw, count=req.count, include_replies=req.include_replies,
                        cookies=cookies, sort_by=req.sort_by,
                    ))
                    plat_names.append("twitter")

                elif platform == "reddit":
                    if not pc.get("reddit_session"):
                        continue
                    from src.crawlers.reddit_crawler import RedditCookieFetcher
                    fetcher = RedditCookieFetcher(proxy=proxy)
                    cookies = {}
                    for key in ("reddit_session", "reddit_token", "edgebucket", "redesign_optout"):
                        if pc.get(key):
                            cookies[key] = pc[key]
                    if pc.get("extra_cookies"):
                        cookies.update(pc["extra_cookies"])
                    tasks.append(fetcher.search_posts(
                        kw, count=req.count, cookies=cookies,
                        include_replies=req.include_replies,
                        sort="hot" if req.sort_by == "top" else "new",
                    ))
                    plat_names.append("reddit")

            if not tasks:
                return [], {"keyword": kw, "post_count": 0, "total_rows": 0}, [f"「{kw}」: 无可用平台 Cookie"]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            kw_rows = []
            kw_errors = []
            kw_post_count = 0
            for plat, result in zip(plat_names, results):
                if isinstance(result, Exception):
                    kw_errors.append(f"「{kw}」{plat}: {result}")
                    continue
                posts, csv_string = result
                for p in posts:
                    p["platform"] = plat
                    p["keyword"] = kw
                kw_rows.extend(posts)
                kw_post_count += sum(1 for p in posts if p.get("type", "post") != "comment")

            kw_result = {
                "keyword": kw,
                "post_count": kw_post_count,
                "total_rows": len([x for x in results if not isinstance(x, Exception) and x[0]]) if results else 0,
            }
            return kw_rows, kw_result, kw_errors

    # 并发执行所有关键词搜索
    valid_keywords = [(kw.strip(), i) for i, kw in enumerate(req.keywords) if kw.strip()]
    all_kw_results = await asyncio.gather(
        *[_search_one_keyword(kw, idx) for kw, idx in valid_keywords],
        return_exceptions=True,
    )

    # 合并结果
    for result in all_kw_results:
        if isinstance(result, Exception):
            errors.append(str(result))
            continue
        kw_rows, kw_result, kw_errors = result
        all_rows.extend(kw_rows)
        keyword_results.append(kw_result)
        errors.extend(kw_errors)
        # 合并 platform_counts
        for p in kw_rows:
            plat = p.get("platform", "")
            if p.get("type", "post") != "comment":
                platform_counts[plat] = platform_counts.get(plat, 0) + 1

    if not all_rows:
        error_msg = "、".join(errors) if errors else "所有关键词均未返回结果"
        return {
            "status": "empty",
            "total_posts": 0,
            "total_rows": 0,
            "keyword_results": keyword_results,
            "sampled_posts": [],
            "csv_data": "",
            "csv_filename": "",
            "error": f"批量搜索无结果。{error_msg}",
            "skipped_platforms": skipped_platforms,
        }

    # 构造返回预览（抽样显示）
    sampled = []
    for t in all_rows:
        if t.get("type") == "comment":
            continue
        platform = t.get("platform", "unknown")
        if platform == "twitter":
            sampled.append({
                "platform": "twitter",
                "post_id": t.get("tweet_id", ""),
                "author": t.get("author_name", "") or t.get("author", ""),
                "content": t.get("content", "")[:200],
                "published_at": t.get("created_at", ""),
                "keyword": t.get("keyword", ""),
                "metrics": {"likes": t.get("likes", 0), "retweets": t.get("retweets", 0), "replies": t.get("replies", 0)},
                "has_media": t.get("has_media", False),
                "media_type": t.get("media_type", "none"),
                "media_urls": t.get("media_urls", ""),
                "replies_count": t.get("replies_count", 0),
            })
        elif platform == "reddit":
            sampled.append({
                "platform": "reddit",
                "post_id": t.get("post_id", ""),
                "author": t.get("author", ""),
                "content": (t.get("title", "") + "\n" + t.get("content", ""))[:200].strip(),
                "published_at": t.get("created_at", ""),
                "keyword": t.get("keyword", ""),
                "metrics": {"score": t.get("score", 0), "comments": t.get("num_comments", 0)},
                "has_media": t.get("has_media", False),
                "media_type": t.get("media_type", "none"),
                "media_urls": t.get("media_urls", ""),
                "replies_count": t.get("comments_fetched", 0),
            })
        if len(sampled) >= 500:
            break

    # 生成合并 CSV（含评论行）
    csv_buf = io.StringIO()
    csv_buf.write("\ufeff")
    all_csv_fields = [
        "keyword", "platform", "type", "post_id", "parent_id", "author", "commenter",
        "content", "created_at", "url",
        "likes", "retweets", "replies", "score", "num_comments",
        "has_media", "media_type", "media_urls",
    ]
    writer = csv.DictWriter(csv_buf, fieldnames=all_csv_fields, extrasaction="ignore")
    writer.writeheader()
    for t in all_rows:
        platform = t.get("platform", "")
        row_type = t.get("type", "post")

        if platform == "twitter":
            row = dict(t)
            row["post_id"] = t.get("tweet_id", "")
            row.setdefault("parent_id", "")
            row.setdefault("commenter", "")
            # author 列使用显示名称
            row["author"] = t.get("author_name", "") or t.get("author", "")
            writer.writerow(row)
            replies_raw = t.get("replies_data", "[]")
            if replies_raw and replies_raw != "[]":
                try:
                    replies = _json.loads(replies_raw) if isinstance(replies_raw, str) else replies_raw
                    for reply in replies:
                        display_name = reply.get("display_name", "")
                        writer.writerow({
                            "keyword": t.get("keyword", ""),
                            "platform": "twitter",
                            "type": "comment",
                            "post_id": reply.get("tweet_id", ""),
                            "parent_id": t.get("tweet_id", ""),
                            "author": display_name,
                            "commenter": display_name,
                            "content": reply.get("content", ""),
                            "created_at": reply.get("created_at", ""),
                            "has_media": reply.get("has_media", False),
                            "media_type": reply.get("media_type", "none"),
                            "media_urls": reply.get("media_urls", ""),
                        })
                except Exception:
                    pass
        elif platform == "reddit":
            row = dict(t)
            if row_type == "post":
                row["content"] = (t.get("title", "") + "\n" + t.get("content", "")).strip()
            writer.writerow(row)
        else:
            writer.writerow(t)

    csv_b64 = base64.b64encode(csv_buf.getvalue().encode("utf-8-sig")).decode("ascii")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return {
        "status": "success",
        "total_posts": sum(1 for p in all_rows if p.get("type", "post") != "comment"),
        "total_rows": len(all_rows),
        "keyword_results": keyword_results,
        "sampled_posts": sampled,
        "csv_data": csv_b64,
        "csv_filename": f"batch_search_{timestamp}.csv",
        "skipped_platforms": skipped_platforms,
        "errors": errors,
    }


@app.get("/api/version")
async def api_version():
    """当前版本号和平台"""
    return {"version": get_version(), "platform": get_platform()}


@app.post("/api/check-update")
async def api_check_update():
    """触发后台更新检查，返回当前结果（可能尚未完成）"""
    check_for_update_async()
    # 等待最多 10 秒
    import time
    for _ in range(20):
        info = get_update_info()
        if info is not None:
            return info
        await asyncio.sleep(0.5)
    return {"status": "checking", "message": "正在检查更新..."}


@app.get("/api/update-info")
async def api_update_info():
    """获取已有的更新检查结果（不触发新检查）"""
    info = get_update_info()
    if info is None:
        return {"status": "not_checked", "message": "尚未检查更新"}
    return info


@app.post("/api/choose-folder")
async def choose_folder():
    """打开原生文件夹选择对话框，返回用户选择的路径"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()  # 隐藏主窗口
    root.attributes("-topmost", True)  # 确保对话框在最前面
    selected_path = filedialog.askdirectory(title="选择文件夹")
    root.destroy()

    return {"path": selected_path or ""}


@app.get("/api/platforms")
async def get_platforms():
    """获取可用平台列表"""
    return [p.value for p in Platform]
