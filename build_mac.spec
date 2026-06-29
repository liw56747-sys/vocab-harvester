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

# ── Locate Playwright Chromium (macOS) ──
def _find_playwright_browsers():
    """Return Playwright browser directories to bundle"""
    # Prefer environment variable
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not browsers_path:
        # macOS default: ~/Library/Caches/ms-playwright
        browsers_path = os.path.join(
            str(Path.home()), "Library", "Caches", "ms-playwright"
        )
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
    strip=True,
    upx=True,
    upx_exclude=[],
    name='vocab-harvester',
)
