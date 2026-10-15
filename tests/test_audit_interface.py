"""外部审核契约：无默认实现、无隐式候选入库、旧入口不可复活。"""
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from src.api.main import app
from src.common.database import get_db
from src.common.models import ParsedPost
from src.orchestrator.pipeline import Pipeline
from tests.audit_fake import FakeAuditAdapter


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        yield c


def payload():
    return {'posts':[{'platform':'twitter','post_id':'p1','content':'样本文本','author':'a',
                     'published_at':'2026-10-15T08:00:00',
                     'raw_data':{'type':'comment','parent_id':'parent', 'media_urls':['https://example.test/a.jpg']}}],
            'context':{'policy_version':'next'}}


async def test_unconfigured_interface(client, monkeypatch):
    monkeypatch.setattr(app.state, 'audit_adapter', None, raising=False)
    status = (await client.get('/api/audit/status')).json()
    assert status['configured'] is False and status['automatic_audit'] is False
    for response in [await client.post('/api/audit/submit', json=payload()),
                     await client.get('/api/audit/tasks/anything')]:
        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'audit_not_configured'


@pytest.mark.parametrize('method,path', [('get','/api/model-config'),('post','/api/model-config'),
                                         ('post','/api/analyze-last'),('post','/api/import')])
async def test_retired_endpoints(client, method, path):
    response = await getattr(client, method)(path)
    assert response.status_code == 410
    assert response.json()['detail']['code'] == 'legacy_audit_removed'


async def test_injected_interface_preserves_context_without_ingestion(client, monkeypatch, init_test_db):
    adapter = FakeAuditAdapter()
    monkeypatch.setattr(app.state, 'audit_adapter', adapter, raising=False)
    response = await client.post('/api/audit/submit', json=payload())
    assert response.status_code == 202
    assert adapter.posts[0].raw_data == payload()['posts'][0]['raw_data']
    assert adapter.context == {'policy_version':'next'}
    task = response.json()['task_id']
    response = await client.get(f'/api/audit/tasks/{task}')
    assert response.json()['status'] == 'completed'
    assert response.json()['result']['keywords'][0]['candidate'] == '测试候选'
    adapter.poll_result = AsyncMock(return_value=None)
    assert (await client.get(f'/api/audit/tasks/{task}')).json()['status'] == 'pending'
    db = await get_db()
    assert (await (await db.execute('SELECT COUNT(*) FROM vocabulary')).fetchone())[0] == 0


async def test_empty_submission_rejected(client):
    assert (await client.post('/api/audit/submit', json={'posts':[]})).status_code == 422


async def test_pipeline_has_no_default_adapter(init_test_db, crawl_query):
    pipeline = Pipeline.from_config()
    assert pipeline.adapter is None
    stats = await pipeline.run(crawl_query)
    assert stats['status'] == 'success' and stats['total_posts'] > 0
    assert stats['total_keywords'] == 0 and 'ingested_count' not in stats
    stats = await pipeline.process_posts([ParsedPost('twitter','p','text','a',datetime.now())])
    assert stats['status'] == 'failed' and '尚未接入' in stats['error']


async def test_adapter_wait_timeout():
    adapter = FakeAuditAdapter()
    adapter.poll_result = AsyncMock(return_value=None)
    with pytest.raises(TimeoutError):
        await adapter.submit_and_wait([], timeout=0.02, poll_interval=0.005)
