"""一键构建 vocab-harvester 桌面应用"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 避免 GBK 编码错误
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).parent
DIST_DIR = ROOT_DIR / "dist" / "vocab-harvester"


def check_dependencies():
    """检查构建依赖"""
    missing = []
    for pkg in ["PyInstaller", "webview"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"[错误] 缺少构建依赖: {', '.join(missing)}")
        print("请运行: pip install pywebview pyinstaller")
        return False
    return True


def check_playwright():
    """检查 Playwright Chromium 是否已安装"""
    browsers_path = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
    )
    chromium_dirs = [
        d for d in os.listdir(browsers_path)
        if os.path.isdir(os.path.join(browsers_path, d)) and d.startswith("chromium-")
    ] if os.path.isdir(browsers_path) else []

    if not chromium_dirs:
        print("[*] Playwright Chromium 未安装，正在安装...")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    else:
        print(f"[OK] Playwright Chromium: {', '.join(chromium_dirs)}")


def get_dir_size(path: Path) -> str:
    """计算目录大小"""
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    if total > 1024 * 1024 * 1024:
        return f"{total / (1024**3):.1f} GB"
    return f"{total / (1024**2):.1f} MB"


def build():
    print("=" * 60)
    print("  vocab-harvester 构建桌面应用")
    print("=" * 60)

    if not check_dependencies():
        sys.exit(1)

    print("\n[1/4] 检查 Playwright 浏览器...")
    check_playwright()

    print("\n[2/4] 清理旧构建...")
    build_dir = ROOT_DIR / "build"
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    if build_dir.exists():
        shutil.rmtree(build_dir)

    print("\n[3/4] 运行 PyInstaller...")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(ROOT_DIR / "build.spec"), "--noconfirm"],
        cwd=str(ROOT_DIR),
    )
    if result.returncode != 0:
        print("\n[错误] PyInstaller 构建失败")
        sys.exit(1)

    print("\n[4/4] 构建完成！")
    print(f"\n{'=' * 60}")
    print(f"  输出目录: {DIST_DIR}")
    print(f"  目录大小: {get_dir_size(DIST_DIR)}")
    print(f"  启动程序: {DIST_DIR / 'vocab-harvester.exe'}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    build()
