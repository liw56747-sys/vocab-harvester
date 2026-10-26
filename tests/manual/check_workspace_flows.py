"""Browser interaction contract: drafts, errors, retry, filtering, details and exports."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'work/ui-refresh-2026-10-22'

async def main():
    OUT.mkdir(exist_ok=True)
    requests=[];errors=[];save_fail=True;export_fail=True
    item=dict(word="O'Reilly <test>",category='示例分类',score=.8,status='pending',action='observe',
              evidence='完整原文证据\n第二行 <script>danger()</script>',reason='需要结合上下文判断的完整原因',platforms=['twitter'],task_name='测试任务')
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1040},accept_downloads=True)
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def route(r):
            nonlocal save_fail,export_fail
            url=urlparse(r.request.url);path=url.path;method=r.request.method
            requests.append((path,method,r.request.post_data_json if r.request.post_data else None,parse_qs(url.query)))
            if path=='/':
                await r.fulfill(content_type='text/html',body=(ROOT/'static/index.html').read_text(encoding='utf-8'));return
            if path=='/api/scheduled-tasks' and method=='POST':
                await asyncio.sleep(.15)
                data={'status':'error','error':'测试保存失败'} if save_fail else {'status':'ok'}
            elif path=='/api/vocabulary/export':
                if export_fail:
                    await r.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':'测试临时故障'}));return
                await r.fulfill(content_type='text/csv',body='word\nexample');return
            else:
                data={
                    '/api/stats':dict(total=1,pending=1,approved=0,rejected=0),
                    '/api/vocabulary':dict(items=[item],total=1),'/api/vocabulary/filter-options':{},
                    '/api/vocabulary/review':{'status':'ok'},
                    '/api/collections':dict(runs=[]),'/api/scheduled-tasks':dict(tasks=[]),
                    '/api/version':dict(version='1.9.0'),'/api/chromium-status':dict(status='installed'),
                    '/api/schedule-preview':dict(description='每天 13:00',timezone='Asia/Shanghai',next_run_at='2026-10-23 13:00'),
                    '/api/scheduled-notifications':dict(latest_id=0,notifications=[]),
                    '/api/multi-search':dict(status='started',task_id='run'),
                    '/api/batch-search':dict(status='started',task_id='run'),
                    '/api/search':dict(status='started',task_id='run'),
                    '/api/task-status':dict(status='success',result=dict(status='success',total_posts=0,requested_posts=100)),
                }.get(path,{})
            await r.fulfill(content_type='application/json',body=json.dumps(data))
        await page.route('**/*',route)
        await page.add_init_script("localStorage.setItem('tw_ct0','fake');localStorage.setItem('tw_auth_token','fake');localStorage.setItem('rd_session','fake');")
        await page.goto('http://127.0.0.1:9876/?desktop=windows')
        await page.evaluate("goToPage('twitter')")
        # Account screen stays open for saved sessions and preserves unfinished edits.
        await page.locator('[data-collection-view="accounts"]').click()
        assert await page.locator('#tw-ct0').is_visible()
        await page.locator('#cookie-panel-twitter .cookie-edit-btn').click()
        await page.locator('#tw-ct0').fill('draft-cookie')
        await page.locator('[data-collection-view="create"]').click()
        await page.locator('[data-collection-view="accounts"]').click()
        assert await page.locator('#tw-ct0').input_value()=='draft-cookie'
        await page.locator('#cookie-panel-twitter .cookie-save-btn').click()
        assert await page.locator('#tw-ct0').is_visible()
        assert await page.locator('#tw-ct0').get_attribute('readonly') is not None
        await page.locator('.cookie-tab').nth(1).click()
        assert await page.locator('#rd-cookies').is_visible()
        await page.evaluate('updateCollectionReview()')
        assert '尚未配置账号' in await page.locator('#collection-review').inner_text()
        await page.locator('#rd-cookies').fill('reddit_session=fixture; edgebucket=edge')
        await page.locator('#cookie-panel-reddit .cookie-save-btn').click()
        assert '尚未配置账号' not in await page.locator('#collection-review').inner_text()
        await page.locator('[data-collection-view="create"]').click()
        # File import prepares a draft and never fires a search.
        await page.locator('#keyword-file-input').set_input_files({'name':'keywords.txt','mimeType':'text/plain','buffer':'alpha\nbeta\ngamma\ndelta'.encode()})
        await page.wait_for_function("document.getElementById('search-keyword').value.includes('delta')")
        assert not any(path in ['/api/search','/api/multi-search','/api/batch-search'] for path,*_ in requests)
        await page.locator('#builder-next').click()
        assert await page.locator('#builder-target').inner_text()=='400'
        await page.locator('#search-count').fill('0')
        await page.locator('#builder-next').click()
        assert await page.locator('#search-count-error').is_visible()
        await page.locator('#search-count').fill('50')
        await page.locator('#builder-next').click()
        await page.locator('#collection-review .text-action').first.click()
        assert 'delta' in await page.locator('#search-keyword').input_value()
        # Exact query syntax and split mode produce different plans and API payloads.
        await page.locator('#keyword-query-mode').select_option('single')
        assert await page.locator('#builder-target').inner_text()=='100'
        await page.locator('#search-keyword').fill('alpha OR beta, gamma')
        await page.locator('#builder-next').click();await page.locator('#builder-next').click()
        await page.locator('#multi-search-btn').click()
        await page.wait_for_function("!document.getElementById('multi-search-btn').disabled")
        single=[r for r in requests if r[0] in ['/api/search','/api/multi-search']]
        assert single and single[-1][2]['keyword']=='alpha OR beta, gamma',single
        await page.evaluate('setCollectionStep(0,false)')
        await page.locator('#keyword-query-mode').select_option('split')
        await page.locator('#search-keyword').fill('alpha\nbeta\ngamma\ndelta')
        await page.locator('#builder-next').click();await page.locator('#builder-next').click()
        await page.locator('#multi-search-btn').click()
        await page.wait_for_function("!document.getElementById('multi-search-btn').disabled")
        batch=[r for r in requests if r[0]=='/api/batch-search']
        assert batch and batch[-1][2]['keywords']==['alpha','beta','gamma','delta'],batch
        assert batch[-1][2]['count_scope']=='per_keyword'
        # Entire schedule creation, failed save, retained draft and retry.
        await page.evaluate("goToPage('schedule');showScheduleForm()")
        await page.locator('#schedule-step-next').click()
        assert await page.locator('#schedule-name-error').is_visible()
        await page.locator('#schedule-name').fill('四关键词测试')
        await page.locator('#schedule-keywords').fill('alpha\nbeta\ngamma\ndelta')
        await page.locator('#modal-search-count').fill('50')
        await page.locator('#schedule-step-next').click()
        await page.locator('#schedule-weekday').select_option('*')
        await page.locator('#schedule-time').fill('13:00')
        await page.locator('#schedule-step-next').click()
        await page.locator('#schedule-save-path').fill('/tmp/collection-output')
        assert '/tmp/collection-output' in await page.locator('#schedule-review').inner_text()
        assert '400' in await page.locator('#schedule-review').inner_text()
        await page.locator('#schedule-save-button').click()
        await page.evaluate('submitScheduleWizard()')
        await page.locator('#schedule-save-feedback').wait_for()
        assert len([r for r in requests if r[0]=='/api/scheduled-tasks' and r[1]=='POST'])==1
        assert await page.locator('#schedule-name').input_value()=='四关键词测试'
        await page.evaluate('window.scrollTo(0,0)')
        await page.screenshot(path=str(OUT/'schedule-save-error.png'),full_page=True,animations='disabled')
        save_fail=False
        await page.locator('#schedule-save-button').click()
        await page.locator('#schedule-list-card').wait_for()
        saved=[r[2] for r in requests if r[0]=='/api/scheduled-tasks' and r[1]=='POST'][-1]
        assert saved['params']['count']==50 and saved['params']['count_scope']=='per_keyword'
        assert saved['params']['platforms']==['twitter','reddit'] and saved['cron_expression']=='0 13 * * *'
        assert saved['save_path']=='/tmp/collection-output'
        await page.evaluate("openScheduleHistory('daily / test','测试任务')")
        await page.wait_for_function("document.getElementById('collection-history').textContent.includes('还没有')")
        assert any(r[0]=='/api/collections' and r[3].get('owner')==['daily / test'] for r in requests)
        await page.locator('#history-owner-filter button').click()
        await page.wait_for_function("document.getElementById('history-owner-filter').hidden")
        # Full evidence remains readable without losing filters or scroll context.
        await page.evaluate("goToPage('vocabulary')")
        await page.locator('[data-word-detail="0"]').click()
        assert await page.locator('#vocabulary-detail').is_visible()
        assert await page.locator('#vocabulary-detail-title').inner_text()==item['word']
        assert item['evidence'] in await page.locator('#vocabulary-detail-body').inner_text()
        assert await page.locator('#vocabulary-detail-body script').count()==0
        await page.screenshot(path=str(OUT/'vocabulary-detail.png'),full_page=True,animations='disabled')
        await page.keyboard.press('Escape')
        assert not await page.locator('#vocabulary-detail').is_visible()
        await page.locator('[data-word-action="approve"]').click()
        await page.wait_for_timeout(100)
        reviewed=[r[2] for r in requests if r[0]=='/api/vocabulary/review'][-1]
        assert reviewed['word']==item['word']
        # Selecting export format has no side effect. Errors stay visible and retry works.
        await page.evaluate("goToPage('export')")
        await page.locator('[data-export-format="csv"]').click()
        await page.locator('#export-status').select_option('approved')
        assert not any(r[0]=='/api/vocabulary/export' for r in requests)
        await page.locator('#export-submit').click()
        await page.wait_for_function("document.getElementById('export-result').textContent.includes('导出失败')")
        assert await page.locator('#export-status').input_value()=='approved'
        export_fail=False
        async with page.expect_download() as info:
            await page.locator('#export-submit').click()
        assert (await info.value).suggested_filename=='vocabulary_export.csv'
        assert '已发起下载' in await page.locator('#export-result').inner_text()
        assert not errors,errors
        await browser.close()
    print('PASS: imported drafts, step validation, exact/split queries, schedule save failure/retry/duplicate guard, owner filter, escaped word details, review payload and export retry')

asyncio.run(main())
