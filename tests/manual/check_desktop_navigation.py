"""可选浏览器回归：python tests/manual/check_desktop_navigation.py [--executable PATH]。
只模拟本地页面与 API，不使用账号或访问平台。需要 Playwright Chromium。
"""
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
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        async def route(r):
            path = urlparse(r.request.url).path
            if path == '/':
                await r.fulfill(content_type='text/html', body=(ROOT / 'static/index.html').read_text())
                return
            data = {
                '/api/stats': dict(total=0, pending=0, approved=0, rejected=0),
                '/api/vocabulary': dict(items=[], total=0),
                '/api/vocabulary/filter-options': {},
                '/api/scheduled-tasks': dict(tasks=[]),
                '/api/chromium-status': dict(status='installed'),
                '/api/version': dict(version=(ROOT / 'VERSION').read_text().strip()),
                '/api/scheduled-notifications': dict(latest_id=0, notifications=[]),
            }.get(path, {})
            await r.fulfill(content_type='application/json', body=json.dumps(data))
        await page.route('**/*', route)
        await page.add_init_script('window.uiEvents=[]; window.pywebview={api:{report_ui_event:async e => window.uiEvents.push(e)}};')
        await page.goto('http://127.0.0.1:9876/?desktop=windows')
        await page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
        names = ['dashboard', 'twitter', 'import', 'vocabulary', 'schedule', 'export', 'settings']
        for i in range(140):
            name = names[i % len(names)]
            await page.locator(f'.nav-item[onclick*="showPage(\'{name}\'"]').click()
            assert await page.locator('.page.active').count() == 1
            assert await page.locator('.nav-item.active').count() == 1
            assert await page.locator('#page-' + name).is_visible()
            assert await page.locator('.topbar').is_visible()
        assert await page.evaluate("getComputedStyle(document.querySelector('.page.active')).animationName") == 'none'
        assert await page.evaluate("getComputedStyle(document.querySelector('.topbar')).backdropFilter") == 'none'
        await page.evaluate("showPage('does-not-exist', null)")
        assert await page.locator('#page-settings').is_visible()
        # 注入同步/异步加载错误，确认导航不会丢失且诊断能捕获。
        await page.evaluate("loadStats=()=>{throw new TypeError('TEST_SECRET')}; loadSettings=async()=>{throw new Error('TEST_SECRET')}; showPage('dashboard', null); showPage('settings', null)")
        await page.wait_for_function("window.uiEvents.filter(e=>e.kind==='page_load_error').length===2")
        assert await page.locator('#page-settings').is_visible()
        assert 'TEST_SECRET' not in json.dumps(await page.evaluate('window.uiEvents'))
        assert not errors, errors
        if options.screenshot:
            await page.screenshot(path=options.screenshot)
        await page.goto('http://127.0.0.1:9876/')
        assert not await page.evaluate("document.documentElement.classList.contains('windows-webview')")
        assert await page.evaluate("getComputedStyle(document.querySelector('.page.active')).animationName") == 'fadeIn'
        assert not errors, errors
        await browser.close()
        print('PASS: 140 navigation clicks, invalid target, sync/async failures, private diagnostics, Windows-only CSS; no JS/console errors')

asyncio.run(main())
