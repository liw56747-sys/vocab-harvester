"""Interaction and visual regression of the desktop workspace; no live accounts."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'work/ui-refresh-2026-10-22'

async def main():
    OUT.mkdir(exist_ok=True)
    state = dict(id='demo',run_id='demo',created='2026-10-22T05:00:00Z',status='running',active=True,
        saved_posts=400,total_posts=400,requested_posts=400,missing_posts=0,keyword_count=4,platforms=['twitter','reddit'],
        phase='comments',current_platform='twitter',current_keyword='行业话题',last_progress_at='2026-10-22T06:15:00Z',
        comments_finished=13,comments_pending=387,new_comments=2340,total_comments=2340,comments_enabled=True,
        search_units_completed=8,search_units_total=8)
    tasks=[dict(id='daily',name='每日行业话题采集',task_type='search',enabled=True,
        params=dict(keywords=['行业话题','品牌动态','产品反馈','用户体验'],platforms=['twitter','reddit'],count=50,include_replies=True),
        description='每天 13:00',cron_expression='0 13 * * *',timezone='Asia/Shanghai',next_run_at='2026-10-23 13:00:00',
        last_run_at='2026-10-22T05:00:00Z',last_run_status='running',save_path='/Downloads/行业采集'),
        dict(id='weekly',name='产品图片素材',task_type='search',enabled=True,
        params=dict(keywords=['产品外观','包装设计'],platforms=['twitter','reddit'],count=50,content_mode='images',cron_dialect='unix'),
        description='每周一 09:00',cron_expression='0 9 * * 1',timezone='Asia/Shanghai',next_run_at='2026-10-26 09:00:00',last_run_status='success')]
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1040},device_scale_factor=1)
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def route(r):
            path=urlparse(r.request.url).path
            if path=='/':
                await r.fulfill(content_type='text/html',body=(ROOT/'static/index.html').read_text(encoding='utf-8'));return
            data={'/api/stats':dict(total=0,pending=0,approved=0,rejected=0),
                  '/api/vocabulary':dict(items=[],total=0),'/api/vocabulary/filter-options':{},
                  '/api/collections':dict(runs=[state]),'/api/collections/demo/result':state,
                  '/api/scheduled-tasks':dict(tasks=tasks),'/api/version':dict(version='1.9.0'),
                  '/api/chromium-status':dict(status='installed'),
                  '/api/schedule-preview':dict(description='每周一 09:00',timezone='Asia/Shanghai',next_run_at='2026-10-26 09:00:00'),
                  '/api/scheduled-notifications':dict(latest_id=0,notifications=[])}.get(path,{})
            await r.fulfill(content_type='application/json',body=json.dumps(data))
        await page.route('**/*',route)
        await page.goto('http://127.0.0.1:9876/?desktop=windows')
        await page.screenshot(path=str(OUT/'overview.png'),full_page=True,animations="disabled")
        await page.locator('.quick-link').first.click()
        await page.locator('#builder-next').click()
        assert await page.locator('#search-keyword-error').is_visible()
        await page.locator('#search-keyword').fill('行业话题\n品牌动态\n产品反馈\n用户体验')
        await page.locator('#builder-next').click()
        await page.locator('#search-include-replies').check()
        assert await page.locator('#builder-target').inner_text()=='400'
        await page.evaluate('window.scrollTo(0,0)')
        await page.screenshot(path=str(OUT/'collection.png'),full_page=True,animations="disabled")
        await page.locator('[data-collection-source="profile"]').click()
        assert await page.locator('#user-scrape-card').is_visible()
        assert not await page.locator('#keyword-search-card').is_visible()
        await page.locator('[data-collection-source="keyword"]').click()
        assert '用户体验' in await page.locator('#search-keyword').input_value()
        await page.locator('#collect-mode').select_option('images')
        assert await page.locator('#search-include-replies').is_checked()
        assert await page.locator('#search-include-replies').is_disabled()
        assert 'ZIP' in await page.locator('#builder-output').inner_text()
        await page.locator('#builder-next').click()
        await page.screenshot(path=str(OUT/'collection-review.png'),full_page=True,animations="disabled")
        await page.locator('[data-collection-view="accounts"]').click()
        assert await page.locator('#twitter-login-card').is_visible()
        assert await page.locator('#tw-ct0').is_visible()
        await page.screenshot(path=str(OUT/'accounts.png'),full_page=True,animations="disabled")
        await page.locator('[data-collection-view="history"]').click()
        await page.locator('[data-result="demo"]').wait_for()
        text=await page.locator('#collection-history').inner_text()
        assert '400' in text and '目标' in text and '收集评论' in text
        await page.screenshot(path=str(OUT/'history.png'),full_page=True,animations="disabled")
        await page.locator('[data-result="demo"]').click()
        await page.locator('#collection-history-result .crawl-result').wait_for()
        await page.evaluate("goToPage('schedule')")
        await page.locator('.schedule-card').first.wait_for()
        assert await page.locator('[data-task-run="daily"]').is_disabled()
        await page.screenshot(path=str(OUT/'schedule.png'),full_page=True,animations="disabled")
        await page.locator('[data-task-edit="weekly"]').click()
        await page.wait_for_function("document.getElementById('schedule-name').value==='产品图片素材'")
        assert not await page.locator('#schedule-list-card').is_visible()
        await page.evaluate('window.scrollTo(0,0)')
        await page.screenshot(path=str(OUT/'schedule-editor.png'),full_page=True,animations="disabled")
        await page.locator('#schedule-step-next').click()
        assert await page.locator('#schedule-weekday').input_value()=='mon'
        assert await page.locator('#schedule-time').input_value()=='09:00'
        await page.evaluate('window.scrollTo(0,0)')
        await page.screenshot(path=str(OUT/'schedule-plan.png'),full_page=True,animations="disabled")
        await page.locator('#schedule-step-next').click()
        await page.evaluate('window.scrollTo(0,0)')
        await page.screenshot(path=str(OUT/'schedule-review.png'),full_page=True,animations="disabled")
        await page.locator('#schedule-form-card .text-action').click()
        assert await page.locator('#schedule-list-card').is_visible()
        for name in ['vocabulary','import','export','settings']:
            await page.evaluate('(name)=>goToPage(name)',name)
            await page.screenshot(path=str(OUT/f'{name}.png'),full_page=True,animations="disabled")
        await page.evaluate("switchTheme('dark');goToPage('twitter');setCollectionView('create')")
        await page.screenshot(path=str(OUT/'collection-dark.png'),full_page=True,animations="disabled")
        for width in [1024,768,560]:
            await page.set_viewport_size({'width':width,'height':900})
            for name in ['dashboard','twitter','schedule','settings']:
                await page.evaluate('(name)=>goToPage(name)',name)
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,name,'horizontal overflow')
            await page.evaluate("goToPage('twitter')")
            await page.evaluate('window.scrollTo(0,0)')
            await page.screenshot(path=str(OUT/f'compact-{width}.png'),full_page=True,animations="disabled")
        await page.set_viewport_size({'width':1440,'height':1040})
        await page.locator('[data-collection-view="create"]').focus()
        await page.keyboard.press('ArrowRight')
        assert await page.locator('[data-collection-view="history"]').get_attribute('aria-selected')=='true'
        assert not errors,errors
        await browser.close()
    print('PASS: workspace tabs, source selection, live 400-post plan, image mode, schedule cards/editor, themes, keyboard and three window sizes')

asyncio.run(main())
