"""
Twitter URL 抓取器 — 通过 Playwright 异步浏览器自动化从用户主页/搜索页获取推文

使用 Playwright async API + BrowserManager 共享浏览器 + Cookie 认证。
支持并发标签页抓取（搜索、评论）和网络资源拦截加速。
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.crawlers.platform_config import twitter_config as _TC

logger = logging.getLogger(__name__)

# ── URL 解析工具 ──────────────────────────────────────────

_TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/\w+/status(?:es)?/(\d+)"
)

_PROFILE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/(\w+)"
)

_RESERVED_PATHS = frozenset({
    "status", "i", "search", "home", "explore", "settings",
    "notifications", "messages", "compose", "hashtag",
})


def extract_tweet_id(url: str) -> str | None:
    """从 Twitter/X URL 中提取 tweet ID"""
    m = _TWEET_URL_RE.search(url.strip())
    return m.group(1) if m else None


def extract_tweet_ids(text: str) -> list[str]:
    """从一段文本中提取所有 tweet ID（去重，保持顺序）"""
    ids: list[str] = []
    seen: set[str] = set()
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        tid = extract_tweet_id(line)
        if tid and tid not in seen:
            ids.append(tid)
            seen.add(tid)
    return ids


def extract_username(text: str) -> str | None:
    """从 URL 或文本中提取 Twitter 用户名"""
    text = text.strip().rstrip("/")
    if re.match(r"^\w+$", text):
        return text
    m = _PROFILE_URL_RE.match(text)
    if m:
        name = m.group(1)
        if name.lower() not in _RESERVED_PATHS:
            return name
    return None


# ── CSV 字段 ──────────────────────────────────────────────

_CSV_FIELDS = [
    "type", "tweet_id", "parent_id", "author", "author_name", "commenter",
    "content", "created_at",
    "likes", "retweets", "replies", "quotes", "url",
    "has_media", "media_type", "media_urls",
    "replies_count",
    "is_retweet", "is_reply",
]


# ── 共享的推文提取 JS ────────────────────────────────────
# 用于 _scrape_user / _scrape_search 的每轮滚动提取
# 参数: (alreadySeenIds: string[])  →  返回: tweet object[]
_EXTRACT_TWEETS_JS = r"""(alreadySeenIds, skipMedia) => {
    const seenSet = new Set(alreadySeenIds);

    function extractText(container) {
        if (!container) return '';
        let result = '';
        function walk(node) {
            if (node.nodeType === Node.TEXT_NODE) {
                result += node.textContent;
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                const tag = node.tagName;
                if (tag === 'IMG' && node.alt) { result += node.alt; return; }
                if (tag === 'BR') { result += '\n'; return; }
                if ((tag === 'P' || tag === 'DIV') && result.length > 0 && !result.endsWith('\n')) {
                    result += '\n';
                }
                for (const child of node.childNodes) walk(child);
            }
        }
        walk(container);
        return result;
    }

    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    const results = [];

    for (const article of articles) {
        // ── tweet ID + URL ──
        const links = article.querySelectorAll('a[href*="/status/"]');
        let tweetUrl = '', tweetId = '';
        for (const link of links) {
            const href = link.getAttribute('href');
            if (href && href.includes('/status/')) {
                const m = href.match(/status\/(\d+)/);
                if (m && !seenSet.has(m[1])) {
                    tweetId = m[1];
                    tweetUrl = 'https://x.com' + href;
                    break;
                }
            }
        }

        // ── author (from search results DOM) ──
        let author = '', authorName = '';

        // 方式1: [data-testid="User-Name"]
        const userNameEl2 = article.querySelector('[data-testid="User-Name"]');
        if (userNameEl2) {
            const spans = userNameEl2.querySelectorAll('span');
            for (const span of spans) {
                const t = (span.textContent || '').trim();
                if (t && !t.startsWith('@') && !t.startsWith('·')) {
                    authorName = t;
                    break;
                }
            }
            const uLink = userNameEl2.querySelector('a[href^="/"]');
            if (uLink) {
                const href = uLink.getAttribute('href') || '';
                if (href && !href.includes('/status/') && !href.startsWith('/search')) {
                    author = href.replace('/', '@');
                }
            }
        }

        // 方式2: 回退 — 遍历 user links
        if (!author) {
            const userLinks = article.querySelectorAll('a[role="link"]');
            for (const link of userLinks) {
                const href = link.getAttribute('href') || '';
                if (href.startsWith('/') && !href.includes('/status/')
                    && !href.startsWith('/search') && !href.startsWith('/hashtag/')
                    && href.length > 1 && href.length < 30) {
                    author = href.replace('/', '@');
                    if (!authorName) {
                        const spans = link.querySelectorAll('span');
                        for (const span of spans) {
                            const t = (span.textContent || '').trim();
                            if (t && !t.startsWith('@') && !t.startsWith('·')) {
                                authorName = t;
                                break;
                            }
                        }
                        // 回退: link 全部文本去 @handle
                        if (!authorName) {
                            const ft = (link.textContent || '').trim();
                            const cleaned = ft.replace(author, '').replace('@', '').trim();
                            if (cleaned) authorName = cleaned;
                        }
                    }
                    break;
                }
            }
        }

        const textEl = article.querySelector('[data-testid="tweetText"]');
        const text = extractText(textEl);
        const timeEl = article.querySelector('time');
        const datetime = timeEl ? timeEl.getAttribute('datetime') : '';

        // ── metrics ──
        const statsEls = article.querySelectorAll('[role="group"] [aria-label]');
        let replies = 0, retweets = 0, likes = 0, quotes = 0;
        for (const el of statsEls) {
            const label = el.getAttribute('aria-label') || '';
            const numMatch = label.match(/([\d,]+)/);
            const num = numMatch ? parseInt(numMatch[1].replace(/,/g, '')) : 0;
            const lower = label.toLowerCase();
            if (lower.includes('repl') || lower.includes('回复')) replies = num;
            else if (lower.includes('repost') || lower.includes('转推')) retweets = num;
            else if (lower.includes('like') || lower.includes('喜')) likes = num;
            else if (lower.includes('view') || lower.includes('查看')) quotes = num;
        }

        const socialContext = article.querySelector('[data-testid="socialContext"]');
        const isRetweet = socialContext
            ? socialContext.textContent.toLowerCase().includes('repost')
            : false;

        // ── media（加速模式下跳过提取）──
        const mediaUrls = [];
        let hasPhoto = false, hasVideo = false;
        let mediaType = 'none';
        if (!skipMedia) {
            const photoEls = article.querySelectorAll('[data-testid="tweetPhoto"] img');
            for (const img of photoEls) {
                const src = img.getAttribute('src') || '';
                if (src && src.includes('pbs.twimg.com/media/') && !src.startsWith('blob:')) {
                    mediaUrls.push(src.replace(/&name=\w+/, '&name=large').replace(/\?name=\w+/, '?name=large'));
                    hasPhoto = true;
                }
            }
            const videoEl = article.querySelector('[data-testid="videoPlayer"] video');
            if (videoEl) {
                hasVideo = true;
                const poster = videoEl.getAttribute('poster') || '';
                if (poster && poster.includes('pbs.twimg.com') && !poster.startsWith('blob:')) {
                    mediaUrls.push(poster.replace(/&name=\w+/, '&name=large').replace(/\?name=\w+/, '?name=large'));
                }
            }
            if (hasPhoto && hasVideo) mediaType = 'mixed';
            else if (hasVideo) mediaType = 'video';
            else if (hasPhoto) mediaType = 'image';
        }

        if (text || tweetId) {
            results.push({
                type: 'post',
                tweet_id: tweetId,
                author: author,
                author_name: authorName,
                content: text,
                created_at: datetime,
                url: tweetUrl,
                likes: likes,
                retweets: retweets,
                replies: replies,
                quotes: quotes,
                has_media: skipMedia ? false : (mediaUrls.length > 0 || hasVideo),
                media_type: mediaType,
                media_urls: skipMedia ? '' : mediaUrls.join(';'),
                is_retweet: isRetweet,
                is_reply: false,
            });
        }
    }
    return results;
}"""

# ── 展开截断长推文的 JS ─────────────────────────────────────
# 在每轮滚动提取前调用，点击所有"Show more"按钮展开被截断的长推文
# 排除"Show more replies"等回复展开按钮，只作用于推文正文截断
_EXPAND_TWEETS_JS = r"""() => {
    let count = 0;
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    for (const article of articles) {
        const textEl = article.querySelector('[data-testid="tweetText"]');
        if (!textEl) continue;

        // 方式1: 可靠的 testid
        const showMore = textEl.querySelector('[data-testid="tweet-text-show-more-link"]');
        if (showMore) {
            showMore.click();
            count++;
            continue;
        }

        // 方式2: 在 tweetText 容器内找 "Show more" 可点击元素
        const allEls = textEl.querySelectorAll('*');
        for (const el of allEls) {
            const t = (el.textContent || '').trim().toLowerCase();
            if ((t === 'show more' || t === '显示更多' || t === 'show more…' || t === '查看更多')
                && !t.includes('repl') && !t.includes('回复')) {
                let clickable = el;
                for (let i = 0; i < 5; i++) {
                    if (clickable.getAttribute('role') === 'button'
                        || clickable.tagName === 'A' || clickable.tagName === 'BUTTON'
                        || clickable.onclick) {
                        break;
                    }
                    clickable = clickable.parentElement;
                    if (!clickable || clickable === textEl) { clickable = el; break; }
                }
                clickable.click();
                count++;
                break;
            }
        }
    }
    return count;
}"""


# ── Playwright 异步抓取器 ─────────────────────────────────


class TwitterCookieFetcher:
    """通过 Playwright 异步浏览器自动化 + Cookie 抓取推文"""

    def __init__(self, proxy: str | None = None, block_resources: bool = False):
        self._base_dir = Path(__file__).resolve().parent.parent.parent
        self._cookie_path = self._base_dir / ".twitter_cookies.json"
        self._config_path = self._base_dir / ".twitter_config.json"

        if not proxy:
            proxy = self._load_proxy()

        self.proxy = proxy or None
        self.block_resources = block_resources
        logger.info(f"TwitterCookieFetcher 初始化, proxy={self.proxy}, block_resources={block_resources}")

    def _strip_media_urls(self, tweets: list[dict]) -> None:
        """加速模式时清除媒体 URL（图片/视频链接），因为页面已屏蔽加载这些资源"""
        if not self.block_resources:
            return
        for t in tweets:
            t["has_media"] = False
            t["media_type"] = "none"
            t["media_urls"] = ""
            # 也清除评论中的媒体
            replies_data = t.get("replies_data", "[]")
            if isinstance(replies_data, str):
                try:
                    import json as _json
                    replies = _json.loads(replies_data)
                    for r in replies:
                        r["has_media"] = False
                        r["media_type"] = "none"
                        r["media_urls"] = ""
                    t["replies_data"] = _json.dumps(replies, ensure_ascii=False)
                except Exception:
                    pass
            elif isinstance(replies_data, list):
                for r in replies_data:
                    r["has_media"] = False
                    r["media_type"] = "none"
                    r["media_urls"] = ""

    # ── 配置持久化 ──────────────────────────────────────────

    def _load_proxy(self) -> str | None:
        if not self._config_path.exists():
            return None
        try:
            cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
            return cfg.get("proxy") or None
        except Exception:
            return None

    def _save_config(self, proxy: str | None = None, cookies: dict | None = None):
        cfg = {}
        if self._config_path.exists():
            try:
                cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        if proxy is not None:
            cfg["proxy"] = proxy
        if cookies is not None:
            cfg["ct0"] = cookies.get("ct0", "")
            cfg["auth_token"] = cookies.get("auth_token", "")
        self._config_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _load_cookies_from_config(self) -> dict | None:
        if not self._config_path.exists():
            return None
        try:
            cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
            ct0 = cfg.get("ct0", "")
            auth_token = cfg.get("auth_token", "")
            if ct0 and auth_token:
                return {"ct0": ct0, "auth_token": auth_token}
        except Exception:
            pass
        return None

    # ── 认证 ──────────────────────────────────────────────

    def save_cookies(self, ct0: str, auth_token: str, proxy: str | None = None) -> bool:
        """保存浏览器导出的 cookie"""
        cookies = {"ct0": ct0.strip(), "auth_token": auth_token.strip()}
        if not cookies["ct0"] or not cookies["auth_token"]:
            raise ValueError("ct0 和 auth_token 都不能为空")

        self._save_config(proxy=proxy or self.proxy, cookies=cookies)
        self._cookie_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        logger.info("Twitter cookie 已保存")
        return True

    def is_logged_in(self) -> bool:
        cookies = self._load_cookies_from_config()
        if cookies:
            return True
        if self._cookie_path.exists():
            try:
                data = json.loads(self._cookie_path.read_text(encoding="utf-8"))
                return bool(data.get("ct0") and data.get("auth_token"))
            except Exception:
                pass
        return False

    def get_cookies(self) -> dict | None:
        cookies = self._load_cookies_from_config()
        if cookies:
            return cookies
        if self._cookie_path.exists():
            try:
                data = json.loads(self._cookie_path.read_text(encoding="utf-8"))
                if data.get("ct0") and data.get("auth_token"):
                    return {"ct0": data["ct0"], "auth_token": data["auth_token"]}
            except Exception:
                pass
        return None

    # ── 抓取：用户主页 ────────────────────────────────────

    async def fetch_user_tweets(
        self, usernames: list[str], count: int = 50, include_replies: bool = False,
        cookies: dict | None = None,
    ) -> tuple[list[dict], str]:
        """通过 Playwright 异步抓取用户推文（并发标签页 + 并发评论）"""
        if cookies is None:
            cookies = self.get_cookies()
        if not cookies:
            raise RuntimeError("未配置 Cookie，请先导入浏览器 Cookie")

        from src.crawlers.browser_manager import BrowserManager, apply_resource_blocking

        mgr = BrowserManager.get()
        context = await mgr.new_context(cookies=cookies, proxy=self.proxy)

        all_tweets: list[dict] = []
        errors: list[str] = []

        try:
            # 并发抓取各用户主页推文
            sem_user = asyncio.Semaphore(2)

            async def _scrape_one_user(username: str):
                async with sem_user:
                    page = await context.new_page()
                    if self.block_resources:
                        await apply_resource_blocking(page)
                    try:
                        tweets = await self._scrape_user(page, username, count)
                        logger.info(f"@{username}: 抓取 {len(tweets)} 条推文")
                        return tweets
                    except AccountNotFoundError:
                        msg = f"@{username}: 账号不存在"
                        errors.append(msg)
                        logger.warning(msg)
                        return []
                    except Exception as e:
                        msg = f"@{username}: 抓取出错 - {e}"
                        errors.append(msg)
                        logger.error(msg)
                        return []
                    finally:
                        await page.close()

            user_results = await asyncio.wait_for(
                asyncio.gather(
                    *[_scrape_one_user(u) for u in usernames],
                    return_exceptions=True,
                ),
                timeout=_TC.single_keyword_timeout * 2
            )
            for r in user_results:
                if isinstance(r, Exception):
                    errors.append(str(r))
                elif r:
                    all_tweets.extend(r)

            # 并发抓取评论（添加超时保护）
            if include_replies and all_tweets:
                try:
                    await asyncio.wait_for(
                        self._parallel_scrape_replies(context, all_tweets),
                        timeout=_TC.single_keyword_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"用户主页评论抓取超时（{_TC.single_keyword_timeout}秒）")
                except Exception as e:
                    logger.error(f"用户主页评论抓取失败: {e}")

        except Exception as e:
            logger.error(f"用户主页抓取整体失败: {e}")
        finally:
            await context.close()

        # 设置默认值
        for tweet in all_tweets:
            tweet.setdefault("replies_count", 0)
            tweet.setdefault("replies_data", "[]")

        if errors:
            logger.warning(f"部分用户抓取失败: {'; '.join(errors)}")

        if not all_tweets:
            return [], ""

        self._strip_media_urls(all_tweets)
        csv_string = self._generate_csv_string(all_tweets)
        return all_tweets, csv_string

    # ── 抓取：关键词搜索 ──────────────────────────────────

    async def search_tweets(
        self, keyword: str, count: int = 50, include_replies: bool = False,
        cookies: dict | None = None, sort_by: str = "top",
    ) -> tuple[list[dict], str]:
        """通过 Twitter 搜索页按关键词抓取推文（异步 + 并发评论）"""
        if cookies is None:
            cookies = self.get_cookies()
        if not cookies:
            raise RuntimeError("未配置 Cookie，请先导入浏览器 Cookie")

        from src.crawlers.browser_manager import BrowserManager, apply_resource_blocking

        mgr = BrowserManager.get()
        context = await mgr.new_context(cookies=cookies, proxy=self.proxy)

        tweets: list[dict] = []
        try:
            page = await context.new_page()
            if self.block_resources:
                await apply_resource_blocking(page)
            try:
                tweets = await asyncio.wait_for(
                    self._scrape_search(page, keyword, count, sort_by=sort_by),
                    timeout=_TC.single_keyword_timeout
                )
                logger.info(f"搜索「{keyword}」: 获取 {len(tweets)} 条推文")
            except asyncio.TimeoutError:
                logger.warning(f"搜索「{keyword}」超时（{_TC.single_keyword_timeout}秒），已获取 {len(tweets)} 条")
            except Exception as e:
                logger.error(f"搜索「{keyword}」整体失败: {type(e).__name__}: {e}", exc_info=True)
                raise  # 重新抛出异常，让上层处理
            finally:
                await page.close()

            # 并发抓取评论（添加超时保护）
            if include_replies and tweets:
                try:
                    await asyncio.wait_for(
                        self._parallel_scrape_replies(context, tweets),
                        timeout=_TC.single_keyword_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"搜索「{keyword}」评论抓取超时（{_TC.single_keyword_timeout}秒）")
                except Exception as e:
                    logger.error(f"搜索「{keyword}」评论抓取失败: {e}")

        except Exception as e:
            logger.error(f"搜索「{keyword}」整体失败: {e}")
        finally:
            await context.close()

        # 设置默认值
        for tweet in tweets:
            tweet.setdefault("replies_count", 0)
            tweet.setdefault("replies_data", "[]")

        if not tweets:
            return [], ""

        self._strip_media_urls(tweets)
        csv_string = self._generate_csv_string(tweets)
        return tweets, csv_string

    # ── 并发评论抓取 ──────────────────────────────────────

    async def _parallel_scrape_replies(
        self, context, tweets: list[dict],
    ):
        """并发抓取多条推文的评论（Semaphore 限制 5 个并发标签页）"""
        from src.crawlers.browser_manager import apply_resource_blocking

        sem = asyncio.Semaphore(5)

        async def _scrape_one_reply(tweet: dict):
            async with sem:
                if not tweet.get("url"):
                    tweet["replies_count"] = 0
                    tweet["replies_data"] = "[]"
                    return
                page = await context.new_page()
                if self.block_resources:
                    await apply_resource_blocking(page)
                try:
                    replies = await self._scrape_replies_page(page, tweet["url"])
                    tweet["replies_count"] = len(replies)
                    tweet["replies_data"] = json.dumps(replies, ensure_ascii=False)
                except Exception as e:
                    logger.warning(f"评论抓取失败 ({tweet.get('url')}): {e}")
                    tweet["replies_count"] = 0
                    tweet["replies_data"] = "[]"
                finally:
                    await page.close()

        await asyncio.gather(
            *[_scrape_one_reply(t) for t in tweets],
            return_exceptions=True,
        )

    # ── 页面级抓取方法 ────────────────────────────────────

    async def _scrape_user(self, page, username: str, count: int) -> list[dict]:
        """用 Playwright 抓取单个用户的推文（逐轮滚动+提取+去重）"""
        url = f"https://x.com/{username}"
        logger.info(f"访问 {url} ...")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # 等待 SPA 渲染
        await asyncio.sleep(_TC.user_initial_wait)

        # 检查账号是否存在
        body_text = ""
        try:
            body_text = await page.inner_text("body")
        except Exception:
            pass

        if "doesn't exist" in body_text or "不存在" in body_text:
            raise AccountNotFoundError(f"@{username} 账号不存在")

        # 检查是否被重定向到登录页
        current_url = page.url
        if "login" in current_url or "flow" in current_url:
            raise RuntimeError("Cookie 已过期，请重新从浏览器导出")

        # 等待推文加载（给 Twitter SPA 足够时间渲染）
        for _attempt in range(2):
            try:
                await page.wait_for_selector(
                    'article[data-testid="tweet"]', timeout=_TC.search_load_timeout
                )
                break
            except Exception:
                if _attempt == 0:
                    logger.info(f"@{username}: 首次等待超时，刷新重试...")
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(_TC.user_initial_wait)
                    continue
                logger.warning(f"@{username}: 等待推文超时（重试后仍无结果）")
                return []

        # 逐轮滚动 + 提取 + 去重（虚拟滚动：DOM 同时仅保留 ~15-25 条）
        all_tweets: dict[str, dict] = {}
        stall_count = 0
        max_rounds = min(_TC.user_max_rounds, count)

        # 点击"加载更多"的 JS
        _CLICK_MORE_USER_JS = r"""() => {
            const spans = document.querySelectorAll('span');
            for (const span of spans) {
                const t = (span.textContent || '').toLowerCase().trim();
                if (t.includes('show') || t.includes('more') || t.includes('显示')
                    || t.includes('更多') || t.includes('load') || t.includes('查看')) {
                    let el = span;
                    for (let i = 0; i < 5; i++) {
                        if (!el) break;
                        if (el.getAttribute('role') === 'button' || el.tagName === 'BUTTON'
                            || el.tagName === 'A' || el.onclick
                            || el.getAttribute('tabindex') === '0') {
                            el.click(); return true;
                        }
                        el = el.parentElement;
                    }
                }
            }
            return false;
        }"""

        for round_num in range(max_rounds):
            if len(all_tweets) >= count:
                break

            # 展开截断的长推文
            expanded = await page.evaluate(_EXPAND_TWEETS_JS)
            if expanded > 0:
                await asyncio.sleep(_TC.user_expand_wait)
            
            # 提取当前 DOM 中的推文（排除已见过的 ID）
            new_tweets = await page.evaluate(_EXTRACT_TWEETS_JS, list(all_tweets.keys()), self.block_resources)
            for t in new_tweets:
                tid = t.get("tweet_id", "")
                if tid and tid not in all_tweets:
                    all_tweets[tid] = t
            
            prev_count = len(all_tweets)
            
            # 触底加载
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(_TC.scroll_wait("user"))
                        
            if len(all_tweets) == prev_count:
                stall_count += 1
                        
                # 停滞时尝试点击"加载更多"
                if stall_count >= 2:
                    clicked = await page.evaluate(_CLICK_MORE_USER_JS)
                    if clicked:
                        logger.info(f"@{username} 第{round_num+1}轮: 点击了加载更多按钮")
                        await asyncio.sleep(_TC.click_more_wait)
                        stall_count = 0
                        continue
            
                if stall_count >= 2:
                    logger.info(f"@{username}: 滚动加载完成 ({len(all_tweets)} 条，连续 {stall_count} 轮无新增)")
                    break
            else:
                stall_count = 0

        tweets = list(all_tweets.values())[:count]

        # 补充作者信息（用户主页抓取时统一设置）
        for tweet in tweets:
            if not tweet.get("author"):
                tweet["author"] = f"@{username}"
            if not tweet.get("author_name"):
                tweet["author_name"] = username

        logger.info(f"@{username}: 最终提取 {len(tweets)} 条推文")
        return tweets

    async def _scrape_search(self, page, keyword: str, count: int, sort_by: str = "top") -> list[dict]:
        """用 Playwright 抓取 Twitter 搜索结果（逐轮滚动+提取+去重）"""
        from urllib.parse import quote_plus

        f_param = "live" if sort_by == "live" else "top"
        url = f"https://x.com/search?q={quote_plus(keyword)}&f={f_param}"
        logger.info(f"搜索 {url} ...")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(_TC.search_initial_wait)

        # 检查是否被重定向到登录页
        current_url = page.url
        if "login" in current_url or "flow" in current_url or "logout" in current_url:
            logger.error(f"搜索「{keyword}」: 被重定向到登录页: {current_url}")
            raise RuntimeError("Cookie 已过期，请重新从浏览器导出")

        # 检查是否有登录弹窗遮罩（Twitter 常在页面加载后弹出登录墙）
        try:
            login_modal = await page.query_selector('[data-testid="login-drawer"], form[action="https://x.com/account/login"]')
            if login_modal:
                logger.warning(f"搜索「{keyword}」: 检测到登录弹窗，尝试关闭...")
                # 尝试点击关闭按钮
                close_btn = await page.query_selector('[data-testid="sheetDialog"] [role="button"]:first-child, [aria-label="Close"]')
                if close_btn:
                    await close_btn.click()
                    await asyncio.sleep(1)
                else:
                    logger.warning(f"搜索「{keyword}」: 无法关闭登录弹窗，Cookie 可能已失效")
                    raise RuntimeError("Cookie 已过期，登录弹窗无法关闭，请重新导出 Cookie")
        except RuntimeError:
            raise
        except Exception as e:
            logger.debug(f"登录弹窗检测异常（可忽略）: {e}")

        # 检查是否有错误提示
        body_text = ""
        try:
            body_text = await page.inner_text("body")
            body_text = body_text[:500]
        except Exception:
            pass
        if "Something went wrong" in body_text or "出了点问题" in body_text:
            raise RuntimeError("Twitter 搜索页报错，请稍后重试")

        # 等待推文加载（给 Twitter SPA 足够时间渲染）
        tweets_loaded = False
        for _attempt in range(2):
            try:
                await page.wait_for_selector(
                    'article[data-testid="tweet"]', timeout=_TC.search_load_timeout
                )
                tweets_loaded = True
                break
            except Exception:
                if _attempt == 0:
                    logger.info(f"搜索「{keyword}」: 首次等待超时，刷新重试...")
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(_TC.search_initial_wait * 2)
                    continue
                # 诊断：检查页面状态
                diag = ""
                try:
                    diag = await page.inner_text("body")
                    diag = diag[:300]
                except Exception:
                    pass
                if "no results" in diag.lower() or "没有结果" in diag:
                    logger.info(f"搜索「{keyword}」: Twitter 无匹配结果")
                else:
                    logger.warning(f"搜索「{keyword}」: 等待推文超时（页面可能加载异常），页面内容前300字: {diag[:150]}")
                # 调试截图：当 0 条结果时保存页面截图
                try:
                    import os
                    from datetime import datetime as _dt
                    debug_dir = os.path.join(os.path.expanduser("~"), ".vocab-harvester", "debug")
                    os.makedirs(debug_dir, exist_ok=True)
                    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                    screenshot_path = os.path.join(debug_dir, f"twitter_search_empty_{ts}.png")
                    await page.screenshot(path=screenshot_path, full_page=False)
                    logger.info(f"调试截图已保存: {screenshot_path}")
                    # 同时记录页面 URL
                    logger.info(f"调试信息: 当前页面 URL={page.url}, 页面内容前200字={diag[:200]}")
                except Exception as ss_err:
                    logger.warning(f"保存调试截图失败: {ss_err}")
                return []

        # 逐轮滚动 + 提取 + 去重（虚拟滚动：DOM 同时仅保留 ~15-25 条）
        SEARCH_MAX_ROUNDS = _TC.search_max_rounds
        all_tweets: dict[str, dict] = {}
        stall_count = 0
        MAX_STALLS = 2  # 连续 2 次无新增即停止
        
        # 点击“显示更多的”类按钮
        _CLICK_MORE_JS = r"""() => {
            const spans = document.querySelectorAll('span');
            for (const span of spans) {
                const t = (span.textContent || '').toLowerCase().trim();
                if (t.includes('show') || t.includes('more') || t.includes('显示')
                    || t.includes('更多') || t.includes('load') || t.includes('查看')) {
                    let el = span;
                    for (let i = 0; i < 5; i++) {
                        if (!el) break;
                        if (el.getAttribute('role') === 'button' || el.tagName === 'BUTTON'
                            || el.tagName === 'A' || el.onclick
                            || el.getAttribute('tabindex') === '0') {
                            el.click(); return true;
                        }
                        el = el.parentElement;
                    }
                }
            }
            return false;
        }"""
        
        for round_num in range(SEARCH_MAX_ROUNDS):
            # 条件 A（主要）：已抓取数量 >= 目标数量，立即停止
            if len(all_tweets) >= count:
                logger.info(f"搜索「{keyword}」: 已达到目标数量 {count} 条，停止滚动")
                break
        
            # 展开截断的长推文
            expanded = await page.evaluate(_EXPAND_TWEETS_JS)
            if expanded > 0:
                await asyncio.sleep(_TC.search_expand_wait)
                    
            # 提取当前 DOM 中的推文
            new_tweets = await page.evaluate(_EXTRACT_TWEETS_JS, list(all_tweets.keys()), self.block_resources)
            for t in new_tweets:
                tid = t.get("tweet_id", "")
                if tid and tid not in all_tweets:
                    all_tweets[tid] = t
                    
            prev_count = len(all_tweets)
                    
            # 滚动到页面底部（触底加载）
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            # 随机等待
            await asyncio.sleep(_TC.scroll_wait("search"))
                    
            # 条件 B（保底/触底检测）：连续 2 次无新增即停止
            if len(all_tweets) == prev_count:
                stall_count += 1
                    
                # 停滞时尝试点击"加载更多"
                if stall_count >= 1:
                    clicked = await page.evaluate(_CLICK_MORE_JS)
                    if clicked:
                        logger.info(f"搜索「{keyword}」第{round_num+1}轮: 点击了加载更多按钮")
                        await asyncio.sleep(_TC.scroll_wait("search"))
                        stall_count = 0
                        continue
        
                if stall_count >= MAX_STALLS:
                    logger.info(f"搜索「{keyword}」({sort_by}): 触底停止 ({len(all_tweets)} 条，连续 {stall_count} 轮无新增)")
                    break
            else:
                stall_count = 0

        tweets = list(all_tweets.values())[:count]
        logger.info(f"搜索「{keyword}」: 最终提取 {len(tweets)} 条推文")
        
        # 调试截图：滚动结束后仍然 0 条结果
        if not tweets:
            try:
                import os
                from datetime import datetime as _dt
                debug_dir = os.path.join(os.path.expanduser("~"), ".vocab-harvester", "debug")
                os.makedirs(debug_dir, exist_ok=True)
                ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = os.path.join(debug_dir, f"twitter_search_empty_after_scroll_{ts}.png")
                await page.screenshot(path=screenshot_path, full_page=False)
                logger.info(f"调试截图(滚动后)已保存: {screenshot_path}, URL={page.url}")
            except Exception as ss_err:
                logger.warning(f"保存调试截图失败: {ss_err}")
        
        return tweets

    # ── 评论/回复抓取 ─────────────────────────────────────

    # 提取回复的 JS（在 page.evaluate 中使用）
    _EXTRACT_REPLIES_JS = r"""(excludeIds) => {
        const excludeSet = new Set(excludeIds);
        function extractText(container) {
            if (!container) return '';
            let result = '';
            function walk(node) {
                if (node.nodeType === Node.TEXT_NODE) {
                    result += node.textContent;
                } else if (node.nodeType === Node.ELEMENT_NODE) {
                    if (node.tagName === 'IMG' && node.alt) { result += node.alt; return; }
                    if (node.tagName === 'BR') { result += '\n'; return; }
                    for (const child of node.childNodes) walk(child);
                }
            }
            walk(container);
            return result;
        }

        const articles = document.querySelectorAll('article[data-testid="tweet"]');
        const results = [];
        for (let i = 0; i < articles.length; i++) {
            const article = articles[i];

            // 获取推文 ID
            const statusLinks = article.querySelectorAll('a[href*="/status/"]');
            let tweetId = '';
            for (const link of statusLinks) {
                const href = link.getAttribute('href');
                if (href) {
                    const m = href.match(/status\/(\d+)/);
                    if (m) { tweetId = m[1]; break; }
                }
            }
            if (!tweetId || excludeSet.has(tweetId)) continue;

            const textEl = article.querySelector('[data-testid="tweetText"]');
            const text = extractText(textEl);

            // 作者 @username + 显示名
            let author = '';
            let displayName = '';

            const userNameEl = article.querySelector('[data-testid="User-Name"]');
            if (userNameEl) {
                const spans = userNameEl.querySelectorAll('span');
                for (const span of spans) {
                    const t = (span.textContent || '').trim();
                    if (t && !t.startsWith('@') && !t.startsWith('·')) {
                        displayName = t;
                        break;
                    }
                }
                const userLink = userNameEl.querySelector('a[href^="/"]');
                if (userLink) {
                    const href = userLink.getAttribute('href') || '';
                    if (href && !href.includes('/status/')) {
                        author = href.replace('/', '@');
                    }
                }
            }

            if (!author) {
                const userLinks = article.querySelectorAll('a[role="link"]');
                for (const link of userLinks) {
                    const href = link.getAttribute('href') || '';
                    if (href.startsWith('/') && !href.includes('/status/')
                        && !href.startsWith('/search') && !href.startsWith('/hashtag/')
                        && href.length > 1 && href.length < 30) {
                        author = href.replace('/', '@');
                        const spans = link.querySelectorAll('span');
                        for (const span of spans) {
                            const t = (span.textContent || '').trim();
                            if (t && !t.startsWith('@') && !t.startsWith('·')) {
                                displayName = t;
                                break;
                            }
                        }
                        if (!displayName) {
                            const fullText = (link.textContent || '').trim();
                            const cleaned = fullText.replace(author, '').trim();
                            if (cleaned) displayName = cleaned;
                        }
                        break;
                    }
                }
            }

            const timeEl = article.querySelector('time');
            const datetime = timeEl ? timeEl.getAttribute('datetime') : '';

            // ── 媒体提取 ──
            const mediaUrls = [];
            let hasPhoto = false, hasVideo = false;

            const photoEls = article.querySelectorAll('[data-testid="tweetPhoto"] img');
            for (const img of photoEls) {
                const src = img.getAttribute('src') || '';
                if (src && src.includes('pbs.twimg.com/media/') && !src.startsWith('blob:')) {
                    mediaUrls.push(src.replace(/&name=\w+/, '&name=large').replace(/\?name=\w+/, '?name=large'));
                    hasPhoto = true;
                }
            }

            const videoEl = article.querySelector('[data-testid="videoPlayer"] video');
            if (videoEl) {
                hasVideo = true;
                const poster = videoEl.getAttribute('poster') || '';
                if (poster && poster.includes('pbs.twimg.com') && !poster.startsWith('blob:')) {
                    mediaUrls.push(poster.replace(/&name=\w+/, '&name=large').replace(/\?name=\w+/, '?name=large'));
                }
            }

            let mediaType = 'none';
            if (hasPhoto && hasVideo) mediaType = 'mixed';
            else if (hasVideo) mediaType = 'video';
            else if (hasPhoto) mediaType = 'image';

            results.push({
                tweetId, author, displayName, text, datetime,
                hasMedia: mediaUrls.length > 0 || hasVideo,
                mediaType: mediaType,
                mediaUrls: mediaUrls.join(';'),
            });
        }
        return results;
    }"""

    async def _scrape_replies_page(self, page, tweet_url: str, max_replies: int | None = None) -> list[dict]:
        """
        在给定页面上抓取推文回复（渐进式滚动+去重）。
        由 _parallel_scrape_replies 调用，每个并发任务使用独立 page。
        """
        import time as _time

        if max_replies is None:
            max_replies = _TC.comment_max_replies

        logger.info(f"抓取回复: {tweet_url}")
        try:
            await page.goto(tweet_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning(f"打开推文详情页失败: {e}")
            return []

        await asyncio.sleep(_TC.search_initial_wait)

        # 检查是否被重定向
        if "login" in page.url or "flow" in page.url:
            logger.warning("Cookie 已过期，跳过回复抓取")
            return []

        # 点击"Show more replies"按钮的 JS
        _CLICK_SHOW_MORE_JS = r"""() => {
            const allSpans = document.querySelectorAll('span');
            for (const span of allSpans) {
                const text = (span.textContent || '').toLowerCase().trim();
                if (text.includes('show') || text.includes('more replies')
                    || text.includes('显示') || text.includes('更多')
                    || text.includes('load more') || text.includes('查看')) {
                    let el = span;
                    for (let i = 0; i < 5; i++) {
                        if (!el) break;
                        if (el.getAttribute('role') === 'button'
                            || el.tagName === 'BUTTON'
                            || el.tagName === 'A'
                            || el.onclick
                            || el.getAttribute('tabindex') === '0') {
                            el.click();
                            return true;
                        }
                        el = el.parentElement;
                    }
                }
            }
            const buttons = document.querySelectorAll('[role="button"]');
            for (const btn of buttons) {
                const t = (btn.textContent || '').toLowerCase().trim();
                if (t.includes('more') || t.includes('reply') || t.includes('show')
                    || t.includes('显示') || t.includes('回复') || t.includes('更多')) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }"""

        # 渐进式滚动 + 去重采集
        COMMENT_MAX_ROUNDS = _TC.comment_max_rounds
        collected = {}
        stall_count = 0
        max_stalls = 2
        per_tweet_budget = _TC.single_keyword_timeout / 3
        _tweet_start = _time.time()

        for round_num in range(COMMENT_MAX_ROUNDS):
            # 超时保护
            if _time.time() - _tweet_start > per_tweet_budget:
                logger.info(f"回复超时保护: 已用 {int(_time.time() - _tweet_start)} 秒，停止")
                break

            # 展开截断的长回复
            expanded = await page.evaluate(_EXPAND_TWEETS_JS)
            if expanded > 0:
                await asyncio.sleep(_TC.comment_expand_wait)

            # 提取当前 DOM 中的回复
            main_id = tweet_url.split("/status/")[-1].split("?")[0] if "/status/" in tweet_url else ""
            exclude_set = {main_id} | set(collected.keys())
            new_replies = await page.evaluate(self._EXTRACT_REPLIES_JS, list(exclude_set))

            for r in new_replies:
                tid = r.get("tweetId", "")
                if tid and tid not in collected:
                    collected[tid] = {
                        "tweet_id": tid,
                        "author": r.get("author", ""),
                        "display_name": r.get("displayName", ""),
                        "content": r.get("text", ""),
                        "created_at": r.get("datetime", ""),
                        "has_media": r.get("hasMedia", False),
                        "media_type": r.get("mediaType", "none"),
                        "media_urls": r.get("mediaUrls", ""),
                    }

            prev_count = len(collected)

            # 触底加载
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(_TC.scroll_wait("comment"))

            if len(collected) == prev_count:
                stall_count += 1

                # 连续停滞 2 轮时，尝试点击"加载更多"按钮
                if stall_count >= 2:
                    clicked = await page.evaluate(_CLICK_SHOW_MORE_JS)
                    if clicked:
                        logger.info(f"第 {round_num+1} 轮: 点击了'加载更多回复'按钮")
                        await asyncio.sleep(_TC.comment_more_wait)
                        stall_count = 0
                        continue

                if stall_count >= max_stalls:
                    logger.info(f"回复加载完成: {len(collected)} 条 (连续 {stall_count} 轮无新增)")
                    break
            else:
                stall_count = 0

            if len(collected) >= max_replies:
                logger.info(f"回复达到上限 {max_replies} 条")
                break

        replies = list(collected.values())
        logger.info(f"共获取 {len(replies)} 条回复")
        return replies

    # ── 导出 ──────────────────────────────────────────────

    def _generate_csv_string(self, tweets: list[dict]) -> str:
        """在内存中生成 CSV 字符串（utf-8-sig BOM）"""
        import io
        buf = io.StringIO()
        buf.write('\ufeff')
        writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for tweet in tweets:
            tweet["type"] = "post"
            tweet.setdefault("parent_id", "")
            tweet.setdefault("commenter", "")
            if tweet.get("author_name"):
                tweet["author"] = tweet["author_name"]
            writer.writerow(tweet)
            replies_raw = tweet.get("replies_data", "[]")
            if replies_raw and replies_raw != "[]":
                try:
                    replies = json.loads(replies_raw) if isinstance(replies_raw, str) else replies_raw
                    for reply in replies:
                        display_name = reply.get("display_name", "")
                        writer.writerow({
                            "type": "comment",
                            "tweet_id": reply.get("tweet_id", ""),
                            "parent_id": tweet.get("tweet_id", ""),
                            "author": display_name,
                            "author_name": display_name,
                            "commenter": display_name,
                            "content": reply.get("content", ""),
                            "created_at": reply.get("created_at", ""),
                            "has_media": reply.get("has_media", False),
                            "media_type": reply.get("media_type", "none"),
                            "media_urls": reply.get("media_urls", ""),
                        })
                except (json.JSONDecodeError, TypeError):
                    pass
        return buf.getvalue()


class AccountNotFoundError(Exception):
    """Twitter 账号不存在"""
    pass


# 保持向后兼容
TwitterTwikitFetcher = TwitterCookieFetcher
