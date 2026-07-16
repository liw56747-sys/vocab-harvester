## v1.6.0 更新日志

### 🔧 Bug 修复

#### 1. 修复 macOS 版本启动闪退
- 修复了 `os.listdir()` 在 Playwright 浏览器路径不存在或无权限时导致的应用崩溃
- 补全了 PyInstaller 打包时遗漏的关键模块：
  - `src.orchestrator.job_queue`（定时任务队列）
  - `apscheduler` 及其子模块（定时任务调度器）
  - `webview` 及平台模块（桌面窗口）
- 在 `app.py` 全局强制导入所有核心模块，确保 PyInstaller 静态分析能识别全部依赖

#### 2. 修复 X 平台关键词搜索浏览器路径错误
- 修复了 CI 中 macOS 构建步骤使用了错误的 Windows 环境变量语法（`%LOCALAPPDATA%`）
- macOS 不打包 Chromium 进 .app（codesign 兼容性问题），改为运行时通过 API 端点安装

#### 3. 修复定时任务自动分析功能
- 定时任务现在会加载用户在界面配置的模型 API（从数据库读取），使用真实的 LLM 进行自动分析
- 之前定时任务只使用全局默认配置（可能是 Mock 适配器），导致分析功能无法正常工作
- 应用启动时自动恢复已配置的定时任务（cron 调度器在 lifespan 初始化时加载）

#### 4. 修复 GitHub Release 命名不规范问题
- 新的 Release 名称格式：`Release v1.6.0 - [摘要]`
- 自动过滤无意义的 commit message（如 "update version"、"bump version" 等），替换为 "版本发布"

### 📦 安装包

| 平台 | 文件 | 说明 |
|------|------|------|
| macOS | vocab-harvester-1.6.0.dmg | 双击打开，拖入 Applications |
| Windows | vocab-harvester-1.6.0-setup.exe | 双击安装向导 |

> 如果下载速度慢，可以尝试使用代理或镜像加速。
