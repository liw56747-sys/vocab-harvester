"""本地 HTTP 服务 + 真实 Chromium：模拟服务器轮换 ct0，无需 X 账号或外网。"""
import os
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from src.crawlers.browser_manager import BrowserPool, apply_request_interceptors


@pytest.mark.parametrize('speed', [False, True])
async def test_browser_follows_server_cookie_rotation_across_pages(speed):
    class Handler(BaseHTTPRequestHandler):
        rotation = 0

        def log_message(self, *args):
            pass

        def do_GET(self):
            jar = SimpleCookie(self.headers.get('Cookie', ''))
            current = jar.get('ct0')
            if self.path == '/api':
                ok = current and self.headers.get('x-csrf-token') == current.value
                self.send_response(200 if ok else 403)
            elif self.path == '/cdn':
                self.send_response(200 if not self.headers.get('x-csrf-token') else 403)
            else:
                self.send_response(200)
            if self.path == '/rotate':
                Handler.rotation += 1
                self.send_header('Set-Cookie', f'ct0=rotated-{Handler.rotation}; Path=/; SameSite=Lax')
            self.send_header('Content-Type', 'text/html')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'<html><body>Local session fixture</body></html>')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pool = BrowserPool(size=1)
    try:
        async with async_playwright() as pw:
            executable = os.environ.get('VOCAB_TEST_BROWSER_EXECUTABLE') or pw.chromium.executable_path
            if not Path(executable).exists():
                pytest.skip('需要已安装的 Playwright Chromium 或 VOCAB_TEST_BROWSER_EXECUTABLE')
            browser = await pw.chromium.launch(executable_path=executable, headless=True)
            pool.slots[0].browser = browser
            context = await pool.new_context(cookies={'ct0': 'initial', 'auth_token': 'fake-session'})
            try:
                # 复用生产代码生成的 Cookie 属性，仅将域名与传输设置适配本地 HTTP 服务。
                cookies = await context.cookies('https://x.com')
                for cookie in cookies:
                    cookie.update(domain='127.0.0.1', secure=False, sameSite='Lax')
                await context.add_cookies(cookies)
                origin = f'http://127.0.0.1:{server.server_port}'
                pages = [await context.new_page(), await context.new_page()]
                for page in pages:
                    await apply_request_interceptors(page, block_resources=speed, ct0_token='initial')
                    await page.goto(origin)
                    assert await page.evaluate("document.cookie.includes('ct0=initial')")
                    assert not await page.evaluate("document.cookie.includes('auth_token')")
                for page in pages:  # 主帖与评论页共享会话；每一轮均由服务器更新 Cookie。
                    await page.evaluate("fetch('/rotate')")
                    status = await page.evaluate("""async () => {
                        const ct0 = document.cookie.split('; ').find(c => c.startsWith('ct0=')).slice(4);
                        return (await fetch('/api', {headers: {'x-csrf-token': ct0}})).status;
                    }""")
                    assert status == 200
                    assert await page.evaluate(
                        'async url => (await fetch(url)).status',
                        f'http://localhost:{server.server_port}/cdn') == 200
            finally:
                await context.close()
                await pool.close_all()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
