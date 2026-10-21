"""Image-only contract and failure recovery, isolated from live accounts/CDNs."""
import asyncio
from collections import Counter
import io
import json
import zipfile
from unittest.mock import AsyncMock

import httpx
from PIL import Image
import pytest

from src.collection import engine, images as media
from src.collection.engine import CollectionSpec, Traffic, collect
from src.collection.images import Images, allowed_url, canonical, reddit_images
from src.collection.store import Store
from src.crawlers.reddit_crawler import RedditCookieFetcher
from src.crawlers.twitter_url import TwitterCookieFetcher


def png(color='red'):
    out=io.BytesIO();Image.new('RGB',(4,3),color).save(out,format='PNG');return out.getvalue()


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR',str(tmp_path))
    Traffic.active.clear();Traffic.until.clear();Traffic.delay.clear()
    yield
    Traffic.active.clear();Traffic.until.clear();Traffic.delay.clear()


def make(rows=None):
    store=Store();rid=store.create(CollectionSpec(keywords=['x'],content_mode='images').model_dump(mode='json'))
    images=Images(store)
    if rows: images.discover(rid,rows)
    return store,rid,images


def post(url,**extra):
    return dict(platform='twitter',keyword='x',tweet_id='1',url='https://x.com/i/status/1',image_urls=[url],**extra)


def test_reddit_native_comment_gallery_and_no_video_thumbnail():
    metadata={'id':{'e':'Image','s':{'u':'https://preview.redd.it/a.png?x=1&amp;y=2'}},
              'gif':{'e':'AnimatedImage','s':{'gif':'https://i.redd.it/b.gif','mp4':'bad.mp4'}}}
    assert reddit_images({'gallery_data':{'items':[{'media_id':'id'},{'media_id':'gif'}]},'media_metadata':metadata}) == ['https://preview.redd.it/a.png?x=1&y=2','https://i.redd.it/b.gif']
    comment={'id':'c','body':'![img](id) ![native](https://i.redd.it/c.png)','media_metadata':metadata,'parent_id':'t1_other'}
    rows=[]
    RedditCookieFetcher()._parse_comment_tree([{'kind':'t1','data':comment}],'root',rows,100)
    assert rows[0]['root_post_id']=='root' and rows[0]['parent_id']=='other'
    assert len(rows[0]['image_urls'])==3
    assert reddit_images({'post_hint':'image','url_overridden_by_dest':'https://imgur.com/a.png',
                          'preview':{'images':[{'source':{'url':'https://preview.redd.it/native.png'}}]}})==['https://preview.redd.it/native.png']
    assert reddit_images({'is_video':True,'preview':{'images':[{'source':{'url':'https://i.redd.it/thumbnail.png'}}]}})==[]


@pytest.mark.parametrize('url',['http://i.redd.it/a.png','https://127.0.0.1/a','https://i.redd.it.evil/a',
                                 'https://user:pass@i.redd.it/a','https://i.redd.it:4433/a','file:///tmp/a',
                                 'https://pbs.twimg.com/profile_images/a.png'])
def test_url_boundary(url):
    assert not allowed_url(url)


async def test_original_fallback_dedup_and_portable_export(tmp_path):
    store,rid,images=make([post('https://pbs.twimg.com/media/photo?format=png&name=small',
        replies_data=[{'tweet_id':'2','image_urls':['https://i.redd.it/same.png']}])])
    hits=[]
    def respond(req):
        hits.append(str(req.url))
        if req.url.params.get('name')=='orig':return httpx.Response(403)
        return httpx.Response(200,content=png())
    await images.download(rid,transport=httpx.MockTransport(respond))
    assert images.summary(rid)['images_downloaded']==2
    assert images.summary(rid)['image_files']==1
    assert images.manifest(rid)[1]['post_id']=='1' and images.manifest(rid)[1]['comment_id']=='2'
    await Images(Store(store.path)).download(rid,transport=httpx.MockTransport(lambda req:pytest.fail('redownload')))
    archive=images.export(rid,tmp_path/'exports')
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        manifest=json.loads(z.read('manifest.json'))['images']
        assert len([n for n in z.namelist() if n.startswith('images/')])==1
        assert all('content' not in row and 'title' not in row for row in manifest)
        assert all(z.read(r['file'])==png() for r in manifest)
    assert images.export(rid,tmp_path/'exports')!=archive
    row=images.assets(rid)[0];(images.root/row['file']).write_bytes(b'broken')
    await images.download(rid,transport=httpx.MockTransport(lambda req:httpx.Response(200,content=png())))
    assert all(images.valid_file(r) for r in images.assets(rid))


async def test_faults_are_durable_and_retry_only_failed(monkeypatch):
    urls=['https://i.redd.it/'+x+'.png' for x in ['ok','rate','bad','big','redirect','down']]
    store,rid,images=make([post(u,tweet_id_unused=i) for i,u in enumerate(urls)])
    calls=Counter()
    def response(req):
        name=req.url.path;calls[name]+=1
        if 'rate' in name:return httpx.Response(429)
        if 'bad' in name:return httpx.Response(200,content=b'<html>login</html>')
        if 'big' in name:return httpx.Response(200,headers={'content-length':str(media.MAX_BYTES+1)})
        if 'redirect' in name:return httpx.Response(302,headers={'location':'http://127.0.0.1/private'})
        if 'down' in name:return httpx.Response(503)
        return httpx.Response(200,content=png())
    await images.download(rid,transport=httpx.MockTransport(response))
    assert images.summary(rid)['images_failed']==5
    assert calls['/rate.png']==1 and calls['/down.png']==2
    before=calls['/ok.png']
    def recover(req):calls[req.url.path]+=1;return httpx.Response(200,content=png())
    await images.download(rid,transport=httpx.MockTransport(recover))
    assert images.summary(rid)['images_failed']==0 and calls['/ok.png']==before
    assert not list(images.root.glob('*.part'))


async def test_cancel_midstream_cleans_temp_and_restores_queue():
    store,rid,images=make([post('https://i.redd.it/a.png')])
    entered=asyncio.Event()
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield png(); entered.set(); await asyncio.Event().wait()
    task=asyncio.create_task(images.download(rid,transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Slow()))))
    await asyncio.wait_for(entered.wait(),3);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not list(images.root.glob('*.part'))
    assert images.summary(rid)['images_pending']==1
    await images.download(rid,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=png())))
    assert images.summary(rid)['images_downloaded']==1


async def test_image_engine_inspects_text_only_parent_and_resumes_images_without_search(monkeypatch):
    spec=CollectionSpec(keywords=['x'],content_mode='images',count=1,include_replies=True,block_resources=True)
    assert not spec.block_resources
    store=Store();rid=store.create(spec.model_dump(mode='json'))
    search=AsyncMock(return_value=([{'tweet_id':'1','content':'PRIVATE_TEXT','image_urls':[]}],''))
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    async def comments(fetcher,platform,posts,cookies):
        posts[0].update(comments_status='finished',replies_data=[{'tweet_id':'2','content':'PRIVATE_REPLY','image_urls':['https://i.redd.it/a.png']}])
        return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    original=Images.download
    async def download(self,*args,**kwargs):
        return await original(self,*args,**kwargs,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=png())))
    monkeypatch.setattr(Images,'download',download)
    rows,info=await collect(store,rid,{'twitter':{'ct0':'x','auth_token':'y'}})
    assert info['status']=='success' and Images(store).summary(rid)['images_downloaded']==1
    assert 'PRIVATE' not in json.dumps(store.rows(rid))
    await collect(store,rid,{},images_only=True)
    assert search.await_count==1
    assert Images(store).manifest(rid)[0]['comment_id']=='2'


async def test_limits_and_timeout_are_explicit(monkeypatch):
    store,rid,images=make([post('https://i.redd.it/a.png'),post('https://i.redd.it/b.png')])
    monkeypatch.setattr(media,'MAX_URLS',1)
    monkeypatch.setattr(media,'ATTEMPT_SECONDS',.02)
    async def slow(req):await asyncio.sleep(1);return httpx.Response(200,content=png())
    await images.download(rid,transport=httpx.MockTransport(slow))
    assert {r['status'] for r in images.assets(rid)}=={'failed','limit'}
    assert not list(images.root.glob('*.part'))


async def test_image_api_and_scheduled_export(init_test_db, tmp_path, monkeypatch):
    from src.api import main as api
    from tests.test_collection_api import done
    original=Images.download
    async def download(self,*args,**kwargs):
        return await original(self,*args,**kwargs,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=png())))
    monkeypatch.setattr(Images,'download',download)
    async def search(self,keyword,**kwargs):
        return [{'tweet_id':keyword,'content':'PRIVATE','image_urls':['https://i.redd.it/'+keyword+'.png']}],''
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    async def comments(fetcher,platform,posts,cookies):
        posts[0].update(comments_status='finished',replies_data=[]);return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://local') as client:
        response=await client.post('/api/batch-search',json={'keywords':['a','b'], 'platforms':['twitter'], 'count':1,'content_mode':'images',
            'cookies':[{'platform':'twitter','ct0':'c','auth_token':'a'}]})
        assert response.status_code==200,response.text
        rid=response.json()['task_id'];state=await done(client,rid)
        assert state['result']['images_downloaded']==2 and state['result']['total_posts']==2
        assert state['result']['status']=='success'
        assert 'csv_data' not in state['result'] and 'PRIVATE' not in json.dumps(Store().rows(rid))
        exported=await client.post(f'/api/collections/{rid}/images/export',json={'folder':str(tmp_path/'out')})
        assert exported.status_code==200,exported.text
        assert zipfile.is_zipfile(exported.json()['path'])
        await client.post(f'/api/collections/{rid}/resume?images_only=true')
        assert (await done(client,rid))['result']['images_downloaded']==2
    from src.common.database import get_db
    db=await get_db()
    await db.execute("INSERT INTO scheduled_tasks(id,name,created_at,updated_at) VALUES('scheduled','图片任务','now','now')")
    await db.commit()
    await api._execute_scheduled_task({'id':'scheduled','name':'图片任务','task_type':'search','save_path':str(tmp_path/'scheduled'),
        'params':{'keywords':['scheduled'],'platforms':['twitter'],'count':1,'content_mode':'images'}})
    record=Store().list(owner='scheduled')[0]
    assert record['status']=='success'
    files=list((tmp_path/'scheduled').glob('图片任务*.zip'));assert len(files)==1
    with zipfile.ZipFile(files[0]) as archive:
        assert len(json.loads(archive.read('manifest.json'))['images'])==1
    # Scheduled recovery downloads public CDN images without a fresh search/login.
    await db.execute('DELETE FROM platform_cookies');await db.commit()
    row=Images(Store()).assets(record['id'])[0]
    (Images(Store()).root/row['file']).unlink()
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',AsyncMock(side_effect=AssertionError('must not search')))
    await api._execute_scheduled_task({'id':'scheduled','name':'图片任务','task_type':'search','save_path':str(tmp_path/'scheduled'),
        'params':{},'collection_id':record['id'],'images_only':True})
    assert Images(Store()).valid_file(Images(Store()).assets(record['id'])[0])
    assert Store().get(record['id'])['status']=='success'



async def test_pixel_byte_budget_and_failed_urls_not_retried_per_keyword(monkeypatch):
    store,rid,images=make([post('https://i.redd.it/a.png')])
    monkeypatch.setattr(media,'MAX_PIXELS',2)
    await images.download(rid,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=png())))
    assert '像素' in images.assets(rid)[0]['error']
    monkeypatch.setattr(media,'MAX_PIXELS',40_000_000)
    monkeypatch.setattr(media,'MAX_RUN_BYTES',1)
    await images.download(rid,transport=httpx.MockTransport(lambda r:pytest.fail('budget exceeded')))
    assert images.assets(rid)[0]['status']=='limit'
    monkeypatch.setattr(media,'MAX_RUN_BYTES',2*1024**3)
    attempted=set();calls=[]
    def fail(req):calls.append(1);return httpx.Response(503)
    await images.download(rid,transport=httpx.MockTransport(fail),attempted=attempted)
    await images.download(rid,transport=httpx.MockTransport(fail),attempted=attempted)
    assert len(calls)==2  # two attempts total, not two attempts for each keyword group


async def test_oversized_chunk_unknown_length_and_export_missing_cache(monkeypatch,tmp_path):
    store,rid,images=make([post('https://i.redd.it/a.png')])
    monkeypatch.setattr(media,'MAX_BYTES',10)
    class Large(httpx.AsyncByteStream):
        async def __aiter__(self):yield b'x'*20
    await images.download(rid,transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Large())))
    assert images.summary(rid)['images_failed']==1 and not list(images.root.glob('*.part'))
    monkeypatch.setattr(media,'MAX_BYTES',20*1024*1024)
    await images.download(rid,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=png())))
    (images.root/images.assets(rid)[0]['file']).unlink()
    archive=images.export(rid,tmp_path/'export')
    with zipfile.ZipFile(archive) as z:
        assert json.loads(z.read('manifest.json'))['images'][0]['status']=='failed'
        assert not any(n.startswith('images/') for n in z.namelist())
    assert images.summary(rid)['images_failed']==1


async def test_switching_schedule_to_images_does_not_exclude_text_history(monkeypatch):
    store=Store()
    old=store.create(CollectionSpec(keywords=['x'],count=1).model_dump(mode='json'),owner='schedule')
    store.save(old,'old',[{'platform':'twitter','tweet_id':'1','keyword':'x','content':'old text'}],{})
    store.status(old,'success')
    rid=store.create(CollectionSpec(keywords=['x'],count=1,content_mode='images').model_dump(mode='json'),owner='schedule')
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',AsyncMock(return_value=([{'tweet_id':'1','image_urls':[]}],'')))
    async def comments(fetcher,platform,posts,cookies):posts[0]['comments_status']='finished';return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    rows,info=await collect(store,rid,{'twitter':{'ct0':'c','auth_token':'a'}})
    assert info['status']=='success' and len(rows)==1


@pytest.mark.parametrize('format,extension',[('PNG','png'),('JPEG','jpg'),('WEBP','webp'),('GIF','gif')])
async def test_supported_formats_preserve_original_bytes(format,extension):
    out=io.BytesIO();im=Image.new('RGB',(4,3),'red')
    if format=='GIF':im.save(out,format=format,save_all=True,append_images=[Image.new('RGB',(4,3),'blue')],duration=100,loop=0)
    else:im.save(out,format=format)
    payload=out.getvalue()
    store,rid,images=make([post('https://i.redd.it/a.'+extension)])
    await images.download(rid,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=payload)))
    row=images.assets(rid)[0]
    assert row['status']=='downloaded' and row['file'].endswith('.'+extension)
    assert (images.root/row['file']).read_bytes()==payload


async def test_truncated_jpeg_is_not_accepted_as_downloaded():
    buf=io.BytesIO();Image.new('RGB',(300,300),'red').save(buf,format='JPEG')
    store,rid,images=make([post('https://i.redd.it/broken.jpg')])
    await images.download(rid,transport=httpx.MockTransport(lambda r:httpx.Response(200,content=buf.getvalue()[:-100])))
    assert images.summary(rid)['images_failed']==1
    assert not any(p.suffix=='.jpg' for p in images.root.iterdir())
