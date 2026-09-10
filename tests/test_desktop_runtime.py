"""桌面渲染参数及原生故障恢复的回归测试，无需真实 Windows 窗口。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from src.common import desktop_runtime as runtime


def test_windows_flags_merge_and_are_idempotent():
    env = {'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS': '--remote-debugging-port=9222 --disable-gpu-compositing'}
    runtime.configure_windows_rendering(env, 'win32')
    once = env.copy()
    runtime.configure_windows_rendering(env, 'win32')
    assert env == once
    flags = env['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'].split()
    assert flags.count('--disable-gpu') == 1
    assert flags.count('--disable-gpu-compositing') == 1
    assert '--disable-direct-composition' in flags
    assert '--remote-debugging-port=9222' in flags


def test_non_windows_environment_untouched():
    env = {}
    runtime.configure_windows_rendering(env, 'darwin')
    assert env == {}


def test_ui_diagnostics_exclude_payloads(caplog):
    with caplog.at_level('INFO'):
        runtime.report_ui_event({'kind': 'navigation', 'page': 'page-twitter', 'cookie': 'SECRET',
                                 'message': 'TOKEN', 'error_type': 'E' * 400})
        runtime.report_ui_event('not a dictionary')
    assert 'page-twitter' in caplog.text
    assert 'SECRET' not in caplog.text and 'TOKEN' not in caplog.text
    assert 'E' * 161 not in caplog.text


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


@pytest.fixture
def monitor(monkeypatch, tmp_path):
    # BeginInvoke 必须排队而非立即执行：禁止在 WebView2 回调内开启模态循环。
    pending = []
    core = SimpleNamespace(ProcessFailed=Event(), Environment=SimpleNamespace(BrowserVersionString='test'), Reload=Mock())
    native = SimpleNamespace(InvokeRequired=True, BeginInvoke=pending.append,
                             webview=SimpleNamespace(CoreWebView2=core))
    monkeypatch.setitem(sys.modules, 'System', SimpleNamespace(Action=lambda fn: fn))
    instance = runtime.WindowsRenderMonitor(SimpleNamespace(native=native), 'http://127.0.0.1:8000/?desktop=windows', tmp_path / 'desktop.log')
    instance._confirm = Mock(return_value=True)
    return instance, core, pending


def fail(instance, core, kind='RenderProcessExited'):
    instance._on_process_failed(core, SimpleNamespace(ProcessFailedKind=kind, Reason='Crashed', ExitCode=42))


def test_attach_marshals_to_ui_thread_and_subscribes_once(monitor, monkeypatch):
    instance, core, pending = monitor
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    instance.attach()
    assert not core.ProcessFailed.handlers
    pending.pop(0)()
    instance.attach()
    pending.pop(0)()
    assert len(core.ProcessFailed.handlers) == 1


@pytest.mark.parametrize('kind', ['RenderProcessExited', 'RenderProcessUnresponsive', 'FrameRenderProcessExited'])
def test_recovery_is_deferred_and_reload_requires_confirmation(monitor, caplog, kind):
    instance, core, pending = monitor
    fail(instance, core, kind)
    instance._confirm.assert_not_called()
    core.Reload.assert_not_called()
    fail(instance, core, kind)  # 同一恢复周期不重复提示
    assert len(pending) == 1
    pending.pop(0)()
    core.Reload.assert_called_once()
    assert not instance._prompting
    assert 'exit_code=42' in caplog.text


def test_cancel_preserves_page(monitor):
    instance, core, pending = monitor
    instance._confirm.return_value = False
    fail(instance, core)
    pending.pop(0)()
    core.Reload.assert_not_called()
    assert not instance._prompting


@pytest.mark.parametrize('kind', ['GpuProcessExited', 'UtilityProcessExited'])
def test_self_recovering_processes_only_logged(monitor, kind):
    instance, core, pending = monitor
    fail(instance, core, kind)
    assert not pending
    instance._confirm.assert_not_called()


@pytest.mark.parametrize('kind', ['BrowserProcessExited', 'RenderProcessExited'])
def test_system_browser_fallback(monitor, monkeypatch, kind):
    instance, core, pending = monitor
    core.Reload.side_effect = RuntimeError('WebView unavailable')
    opened = Mock()
    monkeypatch.setattr(runtime.webbrowser, 'open', opened)
    fail(instance, core, kind)
    pending.pop(0)()
    opened.assert_called_once_with('http://127.0.0.1:8000/')


def test_browser_fallback_cancel(monitor, monkeypatch):
    instance, core, pending = monitor
    instance._confirm.return_value = False
    opened = Mock()
    monkeypatch.setattr(runtime.webbrowser, 'open', opened)
    fail(instance, core, 'BrowserProcessExited')
    pending.pop(0)()
    opened.assert_not_called()


def test_failed_dispatch_can_retry(monitor):
    instance, core, pending = monitor
    instance.window.native.BeginInvoke = Mock(side_effect=RuntimeError('disposed'))
    fail(instance, core)
    assert not instance._prompting


def test_logging_captures_uncaught_main_and_worker_exceptions(tmp_path):
    import subprocess
    code = """
import sys, threading
from pathlib import Path
from src.common.desktop_runtime import install_desktop_logging
install_desktop_logging(Path(sys.argv[1]))
if sys.platform == 'win32':
    import ctypes
    ctypes.windll.user32.MessageBoxW = lambda *args: 0
def worker():
    raise RuntimeError('worker-test')
t = threading.Thread(target=worker, name='test-worker')
t.start()
t.join()
raise ValueError('main-test')
"""
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
    assert result.returncode == 1
    log = (tmp_path / 'logs/vocab-harvester.log').read_text(encoding='utf-8')
    assert 'worker-test' in log and 'test-worker' in log
    assert 'main-test' in log and 'ValueError' in log
    assert (tmp_path / 'logs/native-crash.log').exists()
