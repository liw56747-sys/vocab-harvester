# 桌面图标：汇聚 / Gather

2026-10-23。深翡翠底座与象牙白、薄荷绿折叠 V 构成主体；暖金色数据块代表进入采集流程的信息。V 同时对应 Vocab，折叠汇聚表达文本、评论和图片的归集。图标不使用细小文字。

- `app-icon.svg`：可编辑矢量源，透明外围、圆角底座。
- `app-icon-1024.png`：1024 像素 RGBA 母版。
- `app-icon-preview.png`：512 像素预览。
- `app-icon-presentation.png`：深浅背景和 16/32/48/64/128 像素展示。
- 仓库根目录 `icon.ico`：16/20/24/32/40/48/64/96/128/256 像素，现有 Windows PyInstaller 和 Inno Setup 直接使用。
- 仓库根目录 `icon.icns` 与 `icon_macos.png`：macOS 应用、DMG 卷标以及发布流水线的图标生成源。

开发环境中运行 `.venv/bin/python assets/branding/build_icons.py` 可从 SVG 重新生成所有资源，需要 Pillow、Playwright 与 Chromium。运行客户端不增加依赖。

已检查透明边缘、ICO 全部尺寸解码、ICNS 解码以及深浅背景预览。此次只修改品牌资源，不改变采集和调度行为。安装后的桌面图标需随新安装包更新；此资源修改本身不会替换已安装 v1.9.1 的图标。
