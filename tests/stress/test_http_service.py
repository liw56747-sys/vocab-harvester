"""Actual HTTP server lifecycle + concurrent requests, isolated from real user data/accounts."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import httpx


async def test_live_http_load_and_disconnected_collection(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    root=Path(__file__).resolve().parents[2]
    code='''import sys,uvicorn
from src.api import main
main.check_for_update_async=lambda:None
uvicorn.run(main.app,host="127.0.0.1",port=int(sys.argv[1]),log_level="warning")
'''
    with (tmp_path/'server.log').open('w') as log:
        process=subprocess.Popen([sys.executable,'-c',code,str(port)],cwd=root,
            env={**os.environ,'VOCAB_DATA_DIR':str(tmp_path/'isolated')},stdout=log,stderr=log)
        try:
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=15) as client:
                for _ in range(200):
                    assert process.poll() is None,(tmp_path/'server.log').read_text()
                    try:
                        if (await client.get('/api/audit/status')).status_code==200:break
                    except httpx.TransportError:pass
                    await asyncio.sleep(.05)
                else:raise AssertionError('server failed to become ready')
                semaphore=asyncio.Semaphore(20);durations=[]
                async def request(i):
                    async with semaphore:
                        start=time.monotonic()
                        path=['/api/stats','/api/audit/status','/api/collections','/api/scheduled-tasks'][i%4]
                        reply=await client.get(path)
                        assert reply.status_code==200,(path,reply.text)
                        durations.append(time.monotonic()-start)
                await asyncio.gather(*[request(i) for i in range(300)])
                assert (await client.post('/api/import')).status_code==410
                started=await client.post('/api/search',json={'keyword':'offline','count':100,'platforms':['twitter','reddit']})
                assert started.status_code==200
                rid=started.json()['task_id']
                for _ in range(200):
                    state=(await client.get('/api/task-status',params={'task_id':rid})).json()
                    if state['status']!='running':break
                    await asyncio.sleep(.05)
                assert state['status']=='success'
                assert state['result']['status']=='partial' and state['result']['total_posts']==0
                assert 'Cookie' in str(state['result']['errors'])
                assert (await client.get(f'/api/collections/{rid}/result')).status_code==200
                assert (await client.get('/api/stats')).json()['total']==0
                print(f'HTTP_LOAD requests=300 concurrency=20 p95_ms={sorted(durations)[284]*1000:.1f}')
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=5)
