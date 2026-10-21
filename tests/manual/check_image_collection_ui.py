"""Actual Chromium: native image extraction and image mode/export/retry UI."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[2]

async def main():
    import sys
    sys.path.insert(0,str(ROOT))
    from src.crawlers.twitter_url import _EXTRACT_TWEETS_JS, TwitterCookieFetcher
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        page=await browser.new_page(viewport={'width':1300,'height':1100})
        await page.set_content('''<article data-testid="tweet"><a href="/u/status/123">post</a>
          <img src="https://pbs.twimg.com/profile_images/avatar.png">
          <div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/main?format=png&name=small"></div>
          <div data-testid="quoteTweet"><div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/quoted?format=png"></div></div>
          <div data-testid="videoPlayer"><video poster="https://pbs.twimg.com/video_thumb/cover.jpg"></video></div>
          </article><article data-testid="tweet"><a href="/u/status/456">reply</a>
          <div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/reply?format=png"></div></article>
          <article data-testid="tweet"><a href="/u/status/789">reply</a><div data-testid="tweetText">文字配图</div>
          <div data-testid="tweetPhoto"><img src="https://pbs.twimg.com/media/text-reply?format=png"></div></article>''')
        posts=await page.evaluate(_EXTRACT_TWEETS_JS,{'alreadySeenIds':[],'skipMedia':False})
        assert posts[0]['image_urls']==['https://pbs.twimg.com/media/main?format=png&name=small'],posts
        replies=await page.evaluate(TwitterCookieFetcher._EXTRACT_REPLIES_JS,['123'])
        assert len(replies)==2 and all(len(r['imageUrls'])==1 for r in replies)
        assert replies[0]['text']=='' and replies[1]['text']=='文字配图'
        errors=[];requests=[];exports=0
        export_gate=asyncio.Event();export_gate.clear()
        state={'id':'images','run_id':'images','created':'2026-10-21','status':'running','active':True,
               'content_mode':'images','saved_posts':100,'total_posts':100,'requested_posts':600,'missing_posts':500,
               'images_found':120,'images_downloaded':90,'images_pending':20,'images_failed':10,'image_files':60,
               'comments_pending':40,'comments_finished':60,'total_comments':800}
        async def route(r):
            nonlocal exports
            path=urlparse(r.request.url).path
            if path=='/':
                await r.fulfill(content_type='text/html',body=(ROOT/'static/index.html').read_text(encoding='utf-8'));return
            if r.request.method=='POST':requests.append((r.request.url,r.request.post_data_json if r.request.post_data else None))
            data={}
            if path=='/api/chromium-status':data={'status':'installed'}
            elif path=='/api/collections':data={'runs':[state]}
            elif path.endswith('/result'):data=state
            elif path.endswith('/images/export'):
                exports+=1;await export_gate.wait();data={'status':'ok','path':'/Downloads/images.zip'}
            elif path.endswith('/resume'):data={'status':'started'}
            elif path=='/api/scheduled-tasks':data={'status':'ok','tasks':[]}
            await r.fulfill(content_type='application/json',body=json.dumps(data))
        await page.route('**/*',route)
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.goto('http://127.0.0.1:9876/')
        await page.evaluate("showPage('twitter')")
        await page.locator('#collect-mode').evaluate("el=>el.closest('details').open=true")
        await page.locator('#collect-mode').select_option('images')
        assert await page.locator('#search-include-replies').is_checked()
        assert await page.locator('#search-include-replies').is_disabled()
        assert await page.locator('#user-block-resources').is_disabled()
        assert (await page.evaluate("getCollectionOptions('collect')"))['content_mode']=='images'
        await page.evaluate("document.getElementById('collection-history-panel').open=true")
        await page.locator('[data-result="images"]').click()
        await page.locator('[data-image-export]').wait_for()
        assert await page.locator('#collection-history-result [data-download]').count()==0
        await page.wait_for_timeout(3400)
        assert await page.locator('[data-image-export]').count()==1
        await page.locator('[data-image-export]').click()
        await page.evaluate("loadCollectionHistory()")
        assert await page.locator('[data-image-export]').is_disabled()
        await page.evaluate("document.querySelector('[data-image-export]').click()")
        assert exports==1
        export_gate.set()
        await page.wait_for_function("!document.querySelector('[data-image-export]').disabled")
        state.update(status='partial',active=False)
        await page.evaluate('loadCollectionHistory()')
        await page.locator('[data-images="images"]').click()
        assert any('images_only=true' in url for url,_ in requests)
        await page.evaluate("showPage('schedule');showScheduleForm()")
        await page.locator('#schedule-name').fill('图片任务')
        await page.locator('#schedule-keywords').fill('a\nb\nc\nd\ne\nf')
        await page.locator('#schedule-collect-mode').evaluate("el=>el.closest('details').open=true")
        await page.locator('#schedule-collect-mode').select_option('images')
        await page.evaluate('openFetchConfigModal()')
        assert await page.locator('#modal-include-replies').is_checked()
        assert await page.locator('#modal-block-resources').is_disabled()
        await page.evaluate('confirmFetchConfig();saveScheduleTask()')
        await page.wait_for_timeout(100)
        saved=next(body for url,body in requests if url.endswith('/api/scheduled-tasks'))
        assert saved['params']['content_mode']=='images' and saved['params']['include_replies']
        assert not saved['params']['block_resources']
        assert not errors,errors
        await browser.close()
        print('PASS: X main/text+image/image-only replies, thumbnail exclusion, image mode, summary polling, export, retry and scheduled payload')

asyncio.run(main())
