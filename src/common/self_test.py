"""Offline release artifact smoke test. Runs only with --self-test REPORT.json."""
import asyncio
import base64
import csv
import io
import json
from pathlib import Path
from zoneinfo import ZoneInfo


async def check():
    import httpx
    from playwright.async_api import async_playwright
    from src.api import main as api
    from src.collection.engine import CollectionSpec
    from src.collection.store import Store
    from src.collection.api import result
    from src.common.version import get_version
    from src.crawlers.twitter_url import TwitterCookieFetcher
    from src.crawlers.reddit_crawler import RedditCookieFetcher
    assert ZoneInfo('Asia/Shanghai')
    api.check_for_update_async = lambda: None  # Offline test must not contact GitHub.
    async with api.lifespan(api.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://self-test') as client:
            for path in ['/', '/api/stats', '/api/audit/status', '/api/collections', '/api/scheduled-tasks']:
                response=await client.get(path)
                assert response.status_code==200, (path,response.status_code)
            assert (await client.get('/api/audit/status')).json()['configured'] is False
            assert (await client.post('/api/analyze-last')).status_code==410
        store=Store()
        rid=store.create(CollectionSpec(keywords=['smoke'],count=1).model_dump(mode='json'))
        store.save(rid,'unit',[{'platform':'twitter','keyword':'smoke','tweet_id':'1','content':'中文😀'}],{})
        store.status(rid,'success')
        data=result(store,rid)
        assert data['total_posts']==1 and base64.b64decode(data['xlsx_data']).startswith(b'PK')
        rows=list(csv.DictReader(io.StringIO(base64.b64decode(data['csv_data']).decode('utf-8-sig'))))
        assert rows[0]['content']=='中文😀'
        from PIL import Image
        from src.collection.images import Images
        import zipfile
        picture=io.BytesIO();Image.new('RGB',(2,2),'red').save(picture,format='PNG')
        image_run=store.create(CollectionSpec(keywords=['image'],content_mode='images').model_dump(mode='json'))
        images=Images(store)
        images.discover(image_run,[{'platform':'reddit','keyword':'image','post_id':'1','image_urls':['https://i.redd.it/self-test.png']}])
        await images.download(image_run,transport=httpx.MockTransport(lambda request:httpx.Response(200,content=picture.getvalue())))
        assert images.summary(image_run)['images_downloaded']==1
        archive=images.export(image_run,store.path.parent/'exports')
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None and json.loads(z.read('manifest.json'))['images'][0]['status']=='downloaded'
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page()
            await page.set_content('<main>bundled browser ready</main>')
            assert await page.locator('main').inner_text()=='bundled browser ready'
            await browser.close()
    return {'status':'passed','version':get_version(),'checks':['startup','database','static-ui','audit-disconnected',
             'retired-endpoint','checkpoint','csv','xlsx','tzdata','crawler-imports','image-download-validate-export','bundled-chromium']}


def run(report):
    try:
        outcome=asyncio.run(check())
    except Exception:
        import traceback
        Path(report).write_text(json.dumps({'status':'failed','error':traceback.format_exc()},ensure_ascii=False,indent=2),encoding='utf-8')
        raise
    Path(report).write_text(json.dumps(outcome,ensure_ascii=False,indent=2),encoding='utf-8')
