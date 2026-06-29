"""vocab-harvester 桌面应用入口

PyWebView 原生窗口 + FastAPI 后端线程，打包为独立 .exe。
"""

import os
import sys
import socket
import threading
import time
import asyncio
import traceback
from pathlib import Path


# ── PyInstaller 路径处理 ──
# sys._MEIPASS = _internal/ 目录（打包进来的静态资源）
# sys.executable 父目录 = exe 所在目录（用于写日志、数据库等可变文件）
_BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_EXE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

# 日志文件写到 exe 旁边，方便排查问题
_LOG_FILE = _EXE_DIR / "vocab-harvester.log"


def _log(msg: str):
    """写一行到日志文件，静默失败"""
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ── Playwright 浏览器路径 ──
# 打包后 Chromium 位于 _internal/playwright_browser/（Windows 打包方式）
_BUNDLED_BROWSER = _BUNDLED_DIR / "playwright_browser"
if _BUNDLED_BROWSER.is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_BUNDLED_BROWSER)
elif sys.platform == "darwin":
    # macOS：Chromium 不打包进 .app（codesign 兼容性问题）
    # 首次启动时自动安装到 ~/Library/Caches/ms-playwright
    _MAC_PW_PATH = Path.home() / "Library" / "Caches" / "ms-playwright"
    _MAC_CHROMIUM = _MAC_PW_PATH / "chromium-1223"  # 与 Windows 版本一致
    if not _MAC_CHROMIUM.is_dir():
        _log("Playwright Chromium not found, installing...")
        try:
            import subprocess
            # 打包后 sys.executable 是 app 自身，无 playwright 模块
            # 尝试系统 python3 和直接调用 playwright CLI
            for cmd in [
                ["python3", "-m", "playwright", "install", "chromium"],
                ["playwright", "install", "chromium"],
            ]:
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120,
                    )
                    if result.returncode == 0:
                        _log(f"Playwright Chromium installed via: {' '.join(cmd)}")
                        break
                except FileNotFoundError:
                    continue
            else:
                _log("Could not install Playwright Chromium automatically")
        except Exception as e:
            _log(f"Playwright install error: {e}")
    else:
        _log(f"Playwright Chromium found at {_MAC_PW_PATH}")

# 确保 _MEIPASS 根目录在 sys.path（让 from src.xxx import 正常工作）
if str(_BUNDLED_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_DIR))

# 实际使用的端口会在 main() 中动态确定
PORT = 8000
URL = ""

# 端口持久化文件（保持端口不变，让 localStorage 跨次启动有效）
_PORT_FILE = _EXE_DIR / ".port"


def _port_is_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _find_free_port(start: int = 8000, end: int = 8020) -> int:
    """优先复用上次的端口（持久化 localStorage），否则从 start 开始找空闲端口"""
    # 尝试上次的端口
    try:
        if _PORT_FILE.exists():
            saved = int(_PORT_FILE.read_text(encoding="utf-8").strip())
            if start <= saved < end and not _port_is_open(saved):
                return saved
    except (ValueError, OSError):
        pass
    # 找不到就扫描
    for port in range(start, end):
        if not _port_is_open(port):
            return port
    return start  # 都不空闲就用默认端口，让 uvicorn 报错


def _start_server(port: int):
    """在守护线程中启动 uvicorn"""
    try:
        _log(f"server thread starting, port={port}")
        _log(f"_BUNDLED_DIR={_BUNDLED_DIR}")
        _log(f"_EXE_DIR={_EXE_DIR}")
        _log(f"sys.path[0]={sys.path[0] if sys.path else '(empty)'}")

        import uvicorn
        _log("uvicorn imported")

        from src.api.main import app as fastapi_app
        _log("fastapi app imported")

        # 初始化数据库
        from src.common.config import load_settings
        from src.common.database import init_db

        settings = load_settings()
        # 数据库写到 exe 旁边（而非 _internal），避免打包时丢失
        db_dir = _EXE_DIR / "data"
        db_dir.mkdir(exist_ok=True)
        db_path = db_dir / "vocab.db"
        _log(f"database path: {db_path}")

        asyncio.run(init_db(db_path))
        _log("database initialized")

        uvicorn.run(
            fastapi_app,
            host="127.0.0.1",
            port=port,
            reload=False,
            log_level="warning",
            log_config=None,
        )
    except Exception as e:
        _log(f"FATAL: server thread crashed: {e}")
        _log(traceback.format_exc())


def _wait_for_server(port: int, max_wait: int = 15) -> bool:
    """轮询等待服务器就绪"""
    start = time.time()
    while time.time() - start < max_wait:
        if _port_is_open(port):
            return True
        time.sleep(0.3)
    return False


def main():
    global PORT, URL

    _log("=== vocab-harvester starting ===")
    _log(f"frozen={getattr(sys, 'frozen', False)}")

    # 启动后台更新检查
    try:
        from src.common.version import check_for_update_async, get_version
        current_ver = get_version()
        _log(f"current version: {current_ver}")
        check_for_update_async()
        _log("update check started in background")
    except Exception as e:
        _log(f"update check init failed: {e}")

    # 自动找空闲端口（优先复用上次的）
    PORT = _find_free_port()
    URL = f"http://127.0.0.1:{PORT}"
    _log(f"selected port: {PORT}")

    # 保存端口以便下次启动复用（保持 localStorage origin 不变）
    try:
        _PORT_FILE.write_text(str(PORT), encoding="utf-8")
    except OSError:
        pass

    # 启动后端
    server_thread = threading.Thread(target=_start_server, args=(PORT,), daemon=True)
    server_thread.start()

    if not _wait_for_server(PORT):
        _log("server failed to start within timeout")
        # 读取日志给用户看
        log_text = ""
        if _LOG_FILE.exists():
            try:
                log_text = _LOG_FILE.read_text(encoding="utf-8")[-500:]
            except Exception:
                pass
        import webview
        html = f"<h2>服务器启动失败</h2><p>端口 {PORT} 无法使用。</p>"
        if log_text:
            html += f"<h3>诊断日志：</h3><pre style='font-size:11px;background:#f5f5f5;padding:8px;overflow:auto'>{log_text}</pre>"
        webview.create_window("错误", html=html)
        webview.start()
        sys.exit(1)

    _log("server ready, creating webview window")

    # 创建原生窗口
    import webview

    # JS API：提供原生文件保存能力（PyWebView 不支持 HTML5 download 属性）
    class JsApi:
        def save_file(self, b64_data: str, filename: str):
            """将 base64 数据保存到用户的 Downloads 文件夹"""
            import base64
            downloads = Path.home() / "Downloads"
            downloads.mkdir(exist_ok=True)
            dest = downloads / filename
            # 避免覆盖
            counter = 1
            while dest.exists():
                stem = dest.stem
                suffix = dest.suffix
                dest = downloads / f"{stem}_{counter}{suffix}"
                counter += 1
            dest.write_bytes(base64.b64decode(b64_data))
            _log(f"file saved: {dest}")
            return str(dest)

    # 图标在 _internal/ 里（打包数据），也在 exe 目录里
    icon_path = _BUNDLED_DIR / "icon.ico"
    if not icon_path.exists():
        icon_path = _EXE_DIR / "icon.ico"

    window = webview.create_window(
        "vocab-harvester",
        url=URL,
        width=1200,
        height=800,
        min_size=(900, 600),
        js_api=JsApi(),
    )

    # 持久化浏览器数据目录（localStorage 等跨次启动保留）
    webview_data = _EXE_DIR / "data" / "webview"
    webview_data.mkdir(parents=True, exist_ok=True)

    webview.start(
        debug=False,
        private_mode=False,
        storage_path=str(webview_data),
    )
    _log("application exited")


if __name__ == "__main__":
    main()
