"""Windows 桌面渲染兼容设置、诊断与 WebView2 故障恢复。"""
from __future__ import annotations

import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import webbrowser

logger = logging.getLogger(__name__)
_fault_file = None


def configure_windows_rendering(environ=None, platform=None):
    """必须先于 webview 导入/窗口创建；追加而非 setdefault，保留已有参数。"""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if platform != 'win32':
        return
    args = environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '').strip()
    for flag in ('--disable-gpu', '--disable-gpu-compositing', '--disable-direct-composition'):
        if not re.search(r'(?<!\S)' + re.escape(flag) + r'(?!\S)', args):
            args = (args + ' ' + flag).strip()
    environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = args


def install_desktop_logging(data_dir: Path) -> Path:
    """日志放在可写的用户数据目录；无控制台安装版也记录线程/原生异常。"""
    global _fault_file
    log_dir = data_dir / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / 'vocab-harvester.log'
    handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s'))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def exception_hook(exc_type, exc_value, exc_traceback):
        logger.critical('主线程未处理异常', exc_info=(exc_type, exc_value, exc_traceback))
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'程序运行异常。诊断日志已保存到：\n{log_path}', 'vocab-harvester', 0x10)
    sys.excepthook = exception_hook
    threading.excepthook = lambda args: logger.error(
        '后台线程未处理异常: %s', args.thread.name if args.thread else 'unknown',
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    try:
        fault_path = log_dir / 'native-crash.log'
        if fault_path.exists() and fault_path.stat().st_size > 5 * 1024 * 1024:
            fault_path.replace(log_dir / 'native-crash.previous.log')
        _fault_file = fault_path.open('a', encoding='utf-8')
        faulthandler.enable(file=_fault_file, all_threads=True)
    except (OSError, RuntimeError):
        logger.warning('无法启用原生崩溃日志', exc_info=True)
    return log_path


def report_ui_event(event):
    """仅记录导航/异常定位字段，不记录表单、Cookie、密钥或页面内容。"""
    if not isinstance(event, dict):
        return
    safe = {key: str(event[key])[:160] for key in ('kind', 'page', 'error_type', 'file', 'line', 'column') if key in event}
    logger.info('UI %s', json.dumps(safe, ensure_ascii=False))


class WindowsRenderMonitor:
    """监听原生事件；所有 WebView2 操作均在 WinForms UI 线程执行。"""

    def __init__(self, window, url: str, log_path: Path):
        self.window = window
        self.url = url
        self.log_path = log_path
        self._core = None
        self._prompting = False
        self._failure_handler = self._on_process_failed

    def attach(self, *_):
        if sys.platform != 'win32':
            return
        try:
            native = self.window.native
            if native.InvokeRequired:
                from System import Action
                native.BeginInvoke(Action(self._attach_on_ui_thread))
            else:
                self._attach_on_ui_thread()
        except Exception:
            logger.exception('无法注册 WebView2 故障监测')

    def _attach_on_ui_thread(self):
        try:
            core = self.window.native.webview.CoreWebView2
            if core is None or core == self._core:
                return
            core.ProcessFailed += self._failure_handler
            self._core = core
            logger.info('WebView2 ready; runtime=%s; software_rendering=True', core.Environment.BrowserVersionString)
        except Exception:
            logger.exception('注册 WebView2 ProcessFailed 失败')

    def _on_process_failed(self, sender, args):
        kind = str(args.ProcessFailedKind)
        reason = str(getattr(args, 'Reason', 'unknown'))
        exit_code = getattr(args, 'ExitCode', 'unknown')
        logger.error('WebView2 ProcessFailed kind=%s reason=%s exit_code=%s', kind, reason, exit_code)
        # GPU/工具子进程由 WebView2 自行恢复；渲染进程故障需要用户决定是否重载。
        if kind not in ('RenderProcessExited', 'RenderProcessUnresponsive', 'FrameRenderProcessExited', 'BrowserProcessExited') or self._prompting:
            return
        self._prompting = True
        # WebView2 不支持在事件回调中运行模态消息循环；返回后再提示/重载。
        try:
            from System import Action
            self.window.native.BeginInvoke(Action(lambda: self._recover(sender, kind)))
        except Exception:
            self._prompting = False
            logger.exception('无法调度 WebView2 恢复提示')

    def _recover(self, sender, kind):
        try:
            if kind == 'BrowserProcessExited':
                self._offer_browser()
            elif self._confirm('界面显示进程出现异常。是否重新加载界面？\n\n尚未保存的输入将丢失；重新加载不会取消后台任务。'):
                try:
                    sender.Reload()
                    logger.info('WebView2 已按用户选择重新加载')
                except Exception:
                    logger.exception('WebView2 重载失败')
                    self._offer_browser()
        finally:
            self._prompting = False

    def _confirm(self, message):
        import ctypes
        message += f'\n\n诊断日志：{self.log_path}'
        return ctypes.windll.user32.MessageBoxW(None, message, 'vocab-harvester 界面恢复', 0x24) == 6

    def _offer_browser(self):
        if self._confirm('内嵌界面暂时无法恢复。是否在系统浏览器中打开界面？\n\n请保持当前程序窗口打开，以继续使用后台服务。'):
            self.open_in_browser()

    def open_in_browser(self):
        # 保持后台服务所在窗口开启，浏览器使用普通样式。
        logger.info('打开系统浏览器备用界面；请保持程序窗口开启')
        webbrowser.open(self.url.split('?', 1)[0])
