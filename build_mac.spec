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

# ── Playwright browsers are NOT bundled on macOS ──
# Chromium's .app has deeply nested frameworks that break PyInstaller's codesign.
# Instead, the app runs `playwright install chromium` on first launch.
# This keeps the PyInstaller output clean and avoids signing errors.
pw_datas = []
print("[*] Playwright browsers: will be installed at runtime (not bundled)")

a = Analysis(
    [os.path.join(project_dir, 'app.py')],
    pathex=[project_dir],
    binaries=[],
    datas=[
        # Frontend
        (os.path.join(project_dir, 'static', 'index.html'), 'static'),
        # Config
        (os.path.join(project_dir, 'config', 'settings.yaml'), 'config'),
        # Icon (macOS .icns)
        (os.path.join(project_dir, 'icon.icns'), '.'),
        # Version
        (os.path.join(project_dir, 'VERSION'), '.'),
    ] + pw_datas,
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
        'websockets.legacy',
        'websockets.legacy.server',
        'websockets.legacy.client',
        'websockets.legacy.protocol',
        'websockets.legacy.handshake',
        'websockets.asyncio',
        'websockets.asyncio.server',
        'websockets.asyncio.client',
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
