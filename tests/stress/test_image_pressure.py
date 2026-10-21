"""Real local HTTP downloads and durable scale/concurrency tests; no platform load."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import zipfile
from urllib.parse import urlsplit

import httpx
import pytest
from src.collection import images as media
from src.collection.images import Images
from src.collection.store import Store
from src.collection.engine import CollectionSpec
from tests.test_image_collection import png


async def test_live_download_global_concurrency_and_disconnection(tmp_path,monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR',str(tmp_path))
    active=0;peak=0;lock=threading.Lock();hits=Counter()
    payload=png()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active);hits[self.path]+=1
            try:
                time.sleep(.005)
                self.send_response(200)
                self.send_header('Content-Length',str(len(payload)+(100 if 'broken' in self.path else 0)))
                self.end_headers();self.wfile.write(payload)
            finally:
                with lock:active-=1
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    host=f'http://127.0.0.1:{server.server_port}'
    validate=media.allowed_url
    monkeypatch.setattr(media,'allowed_url',lambda u: u.startswith(host+'/') or validate(u))
    store=Store();images=Images(store);runs=[]
    for batch in range(3):
        rid=store.create(CollectionSpec(keywords=['local'],content_mode='images').model_dump(mode='json'));runs.append(rid)
        images.discover(rid,[{'platform':'reddit','post_id':f'{batch}-{i}','keyword':'local',
                             'image_urls':[f'{host}/{batch}/{i}.png']} for i in range(20)])
    # Seed as pending: localhost is test-only, never accepted by production URL policy.
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            await asyncio.gather(*[asyncio.wrap_future(pool.submit(asyncio.run,images.download(rid))) for rid in runs])
        assert peak<=2 and peak>0
        assert all(images.summary(rid)['images_downloaded']==20 for rid in runs)
        images.discover(runs[0],[{'platform':'reddit','post_id':'broken','keyword':'local','image_urls':[host+'/broken.png']}])
        await images.download(runs[0])
        assert images.summary(runs[0])['images_failed']==1 and hits['/broken.png']==2
        assert not list(images.root.glob('*.part'))
        with store.connect() as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        print(f'IMAGE_HTTP downloads=60 parallel_runs=3 peak_connections={peak}; truncated response rejected')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


async def test_600_parents_1200_images_snapshot_readers_and_recovery(tmp_path,monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR',str(tmp_path))
    store=Store();rid=store.create(CollectionSpec(keywords=list('abcdef'),platforms=['twitter','reddit'],count=50,content_mode='images').model_dump(mode='json'))
    images=Images(store)
    rows=[]
    for i in range(600):
        rows.append({'platform':'twitter' if i<300 else 'reddit','keyword':'abcdef'[i%6], 'tweet_id':str(i),'post_id':str(i),
                     'image_urls':[f'https://i.redd.it/{i}.png'],
                     **({'replies_data':[{'tweet_id':str(1000+i),'image_urls':[f'https://i.redd.it/{1000+i}.png']}]} if i<300 else {})})
        if i>=300:rows.append({'platform':'reddit','keyword':'abcdef'[i%6],'type':'comment','post_id':str(i),'root_post_id':str(i),
                              'comment_id':str(1000+i),'image_urls':[f'https://i.redd.it/{1000+i}.png']})
    images.discover(rid,rows);images.discover(rid,rows)
    assert images.summary(rid)['images_found']==1200 and images.summary(rid)['image_sources']==1200
    hits=Counter();data=png()
    async def response(req):
        hits[req.url.path]+=1
        await asyncio.sleep(0)
        return httpx.Response(200,content=data)
    async def readers():
        for _ in range(15):
            summaries=await asyncio.gather(*[asyncio.to_thread(images.summary,rid) for _ in range(20)])
            assert all(len(json.dumps(s))<2048 for s in summaries)
    await asyncio.gather(images.download(rid,transport=httpx.MockTransport(response)),readers())
    assert images.summary(rid)['images_downloaded']==1200 and images.summary(rid)['image_files']==1
    await images.download(rid,transport=httpx.MockTransport(lambda r:pytest.fail('completed URLs redownloaded')))
    assert all(n==1 for n in hits.values())
    paths=await asyncio.gather(*[asyncio.to_thread(images.export,rid,tmp_path/'out') for _ in range(2)])
    assert paths[0]!=paths[1]
    for path in paths:
        with zipfile.ZipFile(path) as z:
            manifest=json.loads(z.read('manifest.json'))['images']
            assert len(manifest)==1200 and len([r for r in manifest if r['comment_id']])==600
            assert len([n for n in z.namelist() if n.startswith('images/')])==1
            assert z.testzip() is None
    with store.connect() as db:assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    print('IMAGE_SCALE parents=600 refs=1200 readers=20 reads=300 archives=2 unique_files=1')
