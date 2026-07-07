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

        # ── 翻页搜索：使用 after 参数逐页请求，最多 10 页 ──
        all_posts: list[dict] = []
        seen_ids: set[str] = set()
        after: str | None = None
        max_pages = 10
        request_limit = min(100, count + 25)

        try:
            async with httpx.AsyncClient(
                timeout=_RC.search_load_timeout / 1000,
                follow_redirects=True,
                cookies=cookie_dict,
                headers=headers,
                proxy=proxy_url,
            ) as client:
                for page_num in range(max_pages):
                    remaining = count - len(all_posts)
                    if remaining <= 0:
                        break

                    page_params = {
                        "q": keyword,
                        "sort": sort,
                        "limit": max(request_limit, remaining + 10),
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
                    if not after or page_new == 0:
                        break

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
        sem = asyncio.Semaphore(5)  # 最多 5 个并发请求

        try:
            async with httpx.AsyncClient(
                timeout=_RC.comment_more_wait * 20,
                follow_redirects=True,
                cookies=cookie_dict,
                headers=headers,
                proxy=proxy_url,
            ) as client:
                async def _fetch_one(post: dict):
                    async with sem:
                        post_id = post.get("post_id", "")
                        subreddit = post.get("subreddit", "").replace("r/", "")
                        if not post_id or not subreddit:
                            return
                        if post.get("num_comments", 0) == 0:
                            return

                        try:
                            comments = await self._fetch_post_comments(client, subreddit, post_id)
                            post["comments_fetched"] = len(comments)
                            # 将评论加入结果（线程安全：gather 后统一处理）
                            return comments
                        except Exception as e:
                            logger.warning(f"帖子 {post_id} 评论抓取失败: {e}")
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

    async def _fetch_post_comments(
        self,
        client,
        subreddit: str,
        post_id: str,
        max_comments: int = 500,
    ) -> list[dict]:
        """抓取单个帖子的所有评论（递归展开）"""
        url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json"
        params = {"depth": "10", "limit": "500"}

        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning(f"评论 API 返回 {resp.status_code}")
            return []

        data = resp.json()
        if not isinstance(data, list) or len(data) < 2:
            return []

        # data[1] 是评论列表
        comment_listing = data[1]
        children = comment_listing.get("data", {}).get("children", [])

        comments: list[dict] = []
        self._parse_comment_tree(children, post_id, comments, max_comments)
        return comments

    def _parse_comment_tree(
        self,
        children: list[dict],
        parent_post_id: str,
        comments: list[dict],
        max_comments: int,
    ):
        """递归解析 Reddit 评论树"""
        for child in children:
            if len(comments) >= max_comments:
                return

            if child.get("kind") != "t1":
                continue

            d = child.get("data", {})

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

            body = d.get("body", "") or ""
            if len(body) > 2000:
                body = body[:2000] + "..."

            comments.append({
                "type": "comment",
                "post_id": parent_post_id,
                "parent_id": d.get("parent_id", "").replace("t3_", "").replace("t1_", ""),
                "author": "",
                "commenter": f"u/{d.get('author', '[deleted]')}",
                "subreddit": f"r/{d.get('subreddit', '')}",
                "title": "",
                "content": body,
                "created_at": created_at,
                "url": f"https://www.reddit.com{d.get('permalink', '')}",
                "score": d.get("score", 0),
                "upvote_ratio": 0,
                "num_comments": 0,
                "has_media": False,
                "media_type": "none",
                "media_urls": "",
            })

            # 递归子评论
            replies = d.get("replies")
            if isinstance(replies, dict):
                sub_children = replies.get("data", {}).get("children", [])
                self._parse_comment_tree(sub_children, parent_post_id, comments, max_comments)

    # ── 导出 ──────────────────────────────────────────────

    def _generate_csv_string(self, posts: list[dict]) -> str:
        """在内存中生成 CSV 字符串"""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(posts)
        return buf.getvalue()
