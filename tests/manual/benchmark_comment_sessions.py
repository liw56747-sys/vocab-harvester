"""Local Chromium lifecycle comparison; no platform traffic or real account data."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.collection.engine import fetch_comments
from src.collection.comment_session import CommentSession
from src.crawlers.browser_manager import BrowserManager
from src.crawlers.twitter_url import TwitterCookieFetcher

async def main(args):
    results=[]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        async def local_replies(self,page,url,progress):
            # Exercise real page creation/render/cleanup, with identical local content.
            await page.set_content('<article>Local comment</article>')
            assert await page.locator('article').inner_text()=='Local comment'
            progress.update(comments_status='finished',comments_reason='',comments_warning='',
                            replies_count=1,replies_data=[{'tweet_id':progress['tweet_id']+'-reply','content':'Local comment'}])
        try:
            for round_no in range(args.rounds):
                for mode in (['per_parent','reuse'] if round_no%2==0 else ['reuse','per_parent']):
                    contexts=[];pages=[];fetcher=TwitterCookieFetcher();session=None
                    cookies={'ct0':'local-test','auth_token':'local-test'}
                    async def context(**kwargs):
                        ctx=await browser.new_context();contexts.append(ctx)
                        ctx.on('page',lambda p:pages.append(p))
                        await ctx.route('**/*',lambda route:route.abort())
                        return ctx
                    if mode=='reuse':
                        session=CommentSession(fetcher,'twitter',cookies)
                        fetcher._collection_comment_session=session
                    start=time.perf_counter()
                    try:
                        with patch.object(BrowserManager,'get',return_value=SimpleNamespace(new_context=context)),patch.object(TwitterCookieFetcher,'_scrape_replies_page',local_replies):
                            for i in range(args.posts):
                                posts=[{'tweet_id':str(i),'url':f'https://x.com/i/status/{i}'}]
                                await fetch_comments(fetcher,'twitter',posts,cookies)
                                assert posts[0]['comments_status']=='finished'
                                assert posts[0]['replies_data'][0]['tweet_id']==str(i)+'-reply'
                                assert sum(len(c.pages) for c in contexts)<=(1 if session else 0)
                                if session:assert session.idle_pages[0].url=='about:blank'
                    finally:
                        if session:await session.close()
                    elapsed=time.perf_counter()-start
                    assert not browser.contexts,'context leaked'
                    expected=(args.posts+19)//20 if session else args.posts
                    assert len(contexts)==expected and len(pages)==expected
                    result={'round':round_no+1,'mode':mode,'posts':args.posts,'contexts_created':len(contexts),
                            'pages_created':len(pages),'seconds':round(elapsed,3),'remaining_contexts':len(browser.contexts)}
                    results.append(result);print(json.dumps(result),flush=True)
        finally:
            await browser.close()
    medians={mode:round(statistics.median(r['seconds'] for r in results if r['mode']==mode),3) for mode in ['per_parent','reuse']}
    report={'scope':'Local Chromium with simulated comment content; excludes real platform/network/scrolling delays.',
            'results':results,'median_seconds':medians,
            'local_elapsed_reduction_percent':round(100*(1-medians['reuse']/medians['per_parent']),1)}
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='results'}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--posts',type=int,default=60)
    parser.add_argument('--rounds',type=int,default=3);parser.add_argument('--report',required=True)
    asyncio.run(main(parser.parse_args()))
