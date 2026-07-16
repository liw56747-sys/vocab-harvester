# v1.6.1 更新日志

## 🐛 问题修复

### 修复 X/Twitter 平台抓取报错：`Executable doesn't exist at .../chromium_headless_shell-XXXX`

**根本原因：** v1.6.0 CI 构建时 `pip install playwright` 安装了最新版 1.61.0（需要 `chromium_headless_shell-1228`），而用户系统 Playwright 为 1.60.0（只会安装 `chromium_headless_shell-1223`），版本不匹配导致 Playwright headless 模式找不到正确的浏览器可执行文件。

**修复内容：**

- **锁定 Playwright 版本** — `requirements.txt` 中 `playwright>=1.40.0` → `playwright==1.60.0`，确保 CI 构建环境与用户本地环境版本一致，消除版本漂移风险
- **CI 补装 headless-shell** — `.github/workflows/release.yml` 中 macOS 和 Windows 构建步骤追加 `playwright install chromium-headless-shell`，确保 CI 测试能通过（Playwright headless 模式同时需要 `chromium` 完整浏览器和 `chromium-headless-shell` 两个组件）
- **运行时同时安装双组件** — `src/api/main.py` 中首次启动安装逻辑改为依次安装 `chromium` 和 `chromium-headless-shell`，两者都成功才算安装完成
- **运行时使用打包内 Playwright** — 安装子进程注入 `PYTHONPATH` 指向 app 内的 `_MEIPASS` 目录，确保子进程加载打包在 app 内的 Playwright 模块（而非用户系统版本），从根源上避免版本不一致
- **启动检测增强** — `app.py` 中 macOS Chromium 检测从「是否存在任意 `chromium*` 目录」改为同时检查 `chromium-` 和 `chromium_headless_shell-` 两个目录，缺少任一即触发安装

### 修复 Reddit 不受影响的原因说明

Reddit 爬虫使用 `httpx` + Reddit JSON API 纯 HTTP 请求方式抓取数据，不依赖 Playwright 浏览器；而 X/Twitter 爬虫需要通过 Playwright 启动 Chromium 浏览器渲染页面并滚动加载数据，因此浏览器组件缺失只会影响 X 平台。

---

## ✨ 新功能

### 首次启动浏览器组件安装引导（macOS）

macOS 版 .app 出于代码签名兼容性不打包 Chromium 浏览器，首次启动时需自动安装。本次新增了完整的安装引导 UI：

- **自动触发** — 页面加载时调用 `/api/chromium-status` 检测浏览器状态，如需安装则自动显示全屏遮罩并触发 `/api/install-chromium`
- **实时进度** — 轮询后端安装状态，显示当前正在下载的组件名称（`chromium` / `chromium-headless-shell`）
- **成功动画** — 安装完成后显示 ✓ 动画，2 秒后自动切换
- **安装后更新提示** — 安装完成后弹出提示框，引导用户通过「一键更新」重新下载最新版安装包覆盖安装，确保所有功能正常工作。提供两个选项：
  - 「跳过，直接使用」— 关闭遮罩继续使用
  - 「重新安装应用」— 自动触发一键更新流程
- **安装失败兜底** — 若自动安装失败，显示完整的终端命令供用户手动执行，命令已更新为同时安装两个浏览器组件
- **中英文国际化** — 所有新增文案均支持中英文切换

---

## 📁 修改文件

| 文件 | 修改内容 |
|---|---|
| `requirements.txt` | 锁定 Playwright 版本为 `==1.60.0` |
| `.github/workflows/release.yml` | CI 安装 `chromium` 后追加安装 `chromium-headless-shell`；移除多余的 `pip install playwright`（改用 requirements.txt 中的锁定版本） |
| `app.py` | macOS Chromium 检测改为同时检查 `chromium-` 和 `chromium_headless_shell-` 目录 |
| `src/api/main.py` | 运行时安装改为依次安装 `chromium` + `chromium-headless-shell`；子进程注入 `PYTHONPATH` 使用打包内的 Playwright |
| `static/index.html` | 新增 Chromium 安装引导 JS（自动触发/轮询进度/成功失败处理）；新增安装后更新提示 UI；新增 i18n 词条（中英文）；更新错误提示中的手动安装命令 |
| `VERSION` | `1.6.0` → `1.6.1` |
