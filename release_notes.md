# v1.6.3 更新日志

## ✨ 核心改进

### macOS 安装包内置 Chromium Headless Shell，X/Twitter 开箱即用

之前版本的 macOS .app 不打包任何浏览器组件（Chromium 完整浏览器的 .app 包会破坏 PyInstaller 代码签名），用户首次启动时需从 CDN 下载约 92 MB 的 headless shell 组件，CDN 速度慢时会导致长时间卡在「正在下载浏览器组件」界面。

**本次改进：**

- **打包 headless shell 进 .app** — `build_mac.spec` 新增 `_find_headless_shell()` 函数，CI 构建时将 `chromium_headless_shell`（190 MB，纯二进制文件，无 .app 包结构）打包进安装目录，不影响代码签名
- **开箱即用** — macOS 用户安装后 X/Twitter 抓取功能直接可用，无需等待下载
- **精简运行时安装** — `src/api/main.py` 中运行时安装组件从 `chromium` + `chromium-headless-shell`（431 MB）精简为仅 `chromium-headless-shell`（92 MB），作为兜底方案
- **启动检测优化** — `app.py` 中优先检测打包内的 headless shell，找到则直接使用，不再触发安装引导

**验证结果：** 通过模拟 macOS .app 打包目录结构验证，Playwright 可正确从 `_MEIPASS/playwright_browser/chromium_headless_shell-1223/` 路径启动 headless 浏览器并加载网页。

---

## 📁 修改文件

| 文件 | 修改内容 |
|---|---|
| `build_mac.spec` | 新增 `_find_headless_shell()` 函数，将 `chromium_headless_shell-*` 目录打包进 `playwright_browser/` |
| `app.py` | 浏览器检测逻辑重构：优先检测打包内 headless shell → macOS 用户缓存兜底 → 触发安装引导 |
| `src/api/main.py` | 运行时安装组件精简为仅 `chromium-headless-shell`；错误提示命令同步更新 |
| `VERSION` | `1.6.2` → `1.6.3` |

---

## 📌 前序版本回顾

- **v1.6.2** — macOS 首次启动安装引导增加「跳过」按钮
- **v1.6.1** — 修复 X/Twitter 平台 Playwright headless-shell 缺失导致的抓取报错
