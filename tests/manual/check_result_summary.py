"""真实页面 + 模拟接口，检查采集结果只显示汇总和下载按钮。"""
import argparse
import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument('--executable')
parser.add_argument('--screenshot')
options = parser.parse_args()

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=options.executable, headless=True)
        page = await browser.new_page(viewport={'width': 1200, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        current = {}
        payload = base64.b64encode(b'export-test-data').decode()
        async def route(r):
            path = urlparse(r.request.url).path
            if path == '/':
                await r.fulfill(content_type='text/html', body=(ROOT / 'static/index.html').read_text())
                return
            data = {
                '/api/stats': dict(total=0, pending=0, approved=0, rejected=0),
                '/api/vocabulary': dict(items=[], total=0),
                '/api/chromium-status': dict(status='installed'),
                '/api/version': dict(version=(ROOT / 'VERSION').read_text().strip()),
                '/api/scheduled-notifications': dict(latest_id=0, notifications=[]),
                '/api/search': dict(task_id='test'), '/api/batch-search': dict(task_id='test'),
                '/api/twitter-fetch': dict(task_id='test'),
                '/api/task-status': current,
            }.get(path, {})
            await r.fulfill(content_type='application/json', body=json.dumps(data))
        await page.route('**/*', route)
        await page.add_init_script("""
          localStorage.setItem('tw_ct0','fake'); localStorage.setItem('tw_auth_token','fake');
          localStorage.setItem('rd_cookies','fake');
          window.downloads=[];
          window.pywebview={api:{report_ui_event:async()=>{}, save_file:async(data,name)=>{downloads.push({data,name});return name;}}};
        """)
        await page.goto('http://127.0.0.1:9876/?desktop=windows')
        await page.evaluate("showPage('twitter'); document.getElementById('search-keyword').value='test'; document.getElementById('twitter-urls').value='https://x.com/test'; document.querySelectorAll('#platform-checks input').forEach(c=>c.checked=true)")
        for mode in ['single', 'batch', 'profile']:
            container = page.locator('#twitter-result' if mode == 'profile' else '#multi-search-result')
            for status in ['partial', 'success', 'empty', 'cancelled', 'error']:
                current = {'status': 'success', 'result': {
                    'status': 'success' if status == 'partial' else status,
                    'total_posts': 51, 'total_rows': 127,
                    'count_results': [dict(platform='twitter', requested_count=50, actual_count=50, missing_count=0),
                                      dict(platform='reddit', requested_count=50, actual_count=1, missing_count=49 if status == 'partial' else 0)],
                    'sampled_posts': [{'content': 'PRIVATE_POST_BODY', 'author': 'PRIVATE_AUTHOR', 'media_urls': 'https://example.com/private.png'}],
                    'errors': ['https://x.com/private/status/123 Page.wait_for_selector: Timeout 15000ms locator(article)'] * 500 if status == 'partial' else [],
                    'csv_data': payload, 'xlsx_data': payload,
                    'csv_filename': 'test.csv', 'xlsx_filename': 'test.xlsx',
                    'message': 'PRIVATE_MESSAGE',
                }}
                if status == 'error':
                    current = {'status': 'error', 'error': 'PRIVATE_ERROR Timeout 15000ms'}
                elif status == 'empty':
                    current['result'].update(total_posts=0, total_rows=0, count_results=[], csv_data='', xlsx_data='', error='PRIVATE_ERROR')
                command = {'single': 'startMultiSearch()', 'batch': "startBatchSearch(['test','test2'])", 'profile': 'startTwitterFetch()'}[mode]
                await page.evaluate(command)
                await container.locator('.crawl-result').wait_for()
                text = await container.inner_text()
                assert len(text) < 1000, text
                for hidden in ['PRIVATE_', 'https://', 'wait_for_selector', 'Timeout', 'locator(']:
                    assert hidden not in text, text
                assert await container.locator('.post-card, img, .error-box').count() == 0
                assert '耗时' in text
                if status == 'partial':
                    assert '部分未完成' in text and '49' in text and '76' in text
                    assert await container.locator('.crawl-result.warning').count() == 1
                    before = len(await page.evaluate('downloads'))
                    await container.locator('[data-download="csv"]').click()
                    await container.locator('[data-download="xlsx"]').click()
                    records = (await page.evaluate('downloads'))[before:]
                    assert records == [{'data': payload, 'name': 'test.csv'}, {'data': payload, 'name': 'test.xlsx'}]
                    if options.screenshot and mode == 'single':
                        await container.screenshot(path=options.screenshot)
                if status == 'error':
                    button = '#twitter-fetch-btn' if mode == 'profile' else '#multi-search-btn'
                    assert await page.locator(button).is_enabled()
        assert not errors, errors
        await browser.close()
        print('PASS: 3 collection flows x 5 statuses; summaries only, 500 diagnostics hidden, correct totals, downloads work after elapsed badge, no JS errors')

asyncio.run(main())
