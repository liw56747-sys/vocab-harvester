"""Real Chromium, simulated virtual X threads: adaptive waits and delta chunks."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.async_api import async_playwright
from src.crawlers import twitter_url
from src.crawlers.comment_policy import managed_attempt
from src.crawlers.progress import observe_rows

HTML = r'''<!doctype html><html><body><main></main><script>
let batch=0; const parent=__POST__;
function article(id,text) { return `<article data-testid="tweet"><div data-testid="User-Name"><a href="/tester"><span>Tester</span></a></div><a href="/tester/status/${id}"><time datetime="2026-10-26T00:00:00Z"></time></a><div data-testid="tweetText">${text}</div><div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/${id}.jpg"></div></article>`; }
function render(){ document.querySelector('main').innerHTML=article(parent,'root')+Array.from({length:20},(_,i)=>article(parent*1000+batch*20+i+1,'reply '+(batch*20+i))).join(''); }
window.scrollBy=()=>{ if(batch<4){batch++;render();} };render();
</script></body></html>'''

async def main():
    timing=replace(twitter_url._TC,search_initial_wait=.01,comment_min_interval=.01,
                   comment_scroll_min=.05,comment_scroll_max=.05,comment_stall_wait=.01,
                   comment_more_wait=.01,comment_ready_poll_ms=10)
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        context=await browser.new_context()
        errors=[]
        image_mode=False
        context.on('page',lambda page:page.on('pageerror',lambda e:errors.append(str(e))))
        async def route(r):
            if r.request.resource_type=='document':
                pid=r.request.url.rsplit('/',1)[-1]
                body=HTML.replace('__POST__',pid)
                if image_mode:
                    body=body.replace('function article(id,text) { return',
                        'function article(id,text) { const media=id%20===0 ? \'<div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/\'+id+\'.jpg"></div>\' : \'\'; return')
                    body=body.replace('<div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/${id}.jpg"></div>','${media}')
                await r.fulfill(content_type='text/html',body=body)
            else:
                await r.abort()
        await context.route('**/*',route)
        posts=[dict(platform='twitter',tweet_id=str(i),url=f'https://x.com/tester/status/{i}',replies=100) for i in [1000,2000]]
        chunks=[]
        with patch.object(twitter_url,'_TC',timing), managed_attempt(), observe_rows(lambda rows:chunks.extend(rows)):
            await twitter_url.TwitterCookieFetcher()._parallel_scrape_replies(context,posts)
        assert all(post['comments_status']=='finished' and len(post['replies_data'])==100 for post in posts),[(p['comments_status'],p['replies_count']) for p in posts]
        assert len(chunks)==10 and all(len(c['replies_data'])==20 for c in chunks)
        assert len({r['tweet_id'] for post in posts for r in post['replies_data']})==200
        assert all(int(post['tweet_id'])*1000 < int(r['tweet_id']) <= int(post['tweet_id'])*1000+100 for post in posts for r in post['replies_data'])
        assert all(len(r['image_urls'])==1 for post in posts for r in post['replies_data'])
        assert not context.pages and not errors,(len(context.pages),errors)
        image_mode=True
        posts=[dict(platform='twitter',tweet_id=str(i),url=f'https://x.com/tester/status/{i}',replies=10000) for i in [3000,4000]]
        fetcher=twitter_url.TwitterCookieFetcher();fetcher._image_collection=True
        chunks=[]
        with patch.object(twitter_url,'_TC',timing), managed_attempt(), observe_rows(lambda rows:chunks.extend(rows)):
            await fetcher._parallel_scrape_replies(context,posts)
        assert all(post['comments_status']=='finished' and post['image_comments_scanned']==100 for post in posts),posts
        assert all(len(post['replies_data'])==5 and post['image_scan_exhausted'] for post in posts)
        assert len(chunks)==10 and all(len(c['replies_data'])==1 for c in chunks)
        assert all(not r['content'] for post in posts for r in post['replies_data'])
        assert not context.pages and not errors,(len(context.pages),errors)
        await context.close();await browser.close()
    print('PASS: real Chromium, text traversal 200 replies; sparse image traversal 200 checks/10 images, no count-based retry, adaptive waits and page cleanup')

asyncio.run(main())
