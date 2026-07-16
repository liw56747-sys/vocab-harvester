# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: vocab-harvester macOS desktop app"""

import os
import sys
from pathlib import Path

block_cipher = None

# SPECPATH is auto-set by PyInstaller to the directory containing this spec file
try:
    project_dir = SPECPATH
except NameError:
    project_dir = os.path.dirname(os.path.abspath(__file__))

# ── Playwright browsers: bundle headless shell only on macOS ──
# Chromium's full .app has deeply nested frameworks that break PyInstaller's codesign.
# However, chromium_headless_shell is a plain binary (not .app) and is safe to bundle.
# The crawler only uses headless=True mode, which requires chromium_headless_shell.
def _find_headless_shell():
    """Locate chromium_headless_shell for bundling (macOS only)"""
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not browsers_path:
        browsers_path = os.path.join(os.path.expanduser("~/Library/Caches"), "ms-playwright")
    if not os.path.isdir(browsers_path):
        print(f"[WARN] Playwright browsers path not found: {browsers_path}")
        print("[INFO] Headless shell will be downloaded at runtime")
        return []
    datas = []
    for name in os.listdir(browsers_path):
        src = os.path.join(browsers_path, name)
        if os.path.isdir(src) and name.startswith("chromium_headless_shell"):
            dst = os.path.join("playwright_browser", name)
            datas.append((src, dst))
            print(f"  Bundling headless shell: {name} -> {dst}")
    if not datas:
        print("[WARN] chromium_headless_shell not found, will install at runtime")
    return datas

print("[*] Locating Playwright headless shell for macOS bundle...")
pw_datas = _find_headless_shell()

a = Analysis(
    [os.path.join(project_dir, 'app.py')],
    pathex=[project_dir],
    binaries=[],
    datas=[
        # Frontend
        (os.path.join(project_dir, 'static', 'index.html'), 'static'),
        (os.path.join(project_dir, 'static', 'vendor'), 'static/vendor'),
        # Config
        (os.path.join(project_dir, 'config', 'settings.yaml'), 'config'),
        # Icon (macOS .icns)
        (os.path.join(project_dir, 'icon.icns'), '.'),
        # Version
        (os.path.join(project_dir, 'VERSION'), '.'),
        # GitHub Token (.env file, if exists)
    ] + ([
        (os.path.join(project_dir, '.env'), '.'),
    ] if os.path.exists(os.path.join(project_dir, '.env')) else []) + pw_datas,
    hiddenimports=[
        # uvicorn submodules (PyInstaller often misses these)
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
        # websockets submodules
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
        # apscheduler（定时任务）
        'apscheduler',
        'apscheduler.schedulers',
        'apscheduler.schedulers.asyncio',
        'apscheduler.triggers',
        'apscheduler.triggers.cron',
        'apscheduler.triggers.interval',
        'apscheduler.triggers.date',
        'apscheduler.jobstores',
        'apscheduler.jobstores.memory',
        'apscheduler.executors',
        'apscheduler.executors.asyncio',
        # pywebview（桌面窗口）
        'webview',
        'webview.platforms',
        # 项目自身模块（PyInstaller 有时漏掉）
        'src',
        'src.api',
        'src.api.main',
        'src.api.routes',
        'src.common',
        'src.common.config',
        'src.common.database',
        'src.common.models',
        'src.common.version',
        'src.adapter',
        'src.adapter.base',
        'src.adapter.mock',
        'src.adapter.xingpan',
        'src.crawlers',
        'src.crawlers.base',
        'src.crawlers.browser_manager',
        'src.crawlers.mock',
        'src.crawlers.platform_config',
        'src.crawlers.reddit_crawler',
        'src.crawlers.twitter_url',
        'src.orchestrator',
        'src.orchestrator.pipeline',
        'src.orchestrator.scheduler',
        'src.orchestrator.job_queue',
        'src.vocabulary',
        'src.vocabulary.manager',
        'src.vocabulary.storage',
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
    strip=True,
    upx=True,
    console=False,
    icon=os.path.join(project_dir, 'icon.icns'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='vocab-harvester',
)

app = BUNDLE(
    coll,
    name='vocab-harvester.app',
    icon=os.path.join(project_dir, 'icon.icns'),
    bundle_identifier='com.vocabharvester.app',
)
