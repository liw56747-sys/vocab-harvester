"""Compare v1.9.3 with working tree using identical local simulated platform/CDN latency.

Measures scheduling/storage/download overlap, not real platform traversal speed.
No live credentials or Internet requests are used.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from PIL import Image
from src.collection import engine
from src.collection.images import Images
from src.collection.store import Store
from src.crawlers.comment_state import comment_summary
from src.crawlers.progress import report_rows
from src.crawlers.twitter_url import TwitterCookieFetcher
from src.crawlers.reddit_crawler import RedditCookieFetcher


async def measure(module, label):
    with tempfile.TemporaryDirectory(prefix='image-pipeline-bench-') as folder:
        with patch.dict(os.environ, VOCAB_DATA_DIR=folder):
            store = Store()
            spec = module.CollectionSpec(keywords=['local'], platforms=['twitter', 'reddit'], count=30, content_mode='images')
            rid = store.create(spec.model_dump(mode='json'))
            module.Traffic.active.clear(); module.Traffic.delay.clear(); module.Traffic.until.clear()
            output = io.BytesIO(); Image.new('RGB', (4, 3), 'red').save(output, format='PNG')
            started = time.monotonic(); first_image = None; requests = set()
            async def search(fetcher, keyword, count, **kwargs):
                platform = 'twitter' if isinstance(fetcher, TwitterCookieFetcher) else 'reddit'
                await asyncio.sleep(.01)
                return [dict(tweet_id=f'{platform}-{i}', post_id=f'{platform}-{i}', image_urls=[]) for i in range(count)], ''
            async def comments(fetcher, platform, posts, cookies):
                post = posts[0]; ident = post['tweet_id']
                await asyncio.sleep(.02)
                replies = [dict(tweet_id=f'{ident}-{i}', comment_id=f'{ident}-{i}', type='comment',
                    post_id=ident, content='transient text ' * 30,
                    image_urls=[f'https://i.redd.it/{ident}.png'] if i == 49 else []) for i in range(50)]
                post.update(comments_status='finished', comments_fetched=50)
                if platform == 'twitter':
                    post['replies_data'] = replies
                    report_rows([post]); return posts
                report_rows([post, *replies]); return [post, *replies]
            async def response(request):
                nonlocal first_image
                await asyncio.sleep(.008)
                if first_image is None: first_image = time.monotonic() - started
                requests.add(str(request.url))
                return httpx.Response(200, content=output.getvalue())
            original = Images.download
            async def download(images, *args, **kwargs):
                return await original(images, *args, **kwargs, transport=httpx.MockTransport(response))
            with patch.object(TwitterCookieFetcher, 'search_tweets', search), \
                 patch.object(RedditCookieFetcher, 'search_posts', search), \
                 patch.object(module, 'fetch_comments', comments), patch.object(Images, 'download', download):
                rows, info = await module.collect(store, rid,
                    {'twitter': {'ct0': 'fake', 'auth_token': 'fake'}, 'reddit': {'reddit_session': 'fake'}})
            summary = Images(store).summary(rid)
            assert info['status'] == 'success' and summary['images_downloaded'] == 60
            assert len(requests) == 60 and len(Images(store).manifest(rid)) == 60
            return dict(mode=label, seconds=round(time.monotonic()-started, 3),
                first_image_seconds=round(first_image, 3), downloaded=60,
                retained_comment_records=comment_summary(rows)['total_comments'])


async def main():
    baseline = types.ModuleType('baseline_image_engine')
    sys.modules[baseline.__name__] = baseline
    source = subprocess.check_output(['git', 'show', 'v1.9.3:src/collection/engine.py'], cwd=ROOT, text=True)
    exec(compile(source, 'v1.9.3:src/collection/engine.py', 'exec'), baseline.__dict__)
    results = []
    for module, label in [(baseline, 'v1.9.3'), (engine, 'working-tree'), (engine, 'working-tree'), (baseline, 'v1.9.3')]:
        results.append(await measure(module, label))
    report = {'scenario': '60 parents / 3000 scanned comments / 60 image URLs; local simulated latency',
              'limitation': 'Does not measure real X/Reddit pagination, rate limiting or network throughput.',
              'results': results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if len(sys.argv) > 1: Path(sys.argv[1]).write_text(json.dumps(report, ensure_ascii=False, indent=2))


asyncio.run(main())
