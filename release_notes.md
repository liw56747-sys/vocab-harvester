# v1.6.2 更新日志

## ✨ 改进

### macOS 首次启动浏览器组件安装引导增加「跳过」按钮

v1.6.1 新增了 macOS 首次启动自动安装 Chromium 浏览器组件的引导界面，但缺少跳过选项。当 CDN 下载速度较慢时，用户会被长时间阻塞在「正在下载浏览器组件」界面，无法使用应用的其他功能。

本次改进：

- **新增跳过按钮** — 下载过程中显示「下载较慢？跳过，先使用应用」链接，点击后可立即使用应用
- **平台说明提示** — 明确标注「Reddit 不需要此组件，X/Twitter 需要」，帮助用户判断是否需要等待
- **中英文国际化** — 新增 `chromium_skip_hint`、`chromium_skip_link`、`chromium_skip_note` 三个 i18n 词条

---

## 📁 修改文件

| 文件 | 修改内容 |
|---|---|
| `static/index.html` | Chromium 安装引导进度区域新增「跳过」链接 + `skipChromiumInstall()` 函数；新增 3 个 i18n 词条（中英文） |
| `VERSION` | `1.6.1` → `1.6.2` |

---

## 📌 上一版本 (v1.6.1) 回顾

v1.6.1 修复了 X/Twitter 平台因 Playwright `chromium_headless_shell` 缺失导致的抓取报错，主要变更包括：

- 锁定 Playwright 版本为 `==1.60.0`
- CI 同时安装 `chromium` + `chromium-headless-shell`
- 运行时同时安装双组件 + 子进程注入 PYTHONPATH
- macOS 首次启动浏览器安装引导 UI
