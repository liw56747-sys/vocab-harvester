"""更新交互浏览器验证：限流提示、手动下载、连续点击及正常升级入口。"""
import argparse
import asyncio
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
        page = await browser.new_page(viewport={'width': 1200, 'height': 800})
        errors, checks = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        info = {'error': '403 Client Error: raw stack https://api.github.com/private'}
        async def route(r):
            path = urlparse(r.request.url).path
            if path == '/':
                await r.fulfill(content_type='text/html', body=(ROOT / 'static/index.html').read_text())
                return
            if path == '/api/check-update':
                checks.append(1)
                await asyncio.sleep(0.05)
                data = info
            else:
                data = {
                    '/api/stats': dict(total=0, pending=0, approved=0, rejected=0),
                    '/api/vocabulary': dict(items=[], total=0),
                    '/api/chromium-status': dict(status='installed'),
                    '/api/version': dict(version=(ROOT / 'VERSION').read_text().strip()),
                    '/api/scheduled-notifications': dict(latest_id=0, notifications=[]),
                }.get(path, {})
            await r.fulfill(content_type='application/json', body=json.dumps(data))
        await page.route('**/*', route)
        await page.add_init_script("window.opened=[]; window.open=(url)=>opened.push(url);")
        await page.goto('http://127.0.0.1:9876/?desktop=windows')
        await page.evaluate("showPage('settings'); Promise.all(Array.from({length:5},()=>manualCheckUpdate()))")
        assert len(checks) == 1
        assert await page.locator('#update-fallback-text').is_visible()
        assert await page.locator('#update-action-btn').inner_text() == '打开下载页'
        text = await page.locator('body').inner_text()
        assert '403 Client Error' not in text and 'raw stack' not in text
        assert '当前已是最新版本' not in text
        await page.locator('#update-action-btn').click()
        assert await page.evaluate('opened') == ['https://github.com/liw56747-sys/vocab-harvester/releases/latest']
        if options.screenshot:
            await page.screenshot(path=options.screenshot)
        await page.evaluate('dismissUpdate()')
        assert await page.evaluate("localStorage.getItem('update_dismissed_version')") is None
        info = {'latest_version': '9.0.0', 'release_page': 'https://github.com/liw56747-sys/vocab-harvester/releases/tag/v9.0.0'}
        await page.evaluate('manualCheckUpdate()')
        assert await page.locator('#update-version').inner_text() == 'v9.0.0'
        assert await page.locator('#update-action-btn').inner_text() == '打开下载页'
        info['download_url'] = info['release_page'].replace('/tag/', '/download/') + '/vocab-harvester-9.0.0.dmg'
        await page.evaluate('manualCheckUpdate()')
        assert await page.locator('#update-action-btn').inner_text() == '一键更新'
        assert await page.evaluate("document.getElementById('update-action-btn').onclick === startOneClickUpdate")
        assert not await page.locator('#update-fallback-text').is_visible()
        info = {'up_to_date': True}
        await page.evaluate('manualCheckUpdate()')
        assert not await page.locator('#update-banner').is_visible()
        info = {'error': '403 raw error'}
        await page.evaluate("document.querySelectorAll('.toast').forEach(e=>e.remove()); reinstallAppFromChromium()")
        await page.locator('#update-fallback-text').wait_for(state='visible')
        assert '当前已是最新版本' not in await page.locator('body').inner_text()
        assert not errors, errors
        await browser.close()
        print('PASS: coalesced clicks, 403 hidden, official manual link, missing-asset fallback, normal one-click update, latest-version and reinstall error states')

asyncio.run(main())
