# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: vocab-harvester 桌面应用"""

import os
import subprocess
import sys
from pathlib import Path

block_cipher = None

# SPECPATH is auto-set by PyInstaller to the directory containing this spec file
try:
    project_dir = SPECPATH
except NameError:
    project_dir = os.path.dirname(os.path.abspath(__file__))

# ── 定位 Playwright Chromium ──
def _find_playwright_browsers():
    """返回需要打包的 Playwright 浏览器目录列表"""
    # 优先从环境变量
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not browsers_path:
        # 默认路径
        browsers_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    if not os.path.isdir(browsers_path):
        print("[WARN] Playwright browsers not found, skipping")
        return []

    datas = []
    for name in os.listdir(browsers_path):
        src = os.path.join(browsers_path, name)
        if os.path.isdir(src) and name.startswith("chromium"):
            dst = os.path.join("playwright_browser", name)
            datas.append((src, dst))
            print(f"  Bundling: {name} -> {dst}")
    return datas

print("[*] Locating Playwright browsers...")
pw_datas = _find_playwright_browsers()

a = Analysis(
    [os.path.join(project_dir, 'app.py')],
    pathex=[project_dir],
    binaries=[],
    datas=[
        # 前端
        (os.path.join(project_dir, 'static', 'index.html'), 'static'),
        # 配置
        (os.path.join(project_dir, 'config', 'settings.yaml'), 'config'),
        # 图标
        (os.path.join(project_dir, 'icon.ico'), '.'),
        # 版本号
        (os.path.join(project_dir, 'VERSION'), '.'),
    ] + pw_datas,
    hiddenimports=[
        # uvicorn 子模块（PyInstaller 常遗漏）
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.protocols.websockets.wsproto_impl',
        # websockets 子模块
        'websockets',
        'websockets.exceptions',
        'websockets.legacy',
        'websockets.legacy.server',
        'websockets.legacy.client',
        'websockets.legacy.protocol',
        'websockets.legacy.handshake',
        'websockets.asyncio',
        'websockets.asyncio.server',
        'websockets.asyncio.client',
        'websockets.asyncio.connection',
        'websockets.frames',
        'websockets.http',
        'websockets.uri',
        'websockets.utils',
        'wsproto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # fastapi
        'fastapi',
        'fastapi.responses',
        # aiosqlite
        'aiosqlite',
        # httpx
        'httpx',
        # pydantic
        'pydantic',
        'pydantic.deprecated.decorator',
        # playwright
        'playwright',
        'playwright.async_api',
        # openai
        'openai',
        # yaml
        'yaml',
        'yaml.cyaml',
        # openpyxl
        'openpyxl',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pytest',
        '_pytest',
        'tests',
        'test_api',
        'test_pipeline',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='vocab-harvester',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=os.path.join(project_dir, 'icon.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='vocab-harvester',
)
