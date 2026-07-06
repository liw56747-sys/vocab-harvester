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

# 🌟 修复 Bug：强制在全局作用域导入核心模块 🌟
# 这样可以让 PyInstaller 的静态分析器识别并打包这些依赖，防止运行时报 ModuleNotFoundError
import src.api.main
import src.api.routes
import src.common.config
import src.common.database
import src.common.version


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
_NEEDS_CHROMIUM_INSTALL = False

if _BUNDLED_BROWSER.is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_BUNDLED_BROWSER)
elif sys.platform == "darwin":
    # macOS：Chromium 不打包进 .app（codesign 兼容性问题）
    # 首次启动时通过 API 端点安装，前端显示引导界面
    _MAC_PW_PATH = Path.home() / "Library" / "Caches" / "ms-playwright"
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_MAC_PW_PATH))
    # 检查是否已有 Chromium
    _chromium_found = any(
        (_MAC_PW_PATH / d).is_dir()
        for d in os.listdir(_MAC_PW_PATH)
        if d.startswith("chromium")
    ) if _MAC_PW_PATH.exists() else False
    if not _chromium_found:
        _NEEDS_CHROMIUM_INSTALL = True
        _log("Playwright Chromium not found, will install via API endpoint")
    else:
        _log(f"Playwright Chromium found at {_MAC_PW_PATH}")

# 确保 _MEIPASS 根目录在 sys.path（让 from src.xxx import 正常工作）
if str(_BUNDLED_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLED_DIR))

# 实际使用的端口会在 main() 中动态确定
PORT = 8000
URL = ""

# 服务器线程错误捕获（让主线程能看到 uvicorn 的具体报错）
_SERVER_ERROR = None

# 端口持久化文件（保持端口不变，让 localStorage 跨次启动有效）
_PORT_FILE = _EXE_DIR / ".port"


def _can_bind_to_port(port: int) -> bool:
    """尝试绑定端口来检测是否可用（比 connect 更可靠）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def _port_is_listening(port: int, timeout: float = 0.3) -> bool:
    """检查端口是否已有服务在监听"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _find_free_port(start: int = 8000, end: int = 8100) -> int:
    """优先复用上次的端口（持久化 localStorage），否则用 bind 检测找空闲端口"""
    # 尝试上次的端口
    try:
        if _PORT_FILE.exists():
            saved = int(_PORT_FILE.read_text(encoding="utf-8").strip())
            if start <= saved < end and _can_bind_to_port(saved):
                return saved
    except (ValueError, OSError):
        pass
    # 扫描范围内的端口
    for port in range(start, end):
        if _can_bind_to_port(port):
            return port
    # 都不可用就用 port=0 让操作系统分配
    return 0


def _kill_existing_instance(port: int = 8000):
    """安装新版本前杀死旧实例，确保端口释放"""
    if not _port_is_listening(port):
        return

    _log(f"existing instance detected on port {port}, attempting to terminate")
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.run(
                ["taskkill", "/F", "/IM", "vocab-harvester.exe"],
                capture_output=True, timeout=10,
            )
        else:
            import signal
            pid_str = ""
            try:
                import subprocess
                r = subprocess.run(
                    ["lsof", "-ti", f"tcp:{port}"],
                    capture_output=True, text=True, encoding="utf-8", timeout=5,
                )
                pid_str = r.stdout.strip()
            except Exception:
                pass
            if pid_str:
                current_pid = str(os.getpid())
                for line in pid_str.split("\n"):
                    pid = line.strip()
                    if pid and pid != current_pid:
                        try:
                            os.kill(int(pid), signal.SIGTERM)
                            _log(f"sent SIGTERM to PID {pid}")
                        except Exception as e:
                            _log(f"failed to kill PID {pid}: {e}")
        time.sleep(2)
    except Exception as e:
        _log(f"failed to kill existing instance: {e}")


def _start_server(port: int):
    """在守护线程中启动 uvicorn"""
    global _SERVER_ERROR, PORT, URL
    try:
        _log(f"server thread starting, port={port}")
        _log(f"_BUNDLED_DIR={_BUNDLED_DIR}")
        _log(f"_EXE_DIR={_EXE_DIR}")
        _log(f"CWD before chdir: {os.getcwd()}")
        _log(f"sys.path[0]={sys.path[0] if sys.path else '(empty)'}")

        # 关键修复：打包后 CWD 可能不是 exe 所在目录，
        # 导致 lifespan 中的 Path("./data") 解析到错误位置，
        # init_db 静默失败，uvicorn 立即退出。
        os.chdir(_EXE_DIR)
        _log(f"CWD after chdir: {os.getcwd()}")

        import uvicorn
        _log("uvicorn imported")

        from src.api.main import app as fastapi_app
        _log("fastapi app imported")

        # 确保数据目录存在（数据库初始化由 FastAPI lifespan 负责，
        # 避免在此处调用 asyncio.run(init_db(...))，否则连接会绑定到
        # 一个已销毁的事件循环，导致 uvicorn 启动后所有 DB 操作失败）
        from src.common.config import load_settings

        settings = load_settings()
        db_dir = _EXE_DIR / "data"
        db_dir.mkdir(exist_ok=True)
        _log(f"database dir ready: {db_dir}")

        # 尝试启动 uvicorn，如果端口绑定失败则尝试备选端口
        actual_port = port
        try:
            config = uvicorn.Config(
                fastapi_app,
                host="127.0.0.1",
                port=port,
                reload=False,
                log_level="warning",
                log_config=None,
            )
            server = uvicorn.Server(config)
            _log(f"uvicorn config created, port={port}")

            # 在事件循环中运行，以便捕获绑定错误
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(server.serve())
            finally:
                loop.close()

            # 检测 uvicorn 是否真正启动成功（lifespan 失败时会静默退出）
            if not getattr(server, "started", False):
                err_msg = (
                    f"uvicorn 未能启动 (port={port})，"
                    f"可能是 lifespan 初始化失败，请检查日志"
                )
                _log(err_msg)
                _SERVER_ERROR = err_msg
                return

        except (OSError, Exception) as bind_err:
            _log(f"port bind/start failed: {bind_err}")
            # 尝试扫描其他端口
            for alt_port in range(8000, 8100):
                if alt_port == port:
                    continue
                if not _can_bind_to_port(alt_port):
                    continue
                _log(f"trying alternative port: {alt_port}")
                try:
                    actual_port = alt_port
                    PORT = alt_port
                    URL = f"http://127.0.0.1:{alt_port}"
                    config = uvicorn.Config(
                        fastapi_app,
                        host="127.0.0.1",
                        port=alt_port,
                        reload=False,
                        log_level="warning",
                        log_config=None,
                    )
                    server = uvicorn.Server(config)
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(server.serve())
                    finally:
                        loop.close()
                    break
                except (OSError, Exception) as alt_err:
                    _log(f"alt port {alt_port} also failed: {alt_err}")
                    continue
            else:
                _SERVER_ERROR = f"所有端口 (8000-8099) 均不可用: {bind_err}"
                _log(_SERVER_ERROR)
                return

        _log(f"uvicorn exited normally (was on port {actual_port})")

    except Exception as e:
        _SERVER_ERROR = str(e)
        _log(f"FATAL: server thread crashed: {e}")
        _log(traceback.format_exc())


def _wait_for_server(port: int, max_wait: int = 20) -> bool:
    """轮询等待服务器就绪，同时检查线程是否已崩溃"""
    start = time.time()
    while time.time() - start < max_wait:
        if _SERVER_ERROR:
            return False
        if _port_is_listening(port):
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

    # 杀死旧实例（更新安装时旧进程可能还在运行）
    _kill_existing_instance()

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
                log_text = _LOG_FILE.read_text(encoding="utf-8")[-800:]
            except Exception:
                pass
        import webview
        error_detail = _SERVER_ERROR or f"端口 {PORT} 无法使用"
        html = f"<h2>服务器启动失败</h2><p>{error_detail}</p>"
        if log_text:
            html += f"<h3>诊断日志：</h3><pre style='font-size:11px;background:#f5f5f5;padding:8px;overflow:auto;max-height:300px'>{log_text}</pre>"
        webview.create_window("错误", html=html)
        webview.start()
        sys.exit(1)

    _log("server ready, creating webview window")

    # 创建原生窗口
    import webview

    # JS API：提供原生文件保存能力（PyWebView 不支持 HTML5 download 属性）
    class JsApi:
        def __init__(self):
            self.window = None

        def save_file(self, b64_data: str, filename: str, folder: str = ""):
            """将 base64 数据保存到指定文件夹（或默认 Downloads）"""
            import base64
            if folder:
                dest_dir = Path(folder)
            else:
                dest_dir = Path.home() / "Downloads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / filename
            # 避免覆盖
            counter = 1
            while dest.exists():
                stem = dest.stem
                suffix = dest.suffix
                dest = dest_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            dest.write_bytes(base64.b64decode(b64_data))
            _log(f"file saved: {dest}")
            return str(dest)

        def choose_folder(self):
            """打开原生文件夹选择对话框，返回用户选择的路径"""
            if not self.window:
                return ""
            result = self.window.create_file_dialog(
                webview.FileDialog.FOLDER,
            )
            if result and len(result) > 0:
                return result[0]
            return ""

    # 图标在 _internal/ 里（打包数据），也在 exe 目录里
    icon_path = _BUNDLED_DIR / "icon.ico"
    if not icon_path.exists():
        icon_path = _EXE_DIR / "icon.ico"

    js_api = JsApi()
    window = webview.create_window(
        "vocab-harvester",
        url=URL,
        width=1200,
        height=800,
        min_size=(900, 600),
        js_api=js_api,
    )
    js_api.window = window

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
