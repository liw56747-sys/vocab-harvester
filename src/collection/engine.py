"""Shared durable execution for manual, batch and scheduled collection.
Search cursors are platform/session dependent. Recovery reopens the persisted partition,
requests a larger prefix and deduplicates against committed IDs; it never trusts a stale cursor.
"""
import asyncio
import hashlib
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from src.collection.store import Store, key, merge, RowIndex
from src.collection.comment_queue import CommentJob, drain_comments
from src.collection.comment_session import CommentSession, CommentOutcomeError
from src.crawlers.comment_state import comment_summary, error_reason
from src.crawlers.progress import observe_rows, report_progress


class CollectionSpec(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    platforms: list[Literal['twitter', 'reddit']] = Field(default_factory=lambda: ['twitter'])
    count: int = Field(default=50, ge=1, le=10000)
    count_scope: Literal['per_keyword', 'per_platform', 'total'] = 'per_keyword'
    content_mode: Literal['text', 'images'] = 'text'
    include_replies: bool = False
    block_resources: bool = False
    sort_by: Literal['top', 'live', 'hot', 'new'] = 'top'
    since: date | None = None
    until: date | None = None  # Exclusive, matching X until: semantics.
    window_days: int = Field(default=7, ge=1, le=366)
    languages: list[str] = Field(default_factory=list)
    alternate_sort: bool = False
    keyword_groups: list[list[str]] = Field(default_factory=list)
    max_retries: int = Field(default=2, ge=0, le=3)
    incremental: bool = False
    lookback_days: int = Field(default=3, ge=1, le=90)
    task_type: Literal['search', 'url_fetch'] = 'search'
    probe: bool = False
    twitter_count_cap_with_replies: int = Field(default=0, ge=0, le=10000)
    batch_size: int = Field(default=0, ge=0, le=50)
    batch_cooldown: float = Field(default=30, ge=0, le=600)

    @model_validator(mode='after')
    def validate_plan(self):
        import re
        self.keywords = list(dict.fromkeys(k.strip() for k in self.keywords if k.strip()))
        self.platforms = list(dict.fromkeys(self.platforms))
        if not self.keywords or len(self.keywords) > 50:
            raise ValueError('请提供 1–50 个非空关键词或主页 URL')
        if not self.platforms:
            raise ValueError('请至少选择一个平台')
        if bool(self.since) != bool(self.until) or (self.since and self.since >= self.until):
            raise ValueError('起止日期必须同时填写，结束日期不包含当天且必须晚于开始日期')
        if self.since and (self.until-self.since).days > 366:
            raise ValueError('单轮日期范围最多 366 天，请分批采集')
        if len(self.languages) > 5 or any(not re.fullmatch('[a-z]{2,3}(?:-[A-Z]{2})?', s) for s in self.languages):
            raise ValueError('语言使用 zh、en 等代码，最多 5 种；仅作用于 X 搜索')
        if len(self.keyword_groups) > 20 or any(not g or len(g) > 10 or any(not x.strip() for x in g) for g in self.keyword_groups):
            raise ValueError('组合最多 20 组，每组 1–10 个非空词')
        windows = ((self.until-self.since).days+self.window_days-1)//self.window_days if self.since else 1
        partitions_count = len(self.keywords)*(1+len(self.keyword_groups))*windows*(2 if self.alternate_sort else 1)*sum(max(1,len(self.languages)) if p=='twitter' else 1 for p in self.platforms)
        if partitions_count > 2000:
            raise ValueError('搜索分段组合超过 2000，请减少关键词/组合或增加每段天数')
        if self.task_type == 'url_fetch':
            if self.platforms != ['twitter']:
                raise ValueError('主页采集目前仅支持 X')
            from src.crawlers.twitter_url import extract_username
            users = [extract_username(u) for u in self.keywords]
            if not all(users):
                raise ValueError('请输入有效的 X 用户名或主页链接')
            self.keywords = list(dict.fromkeys(users))
        if self.content_mode == 'images':
            self.block_resources = False
            self.include_replies = True
        if self.probe:
            self.content_mode = 'text'
            self.count=1
            self.include_replies=False
            self.since=self.until=None
            self.keyword_groups=[]
            self.alternate_sort=False
        return self


def platform_target(spec, platform):
    if platform=='twitter' and spec.include_replies and spec.twitter_count_cap_with_replies:
        return min(spec.count,spec.twitter_count_cap_with_replies)
    return spec.count


def expected_posts(spec):
    targets=sum(platform_target(spec,p) for p in spec.platforms)
    if spec.count_scope=='total':
        return min(spec.count,targets)
    return targets*(len(spec.keywords) if spec.count_scope=='per_keyword' else 1)


def classify(error):
    s = str(error).lower()
    # 403 can be access policy or account permissions: do not label all 403s expired cookies.
    if any(v in s for v in ['429', 'rate limit', 'too many', '频繁', '限流']):
        return 'rate_limit', True, '平台限流，已降低速度；可稍后续抓'
    if '403' in s or 'forbidden' in s:
        return 'access_denied', False, '平台拒绝访问，请检查账号权限或平台验证'
    if any(v in s for v in ['未配置', 'cookie', '登录', '401', 'login', 'logout']):
        return 'authentication', False, '登录会话不可用，请更新 Cookie 后续抓'
    if '403' in s or 'forbidden' in s or '权限' in s:
        return 'access_denied', False, '平台拒绝访问，请检查账号权限或平台验证'
    if any(v in s for v in ['timeout','超时','connect','network','proxy','net::','err_','网络','代理']):
        return 'network', True, '网络或代理异常，可检查连接后续抓'
    if any(v in s for v in ['500','502','503','504','something went wrong','页面异常']):
        return 'platform', True, '平台暂时异常，可稍后续抓'
    return 'unknown', False, '抓取未完成，请检查日志后续抓'


class Traffic:
    """Process-wide gate, including different threads/event loops. Never blocks an event loop."""
    lock = threading.Lock()
    active = set()
    delay = {}
    until = {}

    @classmethod
    async def enter(cls, platform, cancelled):
        while True:
            if cancelled():
                raise asyncio.CancelledError
            with cls.lock:
                if platform not in cls.active and len(cls.active) < 2 and time.monotonic() >= cls.until.get(platform, 0):
                    cls.active.add(platform)
                    return
            await asyncio.sleep(.2)

    @classmethod
    def leave(cls, platform, error=None, elapsed=0):
        with cls.lock:
            cls.active.discard(platform)
            delay = cls.delay.get(platform, 0)
            kind = getattr(error, 'reason', None) or (classify(error)[0] if error else '')
            if error and kind in {'rate_limit', 'network', 'platform'}:
                delay = min(120, max(15 if kind == 'rate_limit' else 2, delay * 2))
            elif error and kind not in {'shortfall', 'limit', 'unavailable'}:
                # Unknown/per-post failures must not inflate a platform-wide delay.
                delay = max(2, delay / 2 - .5)
            elif elapsed > 60:
                delay = min(10, max(1, delay))
            else:
                delay = max(0, delay / 2 - .5)
            cls.delay[platform] = delay
            cls.until[platform] = time.monotonic() + delay


def partitions(spec, platform, keyword):
    variants = [keyword] + [' '.join(g) for g in spec.keyword_groups]
    variants = list(dict.fromkeys(variants))
    windows = [(None, None)]
    if spec.since:
        windows = []
        start = spec.since
        while start < spec.until:
            end = min(start + timedelta(days=spec.window_days), spec.until)
            windows.append((start, end))
            start = end
        windows.reverse()  # Prefer recent data, then progressively backfill history.
    sorts = [spec.sort_by]
    if spec.alternate_sort:
        sorts.append('live' if spec.sort_by in ('top','hot') else 'top')
    for start, end in windows:
        for variant in variants:
            for lang in (spec.languages or ['']) if platform == 'twitter' else ['']:
                for sort in sorts:
                    query = variant
                    if platform == 'twitter' and spec.task_type == 'search':
                        if start:
                            query += f' since:{start} until:{end}'
                        if lang:
                            query += f' lang:{lang}'
                    yield query, sort, start, end


def in_window(row, start, end):
    if start is None:
        return True
    try:
        value = row.get('created_at')
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo:
            dt=dt.astimezone(timezone.utc)
        return start <= dt.date() < end
    except (ValueError, TypeError):
        return False  # Unknown dates must not silently pass a date filter.


async def fetch_comments(fetcher, platform, posts, cookies):
    session = getattr(fetcher, '_collection_comment_session', None)
    if session is not None:
        if platform == 'twitter':
            async with session.twitter_lease() as context:
                await fetcher._parallel_scrape_replies(context, posts)
            return posts
        return await fetcher._fetch_all_comments_parallel(posts, cookies, fetcher.request_headers(), fetcher.proxy,
                                                         client=await session.reddit_client())
    if platform == 'twitter':
        from src.crawlers.browser_manager import BrowserManager
        context = await BrowserManager.get().new_context(cookies=cookies, proxy=fetcher.proxy)
        try:
            await fetcher._parallel_scrape_replies(context, posts)
        finally:
            await fetcher._safe_close(context, label='context(comment-recovery)')
        return posts
    headers = fetcher.request_headers()
    return await fetcher._fetch_all_comments_parallel(posts, cookies, headers, fetcher.proxy)


async def collect(store, rid, cookie_map, *, cancelled=lambda: False, progress=None, comments_only=False, checkpoint=None, seen_ids=None, platform_event=None, images_only=False):
    original_cancelled = cancelled
    cancelled = lambda: original_cancelled() or bool(asyncio.current_task().cancelling())
    record = store.get(rid)
    spec = CollectionSpec(**record['spec'])
    if comments_only and not spec.include_replies:
        spec.include_replies = True
        store.update_spec(rid,spec.model_dump(mode='json'))
    from src.collection.images import Images, without_text
    images = Images(store) if spec.content_mode == 'images' else None
    if images_only and images is None:
        raise ValueError('该记录不是图片采集任务')
    image_attempted = set()
    async def download_images():
        def progress_images():
            publish_progress('images')
            if platform_event: platform_event('progress', '')
        await images.download(rid, {p:c.get('proxy') for p,c in cookie_map.items()}, cancelled,
                              progress_images, attempted=image_attempted)
    owner = record['owner']
    stamp = record['created']
    history_rows = []
    if owner:
        for old in store.list(limit=10000, owner=owner):
            retention = 90 if spec.task_type=='url_fetch' else 30
            oldest = datetime.fromisoformat(stamp)-timedelta(days=retention)
            if old['id'] != rid and oldest <= datetime.fromisoformat(old['created']) < datetime.fromisoformat(stamp):
                if store.get(old['id'])['spec'].get('content_mode','text') != spec.content_mode:
                    continue
                history_rows = merge(history_rows, store.rows(old['id']))
    historical = {key(r) for r in history_rows if r.get('type','post')!='comment'}
    previous = store.units(rid)
    # A previous process may have exited between a chunk commit and cleanup.
    for uid, (saved_rows, _) in previous.items():
        interrupted = []
        for row in saved_rows:
            if row.get('comments_active'):
                row.update(comments_active=False,
                           comments_status='partial' if row.get('replies_count') or row.get('comments_fetched') else 'failed',
                           comments_reason='interrupted', comments_warning='上次处理被中断，可继续补抓')
                interrupted.append(row)
        if interrupted:
            store.save_comment_delta(rid, uid, interrupted)
    all_rows = store.rows(rid)
    counts, errors = [], []
    unit_errors = {uid: f"{state.get('platform', '')} {state.get('keyword', '')}: {state['error']}"
                   for uid, (_, state) in previous.items() if state.get('error')}
    search_denials = {}
    fetchers, comment_sessions = {}, {}
    baseline = comment_summary(all_rows, spec.include_replies)['total_comments']
    attempt_started = datetime.now(timezone.utc).isoformat()
    current_platform = current_keyword = ''
    stage = 'images' if images_only else 'comments' if comments_only else 'posts'
    search_completed = set()
    def publish_progress(phase=None):
        phase = phase or stage
        summary = comment_summary(all_rows, spec.include_replies)
        summary['refreshed_posts'] = sum(r.get('type', 'post') != 'comment' and r.get('collection_kind') == 'revisit' for r in all_rows)
        visible_errors = list(dict.fromkeys([*errors, *unit_errors.values()]))
        summary.update(errors=visible_errors, error='；'.join(visible_errors))
        summary.update(phase=phase, attempt_started=attempt_started,
                       new_comments=max(0, summary['total_comments']-baseline),
                       comments_only=comments_only, current_platform=current_platform,
                       current_keyword=current_keyword, last_progress_at=datetime.now(timezone.utc).isoformat(),
                       search_units_completed=len(search_completed),
                       search_units_total=len(spec.keywords)*len(spec.platforms))
        if images: summary.update(images.summary(rid))
        store.progress(rid, summary)
        if progress:
            total = len(spec.keywords)*len(spec.platforms)
            progress({**summary, 'run_id':rid, 'total_posts':summary['saved_posts'],
                      'requested_posts':expected_posts(spec), 'total':total,
                      'completed':len(search_completed), 'percent':int(100*len(search_completed)/total)})
    publish_progress()
    # Legacy zero-comment shortcuts were never verified against the comment endpoint.
    if comments_only:
        for uid, (saved_rows, saved_state) in previous.items():
            for row in saved_rows:
                if row.get('platform') == 'reddit' and row.get('type', 'post') != 'comment' and row.get('num_comments') == 0 and not row.get('comments_empty_verified'):
                    row['comments_status'] = 'pending'
            store.save(rid, uid, saved_rows, saved_state)

    async def acquire(platform):
        if platform_event:
            platform_event('waiting',platform)
        try:
            await Traffic.enter(platform,cancelled)
        finally:
            if platform_event:
                platform_event('acquired',platform)
    comment_index = None
    last_comment_checkpoint = 0

    def comment_saver(uid, platform, keyword, revisit=False):
        # Bind unit/keyword per queue entry, not the enclosing loop variables.
        def save(incoming):
            nonlocal all_rows, last_comment_checkpoint
            incoming = [{**r, 'platform': platform, 'keyword': keyword,
                         **({'collection_kind': 'revisit'} if revisit else {})} for r in incoming]
            if images:
                incoming = without_text(incoming)
                images.discover(rid, incoming)
            store.save_comment_delta(rid, uid, incoming)
            comment_index.update(incoming)
            all_rows = comment_index.rows()
            publish_progress('comments')
            # Database chunks remain immediately durable; legacy scheduled
            # result conversion is expensive and need not run every scroll.
            if checkpoint and time.monotonic() - last_comment_checkpoint >= 2:
                checkpoint(all_rows)
                last_comment_checkpoint = time.monotonic()
        return save

    store.status(rid, 'running')
    try:
        # Finish every search group before visiting any comment endpoint.
        stages = [] if images_only else (['comments'] if comments_only else
                  ['posts'] + (['comments'] if spec.include_replies else []))
        for stage in stages:
            previous = store.units(rid)
            for platform in spec.platforms:
                if platform_event:
                    platform_event('start',platform)
                current_platform, current_keyword = platform, ''
                publish_progress()
                pc = cookie_map.get(platform, {})
                cookies = {k:v for k,v in pc.items() if k not in ('proxy','extra_cookies') and v}
                cookies.update(pc.get('extra_cookies') or {})
                valid = bool(cookies.get('ct0') and cookies.get('auth_token')) if platform == 'twitter' else bool(cookies.get('reddit_session'))
                if not valid:
                    errors.append(f'{platform}: 未配置 Cookie；请保存账号配置后续抓')
                    if platform_event:
                        platform_event('done',platform)
                    continue
                from src.crawlers.twitter_url import TwitterCookieFetcher
                from src.crawlers.reddit_crawler import RedditCookieFetcher
                proxy = pc.get('proxy') or None
                if proxy and not proxy.startswith(('http://','https://','socks5://')):
                    proxy = 'http://' + proxy
                if platform not in fetchers:
                    fetchers[platform] = TwitterCookieFetcher(proxy=proxy, block_resources=spec.block_resources) if platform == 'twitter' else RedditCookieFetcher(proxy=proxy)
                fetcher = fetchers[platform]
                if stage == 'comments':
                    session = CommentSession(fetcher, platform, cookies)
                    comment_sessions[platform] = session
                    fetcher._collection_comment_session = session
                    comment_index = RowIndex(all_rows)
                    comment_jobs = []
                if stage == 'comments' and spec.incremental and spec.include_replies:
                    cutoff = datetime.fromisoformat(stamp).date() - timedelta(days=spec.lookback_days)
                    recent = [r for r in history_rows if r.get('platform')==platform and r.get('type','post')!='comment'
                              and in_window(r,cutoff,datetime.fromisoformat(stamp).date()+timedelta(days=1))]
                    for parent in recent:
                        uid = 'revisit:'+key(parent)
                        old_rows, old_state = previous.get(uid,([],{}))
                        if old_state.get('finished') or (old_rows and all(r.get('comments_status') == 'finished' for r in old_rows if r.get('type', 'post') != 'comment')):
                            continue
                        post = {**parent, 'collection_kind':'revisit', 'comments_status':'pending'}
                        current = merge(old_rows, [post])
                        store.save(rid, uid, current, old_state)
                        save = comment_saver(uid, platform, parent.get('keyword', ''), True)
                        post = next(r for r in current if r.get('type', 'post') != 'comment')
                        save([post])
                        comment_jobs.append(CommentJob(post, save))
                for keyword_index, keyword in enumerate(spec.keywords):
                    current_keyword = keyword
                    publish_progress()
                    unit = hashlib.sha256((platform+'\0'+keyword).encode()).hexdigest()[:24]
                    rows, state = previous.get(unit, ([], {}))
                    rows = list(rows)
                    def persist(incoming=()):
                        nonlocal rows, all_rows
                        incoming = [{**r, 'platform':platform, 'keyword':keyword} for r in incoming]
                        if images:
                            incoming = without_text(incoming)
                            images.discover(rid, incoming)
                        rows = merge(rows, incoming)
                        state['saved_posts'] = sum(r.get('type','post')!='comment' for r in rows)
                        state['last_saved_id'] = key(rows[-1]) if rows else None
                        store.save(rid, unit, rows, state)
                        if state.get('error'):
                            unit_errors[unit] = f'{platform} {keyword}: {state["error"]}'
                        else:
                            unit_errors.pop(unit, None)
                        all_rows = merge(all_rows, rows)
                        publish_progress()
                        if checkpoint:
                            checkpoint(all_rows)
                    persist()
                    own = {key(r) for r in rows if r.get('type','post') != 'comment'}
                    outside = {key(r) for r in all_rows if r.get('type','post') != 'comment' and key(r) not in own and r.get('collection_kind')!='revisit'}
                    platform_used = sum(k.startswith(platform+':') for k in outside)
                    target = platform_target(spec,platform)
                    if spec.count_scope=='per_platform':
                        target=max(0,target-platform_used)
                    elif spec.count_scope=='total':
                        target=min(max(0,spec.count-len(outside)),max(0,target-platform_used))
                    if stage == 'comments':
                        target = state.get('target', target)
                    outside |= historical - own
                    state['target'] = target
                    state['platform'],state['keyword'] = platform,keyword
                    if stage == 'posts' and platform in search_denials and not state.get('search_finished'):
                        state['error'] = search_denials[platform] + '；本轮暂停该平台后续搜索，请处理访问问题后续抓'
                        errors.append(f'{platform} {keyword}: {state["error"]}')
                        persist()
                        await asyncio.sleep(0)
                    if stage == 'posts' and target and not state.get('search_finished') and platform not in search_denials:
                        plans = list(partitions(spec, platform, keyword))
                        for index, (query, sort, start, end) in enumerate(plans):
                            if index < state.get('partition', 0):
                                continue
                            if cancelled():
                                raise asyncio.CancelledError
                            state['partition'] = index
                            def save_search(incoming):
                                current = {key(r) for r in rows if r.get('type','post') != 'comment'}
                                selected = []
                                for raw in incoming:
                                    r = {**raw, 'platform':platform, 'keyword':keyword, 'search_query':query}
                                    if r.get('type','post') == 'comment' or not in_window(r, start, end):
                                        continue
                                    k = key(r)
                                    if (str(r.get('tweet_id') or r.get('post_id') or '') in (seen_ids or set()) and k not in own) or k in outside or (k not in current and len(current) >= target):
                                        continue
                                    current.add(k)
                                    if spec.include_replies:
                                        saved = next((p for p in rows if key(p)==k),{})
                                        r.setdefault('comments_status',saved.get('comments_status','pending'))
                                    selected.append(r)
                                persist(selected)
                            # Reposition by larger prefixes after restart; IDs are committed every page.
                            requested = max(target, state.get('requested', target))
                            succeeded, fetched, last_requested = True, [], requested
                            for round_no in range(4):
                                if len({key(r) for r in rows if r.get('type','post')!='comment'}) >= target:
                                    state.pop('error',None)
                                    break
                                state['requested'] = requested
                                last_requested = requested
                                succeeded = False
                                for attempt in range(spec.max_retries+1):
                                    if cancelled():
                                        raise asyncio.CancelledError
                                    await acquire(platform)
                                    began, error = time.monotonic(), None
                                    try:
                                        with observe_rows(save_search):
                                            if spec.task_type == 'url_fetch':
                                                fetched, _ = await fetcher.fetch_user_tweets([query], count=requested, cookies=cookies, include_replies=False)
                                            elif platform == 'twitter':
                                                fetched, _ = await fetcher.search_tweets(query, count=requested, cookies=cookies, sort_by=sort, include_replies=False)
                                            else:
                                                fetched, _ = await fetcher.search_posts(query, count=requested, cookies=cookies,
                                                    sort='new' if (start or sort in ('live','new')) else 'hot', include_replies=False)
                                        save_search(fetched)
                                        if cancelled():
                                            raise asyncio.CancelledError
                                        state.pop('error', None)
                                        state['preflight'] = 'passed'
                                        succeeded = True
                                        break
                                    except Exception as exc:
                                        error = exc
                                        kind, retry, message = classify(exc)
                                        state['error'] = f'{kind}: {message}'
                                        if kind in {'access_denied', 'authentication'}:
                                            search_denials[platform] = state['error']
                                        persist()
                                        if not retry or attempt == spec.max_retries:
                                            break
                                    finally:
                                        Traffic.leave(platform, error, time.monotonic()-began)
                                if not succeeded:
                                    errors.append(f'{platform} {keyword}: {state["error"]}')
                                    break
                                if len(fetched) < requested or requested >= 10000:
                                    break
                                requested = min(10000, requested*2)
                                state['requested'] = requested
                            if succeeded and len(fetched) >= last_requested and sum(r.get('type','post')!='comment' for r in rows) < target:
                                state['error'] = 'scan_limit: 本轮扫描达到上限，可缩小日期范围后补抓'
                                errors.append(f'{platform}: {state["error"]}')
                            if state.get('error'):
                                break
                            state['partition'] = index+1
                            state.pop('requested',None)
                            persist()
                            if sum(r.get('type','post')!='comment' for r in rows) >= target:
                                break
                        state['search_finished'] = not bool(state.get('error'))
                        persist()
                    if stage == 'posts':
                        search_completed.add((platform, keyword))
                        publish_progress()
                    if stage == 'comments':
                        save = comment_saver(unit, platform, keyword)
                        comment_jobs.extend(CommentJob(dict(r), save) for r in rows
                                            if r.get('type', 'post') != 'comment' and r.get('comments_status') != 'finished')
                    actual = sum(r.get('type','post')!='comment' for r in rows)
                    if stage == 'posts' or comments_only:
                        counts.append({'platform':platform, 'keyword':keyword, 'requested_count':target,
                                       'actual_count':actual,'missing_count':max(0,target-actual),
                                       'status':'fulfilled' if actual>=target else 'shortfall'})
                    persist()
                    if stage == 'posts' and spec.batch_size and (keyword_index+1)%spec.batch_size==0 and keyword_index+1<len(spec.keywords):
                        await asyncio.sleep(spec.batch_cooldown)
                if stage == 'comments':
                    from src.crawlers.platform_config import PLATFORM_TIMING_MAP
                    async def fetch_parent(post):
                        nonlocal current_keyword
                        current_keyword = post.get('keyword', '')
                        publish_progress('comments')
                        return await fetch_comments(fetcher, platform, [post], cookies)
                    await drain_comments(comment_jobs, session, fetch_parent,
                                         lambda: acquire(platform),
                                         lambda error, elapsed: Traffic.leave(platform, error, elapsed),
                                         cancelled, PLATFORM_TIMING_MAP[platform])
                    store.compact_comments(rid)
                    if checkpoint:
                        checkpoint(all_rows)
                    await session.close()
                if platform_event and stage == stages[-1]:
                    platform_event('done',platform)
        if images:
            current_platform = current_keyword = ''
            publish_progress('images')
            await download_images()
        all_rows = store.rows(rid)
        if images:
            image_state = images.summary(rid)
            if image_state['images_pending'] or image_state['images_failed']:
                errors.append(f"图片待下载或失败 {image_state['images_pending'] + image_state['images_failed']} 个，可只补图片")
        main = [r for r in all_rows if r.get('type','post')!='comment']
        pending = sum(r.get('comments_status')!='finished' for r in main) if spec.include_replies else 0
        expected = expected_posts(spec)
        fresh = [r for r in main if r.get('collection_kind')!='revisit']
        missing = 0 if spec.probe else max(0, expected-len(fresh))
        if missing:
            errors.append(f'目标 {expected} 条去重主帖，当前新增 {len(fresh)} 条；可访问结果不足或分区未完成，可继续补抓')
        if pending:
            errors.append(f'{pending} 个主帖的评论待补抓')
        status = 'partial' if errors else 'success'
        current_platform = current_keyword = ''
        publish_progress('finished')
        store.status(rid,status,'；'.join(dict.fromkeys(errors)))
        if owner and status=='success':
            store.watermark(owner,stamp)
        return all_rows, {'count_results':counts, 'errors':errors, 'status':status,
            'requested_posts':expected, 'missing_posts':missing, 'comments_pending':pending,
            'comments_finished':len(main)-pending if spec.include_replies else 0, 'run_id':rid,
            'count_scope':spec.count_scope, 'date_filter_note': '日期范围按 UTC 日期、结束日期不含当天；Reddit 在可访问搜索结果内筛选，语言过滤仅作用于 X' if spec.since or spec.languages else ''}
    except asyncio.CancelledError:
        store.compact_comments(rid)
        if checkpoint:
            checkpoint(all_rows)
        publish_progress('interrupted')
        store.status(rid,'interrupted','已停止并保存检查点，可继续抓取')
        raise
    except Exception as exc:
        publish_progress('failed')
        store.status(rid,'failed',classify(exc)[2])
        raise
    finally:
        for session in comment_sessions.values():
            await session.close()
            session.fetcher._collection_comment_session = None
