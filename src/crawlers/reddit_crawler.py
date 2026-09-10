"""
Reddit 关键词搜索抓取器 — 使用 Reddit JSON API + httpx 异步

Reddit 的 .json 端点直接返回结构化数据，无需 Playwright DOM 解析。
评论抓取采用并发模式（asyncio.gather + Semaphore）提升效率。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

from src.crawlers.platform_config import reddit_config as _RC

logger = logging.getLogger(__name__)

# ── CSV 字段 ──────────────────────────────────────────────

_CSV_FIELDS = [
    "type", "post_id", "parent_id", "author", "commenter",
    "subreddit", "title", "content",
    "created_at", "url",
    "score", "upvote_ratio", "num_comments",
    "has_media", "media_type", "media_urls",
    "comments_status", "comments_warning",
]


class RedditCookieFetcher:
    """通过 Reddit JSON API + 异步 httpx 搜索 Reddit 帖子"""

    def __init__(self, proxy: str | None = None):
        if proxy and not proxy.startswith(("http://", "https://", "socks5://")):
            proxy = f"http://{proxy}"
        self.proxy = proxy or None
        logger.info(f"RedditCookieFetcher 初始化, proxy={self.proxy}")

    # ── 搜索入口 ──────────────────────────────────────────

    async def search_posts(
        self,
        keyword: str,
        count: int = 50,
        cookies: dict | None = None,
        sort: str = "new",
        time_filter: str = "all",
        include_replies: bool = True,
        task_id: str | None = None,
    ) -> tuple[list[dict], str]:
        """异步搜索 Reddit（原生 async，使用 httpx.AsyncClient）"""
        if not cookies:
            raise RuntimeError("未配置 Reddit Cookie")

        import httpx

        # 构造请求 cookies
        cookie_dict = {}
        for k, v in cookies.items():
            if v:
                cookie_dict[k] = v

        # 完整浏览器请求头
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.reddit.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

        proxy_url = self.proxy if self.proxy else None

        # Reddit 搜索 API 端点（多个端点做容灾）
        endpoints = [
            "https://www.reddit.com/search.json",
            "https://old.reddit.com/search.json",
        ]

        # ── 按游标持续翻页，直到足量、没有后续游标或游标重复 ──
        all_posts: list[dict] = []
        seen_ids: set[str] = set()
        after: str | None = None
        max_pages = max(_RC.search_max_rounds, count)
        visited_cursors: set[str] = set()

        try:
            async with httpx.AsyncClient(
                timeout=_RC.search_load_timeout / 1000,
                follow_redirects=True,
                cookies=cookie_dict,
                headers=headers,
                proxy=proxy_url,
            ) as client:
                for page_num in range(max_pages):
                    # 检查取消信号
                    if task_id:
                        from src.api.main import _is_task_cancelled
                        if _is_task_cancelled(task_id):
                            logger.info(f"Reddit 搜索「{keyword}」: 检测到取消信号，停止抓取，已获取 {len(all_posts)} 条")
                            break
                    
                    remaining = count - len(all_posts)
                    if remaining <= 0:
                        break

                    page_params = {
                        "q": keyword,
                        "sort": sort,
                        "limit": min(100, remaining + 25),
                        "t": time_filter,
                        "type": "link",
                    }
                    if after:
                        page_params["after"] = after

                    data = None
                    last_error = ""

                    for endpoint in endpoints:
                        try:
                            logger.info(f"Reddit 搜索第{page_num+1}页: {endpoint}")
                            resp = await client.get(endpoint, params=page_params)

                            if resp.status_code == 200:
                                data = resp.json()
                                break

                            if resp.status_code == 403:
                                last_error = "403"
                                logger.warning(f"{endpoint} 返回 403，尝试下一个端点")
                                continue

                            if resp.status_code == 429:
                                raise RuntimeError("Reddit 请求过于频繁（429），请稍后再试")

                            last_error = str(resp.status_code)
                            logger.warning(f"{endpoint} 返回 HTTP {resp.status_code}，尝试下一个端点")

                        except (httpx.HTTPError, json.JSONDecodeError) as e:
                            last_error = str(e)
                            logger.warning(f"{endpoint} 请求失败: {e}，尝试下一个端点")
                            continue

                    if data is None:
                        if last_error == "403":
                            raise RuntimeError(
                                "Reddit 所有端点均返回 403。可能原因：\n"
                                "1) Cookie 已过期 — 请重新从浏览器获取\n"
                                "2) 缺少必要 Cookie — 请同时复制 reddit_session、edgebucket、redesign_optout\n"
                                "3) 账号被限制 — 尝试更换 Reddit 账号"
                            )
                        raise RuntimeError(f"Reddit 搜索第{page_num+1}页失败: {last_error}")

                    # ── 解析本页帖子 ──
                    children = data.get("data", {}).get("children", [])
                    page_new = 0

                    for child in children:
                        if len(all_posts) >= count:
                            break

                        d = child.get("data", {})

                        if d.get("stickied"):
                            continue

                        post_id = d.get("id", "")
                        if post_id in seen_ids:
                            continue
                        seen_ids.add(post_id)

                        # 时间戳
                        created_utc = d.get("created_utc", 0)
                        created_at = ""
                        if created_utc:
                            try:
                                created_at = datetime.fromtimestamp(
                                    created_utc, tz=timezone.utc
                                ).isoformat()
                            except Exception:
                                pass

                        # 媒体提取
                        has_media = False
                        media_type = "none"
                        media_urls: list[str] = []

                        if d.get("post_hint") == "image" or d.get("is_video"):
                            has_media = True

                        if d.get("is_video"):
                            media_type = "video"
                            video_data = d.get("media", {}).get("reddit_video", {})
                            if video_data.get("fallback_url"):
                                media_urls.append(video_data["fallback_url"])
                            if d.get("preview", {}).get("images"):
                                try:
                                    img_src = d["preview"]["images"][0]["source"]["url"]
                                    media_urls.append(img_src.replace("&amp;", "&"))
                                except (KeyError, IndexError):
                                    pass

                        elif d.get("post_hint") == "image":
                            media_type = "image"
                            if d.get("url_overridden_by_dest"):
                                media_urls.append(d["url_overridden_by_dest"])
                            if d.get("preview", {}).get("images"):
                                try:
                                    img_src = d["preview"]["images"][0]["source"]["url"]
                                    media_urls.append(img_src.replace("&amp;", "&"))
                                except (KeyError, IndexError):
                                    pass

                        elif d.get("gallery_data"):
                            has_media = True
                            media_type = "image"
                            gallery_items = d.get("media_metadata", {})
                            for item_id in gallery_items:
                                item = gallery_items[item_id]
                                if item.get("s", {}).get("u"):
                                    media_urls.append(item["s"]["u"].replace("&amp;", "&"))

                        elif d.get("url_overridden_by_dest", "").endswith(
                            (".jpg", ".jpeg", ".png", ".gif", ".webp")
                        ):
                            has_media = True
                            media_type = "image"
                            media_urls.append(d["url_overridden_by_dest"])

                        content = d.get("selftext", "") or ""
                        if len(content) > 2000:
                            content = content[:2000] + "..."

                        all_posts.append({
                            "type": "post",
                            "post_id": post_id,
                            "parent_id": "",
                            "author": d.get('author', '[deleted]'),
                            "commenter": "",
                            "subreddit": f"r/{d.get('subreddit', '')}",
                            "title": d.get("title", ""),
                            "content": content,
                            "created_at": created_at,
                            "url": f"https://www.reddit.com{d.get('permalink', '')}",
                            "score": d.get("score", 0),
                            "upvote_ratio": d.get("upvote_ratio", 0),
                            "num_comments": d.get("num_comments", 0),
                            "has_media": has_media,
                            "media_type": media_type,
                            "media_urls": ";".join(media_urls),
                        })
                        page_new += 1

                    # 检查下一页
                    after = data.get("data", {}).get("after")
                    logger.info(f"Reddit 第{page_num+1}页: +{page_new} 帖, 累计 {len(all_posts)}, after={after}")
                    if not after or after in visited_cursors or len(all_posts) >= count:
                        break
                    visited_cursors.add(after)
                    await asyncio.sleep(_RC.scroll_wait("search"))

            logger.info(f"Reddit 搜索「{keyword}」: 共 {len(all_posts)} 帖")

        except httpx.ProxyError:
            raise RuntimeError("无法连接代理服务器，请检查代理地址")
        except httpx.ConnectError:
            raise RuntimeError("无法连接 Reddit，国内用户请配置代理")

        if not all_posts:
            return [], ""

        if include_replies:
            all_rows = await self._fetch_all_comments_parallel(all_posts, cookie_dict, headers, proxy_url)
        else:
            all_rows = all_posts

        csv_string = self._generate_csv_string(all_rows)
        return all_rows, csv_string

    # ── 评论并发抓取 ──────────────────────────────────────

    async def _fetch_all_comments_parallel(
        self,
        posts: list[dict],
        cookie_dict: dict,
        headers: dict,
        proxy_url: str | None,
    ) -> list[dict]:
        """并行为每个帖子抓取评论（Semaphore 限流 + asyncio.gather）"""
        import httpx

        all_rows: list[dict] = list(posts)  # 帖子本身先加入
        sem = asyncio.Semaphore(2)  # 避免大量帖子评论请求瞬间触发限流

        try:
            async with httpx.AsyncClient(
                timeout=_RC.comment_request_timeout,
                follow_redirects=True,
                cookies=cookie_dict,
                headers=headers,
                proxy=proxy_url,
            ) as client:
                async def _fetch_one(post: dict):
                    async with sem:
                        post_id = post.get("post_id", "")
                        subreddit = post.get("subreddit", "").replace("r/", "")
                        post["comments_fetched"] = 0
                        post["comments_status"] = "finished"
                        post["comments_warning"] = ""
                        if not post_id or not subreddit:
                            post["comments_status"] = "failed"
                            post["comments_warning"] = "缺少帖子链接，无法抓取评论"
                            return []
                        if post.get("num_comments") == 0:
                            return []

                        try:
                            comments = await self._fetch_post_comments(client, subreddit, post_id, progress=post)
                            post["comments_fetched"] = len(comments)
                            # 将评论加入结果（线程安全：gather 后统一处理）
                            return comments
                        except Exception as e:
                            logger.warning(f"帖子 {post_id} 评论抓取失败: {e}")
                            post["comments_status"] = "failed"
                            post["comments_warning"] = f"评论抓取失败: {e}"
                            return []

                # 并发抓取所有帖子的评论
                results = await asyncio.gather(
                    *[_fetch_one(p) for p in posts],
                    return_exceptions=True,
                )

                # 合并评论到 all_rows
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.warning(f"帖子评论异常: {result}")
                    elif result:
                        all_rows.extend(result)

        except httpx.ProxyError:
            logger.warning("评论抓取: 无法连接代理")
        except httpx.ConnectError:
            logger.warning("评论抓取: 无法连接 Reddit")
        except Exception as e:
            logger.warning(f"评论抓取异常: {e}")

        comment_count = len(all_rows) - len(posts)
        logger.info(f"Reddit 评论抓取完成: 共 {comment_count} 条评论")
        return all_rows

    async def _get_comment_json(self, client, url: str, params: dict):
        """对临时网络错误、429 和 5xx 有限重试；鉴权失败透传给调用者。"""
        import httpx
        from email.utils import parsedate_to_datetime

        for attempt in range(_RC.comment_retry_attempts):
            delay = _RC.comment_retry_wait * (2 ** attempt)
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt + 1 == _RC.comment_retry_attempts:
                        resp.raise_for_status()
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                            except (ValueError, TypeError):
                                pass
                    # 不提前重试长时间限流，向用户报告并保留已有结果。
                    if delay > _RC.comment_request_timeout:
                        resp.raise_for_status()
                else:
                    resp.raise_for_status()
                    return resp.json()
            except (httpx.TransportError, ValueError):
                if attempt + 1 == _RC.comment_retry_attempts:
                    raise
            await asyncio.sleep(delay)

    async def _fetch_post_comments(
        self, client, subreddit: str, post_id: str,
        max_comments: int | None = None, *, progress: dict | None = None,
    ) -> list[dict]:
        """解析首屏、morechildren 和深层会话；按评论 ID 去重并保留完整正文。"""
        if max_comments is None:
            max_comments = _RC.comment_max_replies
        if progress is None:
            progress = {}
        url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json"
        params = {"depth": "10", "limit": "500", "raw_json": "1"}
        data = await self._get_comment_json(client, url, params)
        if not isinstance(data, list) or len(data) < 2:
            raise RuntimeError("Reddit 评论接口返回了无效数据")

        comments: list[dict] = []
        pending: list[str] = []
        continuations: list[str] = []
        seen: set[str] = set()
        requested: set[str] = set()
        expanded_threads: set[str] = set()
        warning = ""
        self._parse_comment_tree(data[1].get("data", {}).get("children", []),
                                 post_id, comments, max_comments, seen, pending, continuations)
        try:
            for _ in range(_RC.comment_max_rounds):
                pending = list(dict.fromkeys(cid for cid in pending if cid not in seen and cid not in requested))
                continuations = list(dict.fromkeys(cid for cid in continuations if cid not in expanded_threads))
                if len(comments) >= max_comments:
                    warning = f"达到单帖评论上限 {max_comments} 条，可能仍有未采集评论"
                    break
                if not pending and not continuations:
                    break
                await asyncio.sleep(_RC.scroll_wait("comment"))
                if pending:
                    batch, pending = pending[:100], pending[100:]
                    requested.update(batch)
                    more = await self._get_comment_json(client, "https://www.reddit.com/api/morechildren.json", {
                        "api_type": "json", "link_id": f"t3_{post_id}",
                        "children": ",".join(batch), "raw_json": "1",
                    })
                    payload = more.get("json", {})
                    if payload.get("errors") or not isinstance(payload.get("data", {}).get("things"), list):
                        raise RuntimeError("Reddit 更多评论接口返回错误")
                    children = payload["data"]["things"]
                else:
                    parent = continuations.pop(0)
                    expanded_threads.add(parent)
                    thread = await self._get_comment_json(client, url, {**params, "comment": parent})
                    if not isinstance(thread, list) or len(thread) < 2:
                        raise RuntimeError("Reddit 深层评论接口返回无效数据")
                    children = thread[1].get("data", {}).get("children", [])
                self._parse_comment_tree(children, post_id, comments, max_comments,
                                         seen, pending, continuations)
            else:
                warning = "达到评论翻页上限，可能仍有未采集评论"
            if not warning and requested - seen:
                warning = "平台未返回部分折叠评论（可能已删除或不可访问）"
        except Exception as e:
            warning = f"部分评论加载失败，已保留已采集评论: {e}"
            logger.warning(warning)
        if not comments and progress.get("num_comments", 0) and not warning:
            warning = "页面显示有评论，但接口未返回可访问评论"
        progress["comments_status"] = "partial" if warning else "finished"
        progress["comments_warning"] = warning
        progress["comments_fetched"] = len(comments)
        return comments

    def _parse_comment_tree(
        self, children: list[dict], parent_post_id: str, comments: list[dict],
        max_comments: int, seen: set[str] | None = None,
        pending: list[str] | None = None, continuations: list[str] | None = None,
    ):
        """解析已返回的评论，并将折叠节点加入待加载队列。"""
        if seen is None:
            seen = {c.get("comment_id", "") for c in comments}
        if pending is None:
            pending = []
        if continuations is None:
            continuations = []
        for child in children:
            if len(comments) >= max_comments:
                return
            d = child.get("data", {})
            if child.get("kind") == "more":
                pending.extend(d.get("children", []))
                if not d.get("children") and d.get("parent_id", "").startswith("t1_"):
                    continuations.append(d["parent_id"][3:])
                continue
            if child.get("kind") != "t1":
                continue
            cid = d.get("id", "")
            if cid and cid not in seen:
                seen.add(cid)
                created_at = ""
                if d.get("created_utc"):
                    try:
                        created_at = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc).isoformat()
                    except (ValueError, TypeError, OSError):
                        pass
                comments.append({
                    "type": "comment", "comment_id": cid,
                    # 保留 post_id 指向根帖，兼容定时任务的评论分组。
                    "post_id": parent_post_id,
                    "parent_id": d.get("parent_id", "").removeprefix("t3_").removeprefix("t1_"),
                    "author": "", "commenter": f"u/{d.get('author', '[deleted]')}",
                    "subreddit": f"r/{d.get('subreddit', '')}", "title": "",
                    "content": d.get("body", "") or "", "created_at": created_at,
                    "url": f"https://www.reddit.com{d.get('permalink', '')}",
                    "score": d.get("score", 0), "upvote_ratio": 0, "num_comments": 0,
                    "has_media": False, "media_type": "none", "media_urls": "",
                })
            # 重复父节点仍可能带回新的深层回复。
            replies = d.get("replies")
            if isinstance(replies, dict):
                self._parse_comment_tree(replies.get("data", {}).get("children", []),
                                         parent_post_id, comments, max_comments,
                                         seen, pending, continuations)

    # ── 导出 ──────────────────────────────────────────────

    def _generate_csv_string(self, posts: list[dict]) -> str:
        """在内存中生成 CSV 字符串"""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(posts)
        return buf.getvalue()
