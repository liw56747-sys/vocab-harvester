"""版本号管理 + 更新检查"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
import time
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
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

_RELEASES = "https://github.com/liw56747-sys/vocab-harvester/releases"
_GITHUB_API = "https://api.github.com/repos/liw56747-sys/vocab-harvester/releases/latest"
_UPDATE_MANIFEST = _RELEASES + "/latest/download/update.json"
_update_info: dict | None = None
_update_lock = threading.RLock()
_update_worker: threading.Thread | None = None
_update_callbacks = []
_update_checked_at = 0.0
_UPDATE_CACHE_SECONDS = 300
_UPDATE_ERROR_COOLDOWN = 60


def _get_github_token() -> str:
    """从环境变量读取 GitHub Token（支持 .env 文件或系统环境变量）"""
    # 1. 系统环境变量
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    # 2. 查找 .env 文件（兼容源码运行和 PyInstaller 打包）
    env_candidates = []
    # PyInstaller 打包后: sys._MEIPASS 是临时解压目录
    if getattr(sys, 'frozen', False):
        # .app bundle 的 Resources 目录（macOS）
        if sys.platform == "darwin":
            bundle_root = Path(sys.executable).parent.parent / "Resources"
            env_candidates.append(bundle_root / ".env")
        # Windows _internal 目录
        env_candidates.append(Path(sys.executable).parent / "_internal" / ".env")
        env_candidates.append(Path(sys.executable).parent / ".env")
        # _MEIPASS 临时目录
        meipass = Path(getattr(sys, '_MEIPASS', ''))
        if meipass.exists():
            env_candidates.append(meipass / ".env")
    # 源码运行: 项目根目录
    env_candidates.append(Path(__file__).parent.parent.parent / ".env")
    # 当前工作目录
    env_candidates.append(Path.cwd() / ".env")
    # 用户 home 目录
    env_candidates.append(Path.home() / ".vocab-harvester" / ".env")

    for env_file in env_candidates:
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("GITHUB_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            logger.debug(f"GitHub Token loaded from {env_file}")
                            return val
            except Exception:
                continue
    return ""


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


def _release_info(data: dict) -> dict:
    """只接受正式版本与本仓库的安装包地址，空/错误响应不能当成已是最新版。"""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise ValueError("不是正式发布版本")
    tag = data.get("tag_name", "")
    if not isinstance(tag, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise ValueError("版本信息缺少有效版本号")
    latest = tag.removeprefix("v")
    current = get_version()
    plat = get_platform()
    suffix = {"windows": "-setup.exe", "macos": ".dmg"}.get(plat, ".tar.gz")
    filename = f"vocab-harvester-{latest}{suffix}"
    expected_url = f"{_RELEASES}/download/{tag}/{filename}"
    download_url = ""
    for asset in data.get("assets", []):
        if asset.get("name") == filename and asset.get("browser_download_url") == expected_url:
            download_url = expected_url
            break
    if not _version_gt(latest, current):
        return {"up_to_date": True, "current_version": current}
    return {
        "latest_version": latest, "current_version": current, "platform": plat,
        "download_url": download_url, "release_page": f"{_RELEASES}/tag/{tag}",
        "release_notes": str(data.get("body", "")),
    }


class _AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(urljoin("https://github.com", href))


def _release_from_page(client) -> dict:
    """旧版没有 update.json 或 API 限流时，从官方发布页面读取版本与实际附件。"""
    response = client.get(_RELEASES + "/latest")
    response.raise_for_status()
    url = urlparse(str(response.url))
    prefix = urlparse(_RELEASES).path + "/tag/"
    if url.scheme != "https" or url.netloc != "github.com" or not url.path.startswith(prefix):
        raise ValueError("无法确定最新正式版本页面")
    tag = url.path[len(prefix):]
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise ValueError("发布页面版本号无效")
    # GitHub 使用独立 HTML 片段加载附件，不能只猜测文件是否存在。
    assets = client.get(_RELEASES + "/expanded_assets/" + tag)
    assets.raise_for_status()
    parser = _AssetLinks()
    parser.feed(assets.text)
    return {"tag_name": tag, "assets": [
        {"name": link.rsplit("/", 1)[-1], "browser_download_url": link}
        for link in parser.links
    ]}


def _fetch_update_info() -> dict:
    import httpx

    rate_limited = False
    proxy = _get_proxy_url()
    # 校验证书；版本清单和下载信息只从官方 GitHub 获取，不依赖第三方 API 镜像。
    try:
        with httpx.Client(proxy=proxy, timeout=6, follow_redirects=True,
                          headers={"User-Agent": "vocab-harvester"}) as client:
            sources = [("manifest", _UPDATE_MANIFEST), ("api", _GITHUB_API), ("page", None)]
            for source, url in sources:
                try:
                    if source == "page":
                        data = _release_from_page(client)
                    else:
                        headers = {}
                        if source == "api":
                            headers["Accept"] = "application/vnd.github+json"
                            token = _get_github_token()
                            if token:
                                headers["Authorization"] = f"Bearer {token}"
                        response = client.get(url, headers=headers)
                        if response.status_code in (403, 429):
                            rate_limited = True
                        response.raise_for_status()
                        data = response.json()
                    info = _release_info(data)
                    logger.info("更新检查成功，来源=%s", source)
                    return info
                except Exception as exc:
                    logger.warning("更新检查来源 %s 不可用: %s", source, type(exc).__name__)
    except Exception as exc:
        logger.warning("更新检查连接失败: %s", type(exc).__name__)
    return {
        "error": "更新服务暂时繁忙，请稍后重试或打开下载页手动更新。" if rate_limited
                 else "暂时无法连接更新服务，请检查网络或打开下载页手动更新。",
        "error_code": "rate_limited" if rate_limited else "unavailable",
        "release_page": _RELEASES + "/latest",
    }


def _notify_update(callback, info):
    try:
        callback(dict(info))
    except Exception:
        logger.exception("更新检查回调失败")


def check_for_update_async(callback=None):
    """合并并发检查；成功缓存 5 分钟、失败冷却 60 秒，避免启动/点击重复请求。"""
    global _update_info, _update_worker

    def _check():
        global _update_info, _update_checked_at, _update_worker
        try:
            info = _fetch_update_info()
        except Exception:
            logger.exception("更新检查异常")
            info = {"error": "暂时无法检查更新，请稍后重试或打开下载页。",
                    "error_code": "unavailable", "release_page": _RELEASES + "/latest"}
        with _update_lock:
            _update_info = info
            _update_checked_at = time.monotonic()
            callbacks = list(_update_callbacks)
            _update_callbacks.clear()
            _update_worker = None
        for cb in callbacks:
            _notify_update(cb, info)

    with _update_lock:
        if _update_worker is not None:
            if callback:
                _update_callbacks.append(callback)
            return _update_worker
        ttl = _UPDATE_ERROR_COOLDOWN if _update_info and _update_info.get("error") else _UPDATE_CACHE_SECONDS
        if _update_info is not None and time.monotonic() - _update_checked_at < ttl:
            cached = dict(_update_info)
        else:
            _update_info = None
            if callback:
                _update_callbacks.append(callback)
            _update_worker = threading.Thread(target=_check, daemon=True, name="update-check")
            _update_worker.start()
            return _update_worker
    if callback:
        _notify_update(callback, cached)
    return None


def get_update_info() -> dict | None:
    """返回最新检查结果；检查进行中返回 None。"""
    with _update_lock:
        return dict(_update_info) if _update_info is not None else None


def _version_gt(a: str, b: str) -> bool:
    """比较版本号：a > b（支持预发布后缀如 1.2.0-rc1）"""
    import re

    def _parse(v: str) -> tuple:
        # 拆分核心版本和预发布后缀：1.2.0-rc1 → (1,2,0), "rc1"
        m = re.match(r'^(\d+(?:\.\d+)*)[\-._]?(.*)$', str(v).strip())
        if not m:
            return (0, 0, 0, 1, '')
        nums = tuple(int(x) for x in m.group(1).split('.'))
        # 归一化到 3 段，避免不同长度元组比较时预发布位错位
        while len(nums) < 3:
            nums = nums + (0,)
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
    import requests
    try:
        # 检测代理
        proxy_url = _get_proxy_url()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        response = requests.get(
            url,
            headers={"User-Agent": "vocab-harvester"},
            timeout=120,
            proxies=proxies,
            verify=False,  # 跳过 SSL 验证
            stream=True,  # 流式下载
        )
        response.raise_for_status()

        total = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 1024 * 256  # 256KB chunks

        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total)

        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return False
