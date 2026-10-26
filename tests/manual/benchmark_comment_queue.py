"""Deterministic 300-parent/30k-comment engine benchmark, no real accounts.
Compare one/two lanes using the same data, waits and current persistence path.
Also compare full-unit versus delta serialization volume on 5k reply inserts within a 50-parent group.
"""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.collection import engine
from src.collection.engine import CollectionSpec, Traffic
from src.collection.store import Store
from src.crawlers.platform_config import twitter_config
from src.crawlers.progress import report_rows
from src.crawlers.twitter_url import TwitterCookieFetcher


async def measure(lanes):
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder)/'collection.db')
        spec = CollectionSpec(keywords=['a','b','c','d','e','f'], count=50, include_replies=True)
        rid = store.create(spec.model_dump(mode='json'))
        active = peak = calls = 0
        first_comment_at = None
        async def search(self, keyword, count, **kwargs):
            return [dict(tweet_id=f'{keyword}-{i}', post_id=f'{keyword}-{i}') for i in range(count)], ''
        async def comments(fetcher, platform, posts, cookies):
            nonlocal active, peak, calls, first_comment_at
            p = posts[0]; active += 1; peak = max(peak, active); calls += 1
            if first_comment_at is None:
                assert len(store.rows(rid)) == 300
                first_comment_at = time.perf_counter()
            p['replies_data'] = []
            try:
                for start in range(0,100,20):
                    await asyncio.sleep(.005)
                    delta = [{'tweet_id': f'{p["tweet_id"]}-reply-{i}', 'content':'simulated reply'} for i in range(start,start+20)]
                    p['replies_data'].extend(delta)
                    report_rows([{**p, 'replies_data':delta}])
                p.update(comments_status='finished', comments_reason='')
                return posts
            finally:
                active -= 1
        Traffic.active.clear(); Traffic.delay.clear(); Traffic.until.clear()
        timing = replace(twitter_config, comment_concurrency=lanes)
        began = time.perf_counter()
        with patch.object(TwitterCookieFetcher, 'search_tweets', search), patch.object(engine, 'fetch_comments', comments), patch.dict('src.crawlers.platform_config.PLATFORM_TIMING_MAP', twitter=timing):
            rows, info = await engine.collect(store,rid,{'twitter':{'ct0':'fake','auth_token':'fake'}})
        duration = time.perf_counter()-began
        assert info['status']=='success' and len(rows)==300 and active==0 and peak==lanes
        assert all(len(p['replies_data'])==100 and all(r['tweet_id'].startswith(p['tweet_id']+'-reply-') for r in p['replies_data']) for p in rows)
        reopened = Store(store.path)
        assert sum(len(p['replies_data']) for p in reopened.rows(rid))==30000
        assert not Traffic.active
        return dict(lanes=lanes,seconds=round(duration,3),comment_seconds=round(time.perf_counter()-first_comment_at,3),parents=300,comments=30000,peak_active=peak,calls=calls)


async def main(path):
    results=[]
    for lanes in [1,2,2,1]:
        result=await measure(lanes);results.append(result);print(json.dumps(result),flush=True)
    # JSON volume in a fixed 50-parent group, with 5 additions per parent.
    group=[dict(tweet_id=str(i),replies_data=[]) for i in range(50)]
    old_bytes=delta_bytes=0
    for post in group:
        for start in range(0,100,20):
            chunk=[dict(tweet_id=f'{post["tweet_id"]}-{i}',content='reply') for i in range(start,start+20)]
            post['replies_data'].extend(chunk)
            old_bytes+=len(json.dumps(group).encode())
            delta_bytes+=len(json.dumps({**post,'replies_data':chunk}).encode())
    report=dict(scope='simulation; current engine one/two lanes; not real platform speed',runs=results,
                serialization=dict(parents=50,comments=5000,legacy_full_group_bytes=old_bytes,delta_chunk_bytes=delta_bytes),
                final_comments_each_run=30000)
    Path(path).parent.mkdir(parents=True,exist_ok=True);Path(path).write_text(json.dumps(report,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--report',required=True)
    asyncio.run(main(parser.parse_args().report))
