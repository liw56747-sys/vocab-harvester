"""版本号管理 + 更新检查"""

from __future__ import annotations

import json
import logging
import platform
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 版本号 ──────────────────────────────────────────────

def get_version() -> str:
    """读取项目版本号（从 VERSION 文件第一行）"""
    version_file = Path(__file__).parent.parent.parent / "VERSION"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
        # 只取第一行作为版本号
        return text.splitlines()[0].strip() if text else "0.0.0"
    except FileNotFoundError:
        return "0.0.0"


def bump_version(current: str, part: str = "patch") -> str:
    """递增版本号：major / minor / patch"""
    major, minor, patch = (int(x) for x in current.split("."))
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def get_platform() -> str:
    """返回当前平台标识：windows / macos / linux"""
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform == "win32":
        return "windows"
    return "linux"


# ── 更新检查 ──────────────────────────────────────────

_GITHUB_API = "https://api.github.com/repos/liw56747-sys/vocab-harvester/releases/latest"
# 国内可访问的 GitHub 镜像 API
_GITHUB_MIRRORS = [
    "https://ghfast.top/https://api.github.com/repos/liw56747-sys/vocab-harvester/releases/latest",
    "https://gh-proxy.com/https://api.github.com/repos/liw56747-sys/vocab-harvester/releases/latest",
    "https://mirror.ghproxy.com/https://api.github.com/repos/liw56747-sys/vocab-harvester/releases/latest",
]
_update_info: dict | None = None


def _get_proxy_url() -> str | None:
    """自动检测代理地址：环境变量 > macOS系统代理 > 常见本地代理端口"""
    import os
    import subprocess

    # 1. 环境变量
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            logger.debug(f"Proxy from env {var}: {val}")
            return val

    # 2. macOS 系统代理（系统偏好设置 → 网络 → 代理）
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["scutil", "--proxy"],
                capture_output=True, text=True, timeout=3,
            )
            output = result.stdout
            # 解析 HTTPSProxy 配置
            # 格式: HTTPSProxy : {
            #   HTTPEnable : 1
            #   HTTPProxy : 127.0.0.1
            #   HTTPPort : 7890
            # }
            if "HTTPEnable : 1" in output:
                import re
                host_match = re.search(r"HTTPProxy : (.+)", output)
                port_match = re.search(r"HTTPPort : (\d+)", output)
                if host_match and port_match:
                    host = host_match.group(1).strip()
                    port = port_match.group(1).strip()
                    proxy_url = f"http://{host}:{port}"
                    logger.debug(f"Proxy from macOS system: {proxy_url}")
                    return proxy_url
        except Exception as e:
            logger.debug(f"Failed to read macOS system proxy: {e}")

    # 3. 探测常见本地代理端口（Clash / V2Ray / Shadowsocks 等）
    import socket
    common_ports = [
        ("127.0.0.1", 7890),   # Clash / ClashX
        ("127.0.0.1", 7891),   # Clash (alternative)
        ("127.0.0.1", 1087),   # V2Ray / Shadowsocks
        ("127.0.0.1", 1080),   # SOCKS proxy
        ("127.0.0.1", 8080),   # Common HTTP proxy
    ]
    for host, port in common_ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect((host, port))
            s.close()
            proxy_url = f"http://{host}:{port}"
            logger.debug(f"Proxy detected by port scan: {proxy_url}")
            return proxy_url
        except Exception:
            continue

    return None


def check_for_update_async(callback=None):
    """后台线程检查 GitHub 最新版本，完成后调用 callback(info)"""
    def _check():
        global _update_info
        # 重置状态，确保每次检查都是全新的
        _update_info = None

        proxy_url = _get_proxy_url()
        if proxy_url:
            proxy_handler = urllib.request.ProxyHandler({
                "https": proxy_url, "http": proxy_url,
            })
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()

        # 构建尝试列表：原始 API + 镜像
        apis = [_GITHUB_API] + _GITHUB_MIRRORS
        last_error = None

        for api_url in apis:
            try:
                req = urllib.request.Request(
                    api_url,
                    headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "vocab-harvester"},
                )
                with opener.open(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())

                latest = data.get("tag_name", "").lstrip("v")
                current = get_version()

                if latest and _version_gt(latest, current):
                    assets = data.get("assets", [])
                    plat = get_platform()

                    # 根据平台匹配安装包后缀
                    if plat == "macos":
                        suffix = ".dmg"
                    elif plat == "windows":
                        suffix = "-setup.exe"
                    else:
                        suffix = ".tar.gz"

                    download_url = ""
                    for asset in assets:
                        if asset["name"].endswith(suffix):
                            download_url = asset["browser_download_url"]
                            break

                    _update_info = {
                        "latest_version": latest,
                        "current_version": current,
                        "download_url": download_url,
                        "release_notes": data.get("body", ""),
                        "release_page": data.get("html_url", ""),
                        "platform": plat,
                    }
                    logger.info(f"Update available: {current} -> {latest} ({plat})")
                else:
                    _update_info = {"up_to_date": True}
                    logger.info(f"Up to date: {current}")
                return  # 成功，退出

            except Exception as e:
                last_error = e
                logger.debug(f"Update check failed ({api_url}): {e}")
                continue  # 尝试下一个 API

        # 所有 API 都失败
        _update_info = {"error": str(last_error)}

        if callback and _update_info:
            try:
                callback(_update_info)
            except Exception:
                pass

    # 立即重置，避免旧的错误结果被 /api/check-update 轮询读到
    global _update_info
    _update_info = None

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    return t


def get_update_info() -> dict | None:
    """获取更新检查结果（可能在后台线程尚未完成时返回 None）"""
    return _update_info


def _version_gt(a: str, b: str) -> bool:
    """比较版本号：a > b（支持预发布后缀如 1.2.0-rc1）"""
    import re

    def _parse(v: str) -> tuple:
        # 拆分核心版本和预发布后缀：1.2.0-rc1 → (1,2,0), "rc1"
        m = re.match(r'^(\d+(?:\.\d+)*)[\-._]?(.*)$', str(v).strip())
        if not m:
            return (0,)
        nums = tuple(int(x) for x in m.group(1).split('.'))
        pre = m.group(2)
        # 无后缀 > 有后缀（1.0.0 > 1.0.0-rc1）
        # 后缀按字典序比较（alpha < beta < rc）
        return nums + (0, pre) if pre else nums + (1, '')

    try:
        return _parse(a) > _parse(b)
    except (ValueError, AttributeError, TypeError):
        return False


# ── 下载更新 ──────────────────────────────────────────

def download_update(url: str, dest: Path, progress_callback=None) -> bool:
    """下载安装包，支持进度回调 callback(downloaded_bytes, total_bytes)"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vocab-harvester"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 256  # 256KB chunks

            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return False
