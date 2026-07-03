"""vocab-harvester 系统托盘管理器

功能：
- 系统托盘图标，实时显示服务器运行状态（绿/橙/红）
- 启动后自动等待服务器就绪，然后自动打开浏览器
- 左键单击：在浏览器中打开 http://127.0.0.1:8000
- 右键菜单：启动/停止/重启服务器、打开浏览器、退出

用法：python tray_launcher.py
"""

import os
import sys
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

ROOT_DIR = Path(__file__).parent
SERVER_SCRIPT = ROOT_DIR / "start.py"
PORT = 8000
URL = f"http://127.0.0.1:{PORT}"

# ── 图标生成 ──

def _make_icon(color: str) -> Image.Image:
    """生成圆形状态图标（32x32）"""
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 30, 30], fill=color, outline="white", width=1)
    draw.text((10, 7), "V", fill="white")
    return img


ICON_RUNNING = _make_icon("#4CAF50")   # 绿色 = 运行中
ICON_STOPPED = _make_icon("#F44336")   # 红色 = 已停止
ICON_STARTING = _make_icon("#FF9800")  # 橙色 = 启动中


def _port_is_open(port: int, timeout: float = 0.2) -> bool:
    """检测端口是否在监听"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


# ── 服务器进程管理 ──

class ServerManager:
    def __init__(self):
        self._process = None
        self._lock = threading.Lock()
        self._starting = False

    @property
    def is_starting(self) -> bool:
        return self._starting

    @property
    def is_running(self) -> bool:
        if self._starting:
            return False
        with self._lock:
            if self._process is None:
                return False
            return self._process.poll() is None

    def start(self, auto_open_browser: bool = False) -> bool:
        with self._lock:
            if self._process and self._process.poll() is None:
                return False  # 已在运行
            if _port_is_open(PORT):
                # 端口已被占用（可能是其他进程启动的服务器）
                if auto_open_browser:
                    webbrowser.open(URL)
                return False
            self._starting = True
            self._process = subprocess.Popen(
                [sys.executable, str(SERVER_SCRIPT), "--skip-tests"],
                cwd=str(ROOT_DIR),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._lock_pid = self._process.pid

        # 在后台等待服务器就绪
        def _wait_ready():
            max_wait = 30  # 最多等 30 秒
            start_time = time.time()
            while time.time() - start_time < max_wait:
                if _port_is_open(PORT):
                    self._starting = False
                    if auto_open_browser:
                        webbrowser.open(URL)
                    return
                # 检查进程是否已退出（启动失败）
                with self._lock:
                    if self._process and self._process.poll() is not None:
                        self._starting = False
                        return
                time.sleep(0.5)
            # 超时
            self._starting = False

        threading.Thread(target=_wait_ready, daemon=True).start()
        return True

    def stop(self) -> bool:
        self._starting = False
        with self._lock:
            if not self._process or self._process.poll() is not None:
                self._process = None
                return False
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            return True

    def restart(self, auto_open_browser: bool = False):
        self.stop()
        time.sleep(1)
        self.start(auto_open_browser=auto_open_browser)


# ── 托盘应用 ──

class TrayApp:
    def __init__(self):
        self.server = ServerManager()
        self._icon = None

    def _get_icon_image(self) -> Image.Image:
        if self.server.is_starting:
            return ICON_STARTING
        if self.server.is_running:
            return ICON_RUNNING
        return ICON_STOPPED

    def _get_tooltip(self) -> str:
        if self.server.is_starting:
            return "vocab-harvester 启动中..."
        if self.server.is_running:
            return f"vocab-harvester 运行中 ({URL})"
        return "vocab-harvester 已停止"

    def _open_browser(self, icon, item):
        if self.server.is_running:
            webbrowser.open(URL)
        elif not self.server.is_starting:
            self.server.start(auto_open_browser=True)

    def _start_server(self, icon, item):
        self.server.start()

    def _stop_server(self, icon, item):
        self.server.stop()

    def _restart_server(self, icon, item):
        threading.Thread(
            target=self.server.restart, kwargs={"auto_open_browser": True}, daemon=True
        ).start()

    def _exit(self, icon, item):
        self.server.stop()
        icon.stop()

    def run(self):
        # 启动时自动拉起服务器，就绪后自动打开浏览器
        self.server.start(auto_open_browser=True)

        menu = pystray.Menu(
            pystray.MenuItem("打开浏览器", self._open_browser, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("启动服务", self._start_server),
            pystray.MenuItem("停止服务", self._stop_server),
            pystray.MenuItem("重启服务", self._restart_server),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._exit),
        )

        self._icon = pystray.Icon(
            "vocab-harvester",
            icon=self._get_icon_image(),
            title=self._get_tooltip(),
            menu=menu,
        )

        # 定时刷新图标状态
        def _refresh():
            while True:
                time.sleep(2)
                if self._icon:
                    self._icon.icon = self._get_icon_image()
                    self._icon.title = self._get_tooltip()

        t = threading.Thread(target=_refresh, daemon=True)
        t.start()

        self._icon.run()


if __name__ == "__main__":
    TrayApp().run()
