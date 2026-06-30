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
    """读取项目版本号（从 VERSION 文件）"""
    version_file = Path(__file__).parent.parent.parent / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
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
_update_info: dict | None = None


def check_for_update_async(callback=None):
    """后台线程检查 GitHub 最新版本，完成后调用 callback(info)"""
    def _check():
        global _update_info
        try:
            req = urllib.request.Request(
                _GITHUB_API,
                headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "vocab-harvester"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
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

        except Exception as e:
            logger.debug(f"Update check failed: {e}")
            _update_info = {"error": str(e)}

        if callback and _update_info:
            try:
                callback(_update_info)
            except Exception:
                pass

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    return t


def get_update_info() -> dict | None:
    """获取更新检查结果（可能在后台线程尚未完成时返回 None）"""
    return _update_info


def _version_gt(a: str, b: str) -> bool:
    """比较版本号：a > b"""
    try:
        pa = tuple(int(x) for x in a.split("."))
        pb = tuple(int(x) for x in b.split("."))
        return pa > pb
    except (ValueError, AttributeError):
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
