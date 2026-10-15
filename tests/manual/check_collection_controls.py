"""Real browser validates settings payloads, calendar preview and recovery actions."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[2]

async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        page=await browser.new_page(viewport={'width':1300,'height':1000})
        errors=[];requests=[];paths=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        task={'id':'old','name':'旧规则任务','task_type':'search','enabled':True,'cron_expression':'0 15 * * 3',
              'params':{'keywords':['a'],'platforms':['twitter'],'count':100,'analyze':True,'opinion_detail':'legacy'},'workflows':['yq'],'timezone':'Asia/Shanghai',
              'description':'周四 15:00','legacy_weekdays':True,'next_run_at':'2026-10-22T15:00:00+08:00'}
        async def route(r):
            path=urlparse(r.request.url).path
            paths.append(path)
            if path=='/':
                await r.fulfill(content_type='text/html',body=(ROOT/'static/index.html').read_text(encoding='utf-8'));return
            data={}
            if r.request.method=='POST' or r.request.method=='PUT':
                requests.append((r.request.url,r.request.post_data_json if r.request.post_data else None))
                data={'status':'started','task_id':'run'}
            if path=='/api/schedule-preview':
                data={'description':'周三 15:00','timezone':'Asia/Shanghai','next_run_at':'2026-10-21T15:00:00+08:00'}
            elif path=='/api/scheduled-tasks':data={'tasks':[task]}
            elif path=='/api/scheduled-tasks/old':data={'status':'ok'}
            elif path=='/api/scheduled-notifications':data={'notifications':[],'latest_id':0}
            elif path=='/api/vocabulary':data={'items':[],'total':0}
            elif path=='/api/chromium-status':data={'status':'installed'}
            elif path=='/api/audit/status':data={'configured':False,'status':'not_configured'}
            elif path=='/api/vocabulary/filter-options':data={'categories':['历史分类'],'candidate_types':[],'task_names':[]}
            elif path=='/api/version':data={'version':'test'}
            elif path=='/api/collections':data={'runs':[{'id':'run','status':'interrupted','created':'2026-10-15','saved_posts':12,'active':False}]}
            elif path=='/api/collections/run/result' or path=='/api/task-status':
                result={'status':'partial','run_id':'run','total_posts':12,'total_rows':19,'total_comments':7,
                    'requested_posts':100,'missing_posts':88,'comments_pending':3,'comments_finished':9,
                    'errors':['平台限流，可稍后续抓'],'sampled_posts':[{'content':'PRIVATE_DATA'}]}
                data={'status':'success','result':result} if path=='/api/task-status' else result
            await r.fulfill(content_type='application/json',body=json.dumps(data))
        await page.route('**/*',route)
        await page.add_init_script("localStorage.setItem('tw_ct0','fake');localStorage.setItem('tw_auth_token','fake');window.confirm=()=>true;")
        await page.goto('http://127.0.0.1:9876/')
        await page.evaluate("showPage('import')")
        await page.wait_for_function("document.getElementById('audit-status').textContent==='尚未接入'")
        assert await page.locator('#model-config-card, #import-file, #auto-analyze-btn, #schedule-analyze-section, #schedule-exec-mode').count()==0
        await page.evaluate("showPage('vocabulary')")
        await page.wait_for_function("document.querySelector('#filter-category').options.length===2")
        assert await page.locator('#filter-workflow').count()==0
        await page.evaluate("showPage('twitter');document.getElementById('search-keyword').value='a';setCollectionOptions('collect',{count_scope:'total',since:'2026-10-01',until:'2026-10-15',window_days:2,languages:['zh'],alternate_sort:true,keyword_groups:[['b','c']]});startMultiSearch()")
        await page.locator('#multi-search-result .crawl-result').wait_for()
        body=next(body for url,body in requests if url.endswith('/api/search'))
        assert body['count_scope']=='total' and body['window_days']==2
        assert body['keyword_groups']==[['b','c']] and body['languages']==['zh']
        text=await page.locator('#multi-search-result').inner_text()
        assert 'PRIVATE_DATA' not in text and '待补抓：3' in text and '88' in text
        await page.evaluate("loadCollectionHistory()")
        await page.locator('[data-comments="run"]').wait_for(state='attached')
        await page.evaluate("document.querySelector('[data-comments=run]').click()")
        await page.wait_for_function("document.querySelector('#collection-history').textContent.includes('已保存 12')")
        assert any('/api/collections/run/resume?comments_only=true' in url for url,body in requests)
        await page.evaluate("showPage('schedule');editScheduleTask('old')")
        await page.wait_for_function("document.getElementById('schedule-name').value==='旧规则任务'")
        assert await page.locator('#schedule-dialect').input_value()=='apscheduler'
        assert await page.locator('#schedule-collect-scope').input_value()=='per_keyword'
        await page.locator('#schedule-weekday').select_option('wed')
        await page.locator('#schedule-time').fill('15:00');await page.locator('#schedule-time').dispatch_event('change')
        await page.wait_for_function("document.getElementById('schedule-cron').value==='0 15 * * wed'")
        assert await page.locator('#schedule-dialect').input_value()=='unix'
        await page.evaluate("document.getElementById('schedule-missed').value='once';setCollectionOptions('schedule-collect',{count_scope:'per_platform',incremental:true,lookback_days:5});saveScheduleTask()")
        await page.wait_for_timeout(100)
        saved=next(body for url,body in requests if url.endswith('/api/scheduled-tasks/old'))
        assert saved['params']['missed_policy']=='once' and saved['params']['timezone']=='Asia/Shanghai'
        assert saved['params']['incremental'] and saved['params']['lookback_days']==5
        assert saved['cron_expression']=='0 15 * * wed'
        assert saved['params']['analyze'] is False
        assert 'opinion_detail' not in saved['params'] and 'workflows' not in saved
        await page.evaluate("showPage('twitter');checkCollectionAccess()")
        await page.wait_for_function("!document.getElementById('collection-preflight').disabled")
        assert any(url.endswith('/api/collection-preflight') for url,body in requests)
        assert not any(path in paths for path in ['/api/model-config','/api/analyze-last','/api/import'])
        assert not errors,errors
        await browser.close()
        print('PASS: scope/ranges/languages/combinations payload, summary-only recovery, comment retry, legacy calendar preservation, weekday preset, incremental/misfire settings and preflight button')

asyncio.run(main())
