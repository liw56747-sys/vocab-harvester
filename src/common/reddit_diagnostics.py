"""Explicit read-only search probe from the actual packaged network runtime.
Never exports cookies, proxy credentials, response bodies or collected post text.
"""
import argparse
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from src.common.network_runtime import runtime_info


async def probe(config, keyword):
    from src.crawlers.reddit_crawler import RedditCookieFetcher
    from src.common.version import get_version
    report = {'runtime': runtime_info(), 'version': get_version(),
              'proxy_configured': bool(config.get('proxy')), 'requested_posts': 5}
    cookies = {k: v for k, v in config.items()
               if k in {'reddit_session', 'reddit_token', 'edgebucket', 'redesign_optout'} and v}
    cookies.update(config.get('extra_cookies') or {})
    if not cookies.get('reddit_session'):
        return {**report, 'status': 'failed', 'reason': 'missing_session', 'saved_posts': 0}
    fetcher = RedditCookieFetcher(proxy=config.get('proxy'))
    try:
        rows, _ = await asyncio.wait_for(fetcher.search_posts(
            keyword, count=5, cookies=cookies, sort='hot', include_replies=False), timeout=60)
        return {**report, 'status': 'passed', 'saved_posts': len(rows),
                'endpoint': fetcher._preferred_host}
    except Exception as exc:
        # Only export stable codes; exception strings can contain sensitive URLs.
        text = str(exc).lower()
        reason = ('access_denied' if '403' in text else 'rate_limit' if '429' in text
                  else 'timeout' if isinstance(exc, TimeoutError) or 'timeout' in text
                  else 'request_failed')
        return {**report, 'status': 'failed', 'saved_posts': 0, 'reason': reason,
                'exception_type': type(exc).__name__}


def cli(args):
    parser = argparse.ArgumentParser(description='Reddit read-only search diagnostic (no history writes)')
    parser.add_argument('report', type=Path)
    parser.add_argument('--keyword', default='technology')
    parser.add_argument('--data-dir', type=Path, default=Path.home()/'.vocab-harvester')
    opts = parser.parse_args(args)
    logging.disable(logging.CRITICAL)
    try:
        path = opts.data_dir.resolve()/'vocab.db'
        connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        try:
            row = connection.execute("SELECT cookie_json FROM platform_cookies WHERE platform='reddit'").fetchone()
        finally:
            connection.close()
        report = asyncio.run(probe(json.loads(row[0]) if row else {}, opts.keyword))
    except Exception as exc:
        report = {'status': 'failed', 'reason': 'diagnostic_setup_failed',
                  'runtime': runtime_info(), 'exception_type': type(exc).__name__}
    opts.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['status']=='passed' else 1
