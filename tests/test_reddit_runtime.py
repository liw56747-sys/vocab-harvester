import json
from unittest.mock import AsyncMock
import pytest
from src.common import network_runtime, reddit_diagnostics
from src.crawlers.reddit_crawler import RedditCookieFetcher


def test_release_rejects_legacy_tls_and_reports_tested_runtime(monkeypatch):
    monkeypatch.setattr(network_runtime.ssl, 'OPENSSL_VERSION_INFO', (3, 0, 16, 0, 0))
    with pytest.raises(RuntimeError, match='OpenSSL >= 3.5.0'):
        network_runtime.require_release_tls()
    monkeypatch.setattr(network_runtime.ssl, 'OPENSSL_VERSION_INFO', (3, 5, 8, 0, 0))
    assert 'openssl' in network_runtime.require_release_tls()


async def test_probe_uses_saved_session_without_exposing_credentials_or_posts(monkeypatch):
    search = AsyncMock(return_value=([{'post_id':'1','content':'private post'}], ''))
    monkeypatch.setattr(RedditCookieFetcher, 'search_posts', search)
    config = {'reddit_session':'secret', 'extra_cookies':{'edgebucket':'private-edge'},
              'proxy':'http://user:password@127.0.0.1:7897'}
    report = await reddit_diagnostics.probe(config, 'topic')
    assert report['status']=='passed' and report['saved_posts']==1
    assert search.call_args.kwargs['cookies']=={'reddit_session':'secret','edgebucket':'private-edge'}
    text=json.dumps(report)
    assert all(v not in text for v in ('secret','private-edge','password','private post'))
    search.side_effect=RuntimeError('403 http://user:password@host private-cookie')
    report=await reddit_diagnostics.probe(config,'topic')
    assert report['reason']=='access_denied'
    assert 'password' not in json.dumps(report) and 'private-cookie' not in json.dumps(report)


async def test_probe_missing_session_does_not_request_network(monkeypatch):
    search=AsyncMock()
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',search)
    assert (await reddit_diagnostics.probe({},'topic'))['reason']=='missing_session'
    search.assert_not_called()
