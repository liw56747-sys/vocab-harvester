"""Bounded batches and fair passes, after every main-post search has finished."""
import asyncio
from dataclasses import dataclass
from typing import Callable

from src.crawlers.comment_policy import managed_attempt
from src.crawlers.comment_state import error_reason, replies
from src.crawlers.progress import observe_rows


@dataclass
class CommentJob:
    post: dict
    save: Callable[[list[dict]], None]


def comment_count(post):
    if 'image_comments_scanned' in post:
        return post['image_comments_scanned']
    return len(replies(post)) if post.get('platform') == 'twitter' else post.get('comments_fetched', 0)


async def drain_comments(jobs, session, fetch, acquire, release, cancelled, timing):
    """Two per-platform requests share one global gate. No unbounded task fan-out.

    Retry only after every parent has had a first attempt. A cancelled batch is
    fully joined before the browser/client or the global gate can be released.
    """
    pending = list(jobs)
    concurrency = timing.comment_concurrency
    for round_index, budget in enumerate(timing.comment_pass_budgets):
        retry = []
        start = 0
        while start < len(pending):
            if cancelled():
                raise asyncio.CancelledError
            batch = pending[start:start + concurrency]
            start += len(batch)
            ready = []
            for job in batch:
                if session.pause_post(job.post):
                    job.save([job.post])
                else:
                    ready.append(job)
            if not ready:
                await asyncio.sleep(0)
                continue
            await acquire()
            # Re-check after waiting for another task's platform gate.
            if cancelled():
                release(None, 0)
                raise asyncio.CancelledError
            import time
            began = time.monotonic()
            outcomes = []

            async def attempt(job):
                post = job.post
                if session.pause_post(post):
                    job.save([post])
                    return
                before = comment_count(post)
                post.update(comments_active=True, comments_attempted=True,
                            comments_round=round_index + 1)
                job.save([post])
                error = None
                try:
                    with managed_attempt(), observe_rows(job.save):
                        async with asyncio.timeout(budget):
                            updated = await fetch(post)
                    extra = [row for row in updated if row is not post]
                    if extra:
                        job.save(extra)
                except TimeoutError:
                    post.update(comments_status='partial' if comment_count(post) else 'failed',
                                comments_reason='time_slice',
                                comments_warning='本轮单帖处理时间已用完，已保存进度并让出队列；未确认评论完整')
                except asyncio.CancelledError:
                    if post.get('comments_status') != 'finished':
                        post.update(comments_status='partial' if comment_count(post) else 'failed',
                                    comments_reason='interrupted', comments_warning='已停止，已保存评论，可继续补抓')
                    raise
                except Exception as exc:
                    error = exc
                    post.update(comments_status='partial' if comment_count(post) else 'failed',
                                comments_reason=error_reason(exc)[0],
                                comments_warning='评论采集异常，已保存结果，可继续补抓')
                finally:
                    post['comments_active'] = False
                    session.record(post)
                    job.save([post])
                reason = post.get('comments_reason') or error_reason(post.get('comments_warning', ''))[0]
                outcomes.append((error, reason))
                if post.get('comments_status') == 'finished' or session.pause_reason:
                    return
                gained = comment_count(post) > before
                # A budget exhaustion may only have revisited an old prefix.
                # Give it a longer pass; exhausted/unchanged complete traversals
                # and inaccessible comments must not loop indefinitely.
                if reason == 'time_slice' or (reason in {'network', 'platform'} and (round_index == 0 or gained)) or (reason == 'shortfall' and gained):
                    retry.append(job)

            tasks = [asyncio.create_task(attempt(job)) for job in ready]
            joined = asyncio.gather(*tasks)
            try:
                while not joined.done():
                    if cancelled():
                        raise asyncio.CancelledError
                    await asyncio.wait([joined], timeout=.2)
                await joined
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # Retrieve the group exception too, including on cancellation.
                await asyncio.gather(joined, return_exceptions=True)
                # A success in the same batch must not hide a transient error.
                from src.collection.comment_session import CommentOutcomeError
                failure = next((CommentOutcomeError({'comments_reason': reason})
                                for _, reason in outcomes if reason in {'network', 'platform', 'rate_limit'}), None)
                release(failure, time.monotonic() - began)
            if any(reason in {'network', 'platform'} for _, reason in outcomes):
                concurrency = 1
        pending = retry
        if not pending:
            break
