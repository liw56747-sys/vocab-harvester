"""更新检查：公开清单、API 限流回退、并发去重和失败冷却。"""
import threading
from pathlib import Path

import httpx
import pytest

from src.common import version as updater
from build_update_manifest import build_manifest


@pytest.fixture(autouse=True)
def reset_updater(monkeypatch):
    monkeypatch.setattr(updater, '_update_info', None)
    monkeypatch.setattr(updater, '_update_worker', None)
    monkeypatch.setattr(updater, '_update_callbacks', [])
    monkeypatch.setattr(updater, '_update_checked_at', 0)
    monkeypatch.setattr(updater, '_get_proxy_url', lambda: None)
    monkeypatch.setattr(updater, '_get_github_token', lambda: '')
    monkeypatch.setattr(updater, 'get_version', lambda: '1.7.7')
    monkeypatch.setattr(updater, 'get_platform', lambda: 'windows')
    yield
    worker = updater._update_worker
    if worker:
        worker.join(3)
        assert not worker.is_alive()


def release():
    return {'tag_name': 'v1.7.8', 'assets': [
        {'name': f'vocab-harvester-1.7.8{suffix}',
         'browser_download_url': f'{updater._RELEASES}/download/v1.7.8/vocab-harvester-1.7.8{suffix}'}
        for suffix in ['-setup.exe', '.dmg']]}


def network(monkeypatch, handler):
    original = httpx.Client
    calls = []
    def tracked(request):
        calls.append(request)
        return handler(request)
    def factory(**kwargs):
        assert kwargs.get('verify', True) is True
        return original(**kwargs, transport=httpx.MockTransport(tracked), trust_env=False)
    monkeypatch.setattr(httpx, 'Client', factory)
    return calls


@pytest.mark.parametrize('platform,suffix', [('windows', '-setup.exe'), ('macos', '.dmg')])
def test_manifest_works_without_api_or_token(monkeypatch, platform, suffix):
    monkeypatch.setattr(updater, 'get_platform', lambda: platform)
    calls = network(monkeypatch, lambda r: httpx.Response(200, json=release()))
    info = updater._fetch_update_info()
    assert info['latest_version'] == '1.7.8'
    assert info['download_url'].endswith(suffix)
    assert [str(r.url) for r in calls] == [updater._UPDATE_MANIFEST]
    assert 'authorization' not in calls[0].headers


def test_token_only_sent_to_official_api(monkeypatch):
    monkeypatch.setattr(updater, '_get_github_token', lambda: 'private-test-token')
    calls = network(monkeypatch, lambda r: httpx.Response(200, json=release()) if str(r.url) == updater._GITHUB_API else httpx.Response(404))
    assert updater._fetch_update_info()['latest_version'] == '1.7.8'
    assert len(calls) == 2
    assert 'authorization' not in calls[0].headers
    assert calls[1].headers['authorization'] == 'Bearer private-test-token'


@pytest.mark.parametrize('status', [403, 429, 503])
def test_api_failure_falls_back_to_actual_release_page_assets(monkeypatch, status):
    def handler(r):
        if str(r.url) == updater._UPDATE_MANIFEST:
            return httpx.Response(404)
        if str(r.url) == updater._GITHUB_API:
            return httpx.Response(status)
        if str(r.url) == updater._RELEASES + '/latest':
            return httpx.Response(302, headers={'location': updater._RELEASES + '/tag/v1.7.8'})
        if '/expanded_assets/' in str(r.url):
            asset = release()['assets'][0]['browser_download_url']
            return httpx.Response(200, text=f'<a href="{asset}">Installer</a>')
        return httpx.Response(200, text='<html>Release page</html>')
    calls = network(monkeypatch, handler)
    result = updater._fetch_update_info()
    assert result['download_url'] == release()['assets'][0]['browser_download_url']
    assert not result.get('error')
    assert len([r for r in calls if r.url.host == 'api.github.com']) == 1
    assert {r.url.host for r in calls} == {'github.com', 'api.github.com'}


def test_all_sources_failed_returns_friendly_message_and_manual_link(monkeypatch):
    network(monkeypatch, lambda r: httpx.Response(403, text='raw private diagnostic'))
    info = updater._fetch_update_info()
    assert info['error_code'] == 'rate_limited'
    assert info['release_page'] == updater._RELEASES + '/latest'
    assert 'raw private' not in info['error'] and '403' not in info['error']
    assert not info.get('up_to_date')


@pytest.mark.parametrize('data', [{}, {'message': 'API failed'}, {'tag_name': 'v1.7.8', 'draft': True}, {'tag_name': 'v1.7.8-rc1'}, {'tag_name': '../../bad'}])
def test_invalid_metadata_is_not_reported_as_up_to_date(data):
    with pytest.raises(ValueError):
        updater._release_info(data)


def test_missing_or_external_installer_only_offers_release_page():
    data = release()
    data['assets'][0]['browser_download_url'] = 'https://example.com/untrusted.exe'
    info = updater._release_info(data)
    assert info['download_url'] == ''
    assert info['release_page'] == updater._RELEASES + '/tag/v1.7.8'


def test_current_or_newer_installation_does_not_offer_downgrade(monkeypatch):
    monkeypatch.setattr(updater, 'get_version', lambda: '1.7.9')
    assert updater._release_info(release())['up_to_date'] is True


def test_concurrent_startup_and_clicks_share_one_worker_and_notify_callbacks(monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    calls, callbacks = [], []
    def fetch():
        calls.append(1)
        entered.set()
        assert finish.wait(3)
        return {'up_to_date': True}
    monkeypatch.setattr(updater, '_fetch_update_info', fetch)
    first = updater.check_for_update_async(callbacks.append)
    assert entered.wait(2)
    assert updater.get_update_info() is None
    try:
        for _ in range(5):
            assert updater.check_for_update_async(callbacks.append) is first
    finally:
        finish.set()
    first.join(3)
    assert len(calls) == 1 and len(callbacks) == 6
    assert updater.check_for_update_async(callbacks.append) is None
    assert len(calls) == 1 and len(callbacks) == 7


@pytest.mark.parametrize('result,ttl', [({'up_to_date': True}, 300), ({'error': 'busy'}, 60)])
def test_success_cache_and_failure_cooldown_expire(monkeypatch, result, ttl):
    now = [1000.0]
    monkeypatch.setattr(updater.time, 'monotonic', lambda: now[0])
    calls = []
    monkeypatch.setattr(updater, '_fetch_update_info', lambda: calls.append(1) or result.copy())
    updater.check_for_update_async().join(3)
    now[0] += ttl - 1
    assert updater.check_for_update_async() is None
    assert len(calls) == 1
    now[0] += 2
    updater.check_for_update_async().join(3)
    assert len(calls) == 2


def test_public_manifest_matches_built_installers(tmp_path):
    win = tmp_path / 'vocab-harvester-1.7.8-setup.exe'
    mac = tmp_path / 'vocab-harvester-1.7.8.dmg'
    win.write_bytes(b'windows-test')
    mac.write_bytes(b'macos-test')
    manifest = build_manifest('1.7.8', win, mac, 'notes')
    assert updater._release_info(manifest)['download_url'] == release()['assets'][0]['browser_download_url']
    assert manifest['body'] == 'notes'
    win.write_bytes(b'')
    with pytest.raises(ValueError):
        build_manifest('1.7.8', win, mac, 'notes')
