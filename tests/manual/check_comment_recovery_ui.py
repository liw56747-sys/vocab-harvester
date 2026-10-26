"""Real browser: recovery polling, stale results, network failure and duplicate clicks."""
import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width':1300,'height':1100})
        errors = []; counts = {'history':0,'resume':0,'export':0}
        page.on('pageerror', lambda error: errors.append(str(error)))
        state = {'id':'run','run_id':'run','created':'2026-10-16T05:10:23+00:00',
                 'status':'partial','active':False,'saved_posts':100,'total_posts':100,
                 'requested_posts':100,'missing_posts':0,'total_comments':2902,'total_rows':3002,
                 'comments_finished':25,'comments_pending':75,'comments_enabled':True,'new_comments':0,
                 'comments_unstarted':30,'comments_in_progress':2,'comments_partial':40,'comments_failed':3}
        fail = False
        history_gate = asyncio.Event(); history_gate.set()
        export_gate = asyncio.Event(); export_gate.set()
        resume_gate = asyncio.Event(); resume_gate.set()
        async def route(r):
            nonlocal fail
            path = urlparse(r.request.url).path
            if path == '/':
                await r.fulfill(content_type='text/html', body=(ROOT/'static/index.html').read_text(encoding='utf-8')); return
            data = {}
            if path == '/api/collections':
                counts['history'] += 1
                if fail:
                    fail=False
                    await r.fulfill(status=503,body='unavailable'); return
                data = {'runs':[dict(state)]}
                await history_gate.wait()
            elif path.endswith('/resume'):
                counts['resume'] += 1
                await resume_gate.wait()
                state.update(status='running',active=True,new_comments=0)
                data={'status':'started','task_id':'run'}
            elif path.endswith('/result'):
                counts['export'] += 1
                await export_gate.wait()
                data={**state,'csv_data':base64.b64encode(b'export').decode(),'csv_filename':'test.csv'}
            elif path == '/api/chromium-status':data={'status':'installed'}
            elif path == '/api/audit/status':data={'configured':False,'status':'not_configured'}
            await r.fulfill(content_type='application/json',body=json.dumps(data))
        await page.route('**/*', route)
        await page.goto('http://127.0.0.1:9876/')
        await page.evaluate("showPage('twitter');document.getElementById('collection-history-panel').open=true")
        await page.locator('[data-comments="run"]').wait_for()
        await page.evaluate('(s)=>renderCrawlResult("multi-search-result",s)', state)
        text = await page.locator('#multi-search-result').inner_text()
        assert '评论已完成主帖：25' in text and '未开始：30' in text and '处理中：2' in text
        assert '部分完成：40' in text and '失败或中断：3' in text
        assert '不能除以已完成主帖数' in text
        # A refresh already in flight must not suppress the refresh after starting recovery.
        history_gate.clear()
        await page.evaluate("void loadCollectionHistory()")
        resume_gate.clear()
        await page.evaluate("document.querySelector('[data-comments=run]').click();document.querySelector('[data-comments=run]').click()")
        await page.wait_for_function("document.querySelector('[data-comments=run]').disabled")
        assert counts['resume'] == 1
        resume_gate.set()
        await page.wait_for_function("document.getElementById('multi-search-result').textContent.includes('正在采集或补抓')")
        assert '本轮采集完成' not in await page.locator('#multi-search-result').inner_text()
        history_gate.set()
        await page.wait_for_function("document.getElementById('collection-history').textContent.includes('运行中')",timeout=10000)
        state.update(status='partial',active=False,comments_pending=5,comments_finished=95,new_comments=700,
                     comments_reasons=[{'code':'unavailable','label':'帖子或评论不可访问','count':5}])
        await page.wait_for_function("document.getElementById('collection-history-result').textContent.includes('待补抓：5')",timeout=10000)
        text = await page.locator('#collection-history-result').inner_text()
        assert '本轮已结束，仍有缺口' in text and '本轮新增评论：700' in text
        assert '不可访问：5' in text and counts['export'] == 0
        export_gate.clear()
        await page.locator('[data-result="run"]').click()
        await page.evaluate('loadCollectionHistory()')
        assert await page.locator('[data-result="run"]').is_disabled()
        await page.evaluate("document.querySelector('[data-result=run]').click()")
        export_gate.set()
        await page.locator('#collection-history-result [data-download="csv"]').wait_for()
        assert await page.locator('[data-result="run"]').is_enabled()
        assert counts['export'] == 1
        await page.locator('[data-comments="run"]').click()
        await page.wait_for_function("document.getElementById('collection-history-result').textContent.includes('正在采集或补抓')")
        fail=True
        await page.wait_for_function("document.getElementById('collection-history').textContent.includes('进度暂时无法刷新')",timeout=10000)
        state.update(status='success',active=False,comments_pending=0,comments_finished=100,new_comments=50,comments_reasons=[])
        await page.wait_for_function("document.getElementById('collection-history-result').textContent.includes('可访问评论已处理完成')",timeout=10000)
        assert '待补抓：0' in await page.locator('#collection-history-result').inner_text()
        # Stable timer cleanup after folding the history panel.
        await page.evaluate("document.getElementById('collection-history-panel').open=false")
        before=counts['history']; await page.wait_for_timeout(3300)
        assert counts['history']==before and not errors, (counts, errors)
        await browser.close()
        print('PASS: running vs ended vs complete, automatic progress, 503 recovery, no polling exports, duplicate-click guard, stale-result replacement and timer cleanup')

asyncio.run(main())
