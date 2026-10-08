# 项目上下文索引

用途：桌面社交平台数据采集、评论导出和词库分析系统。
范围：仅索引已查阅的桌面启动、采集结果、定时任务和发布更新部分；不代表全仓库审查。
仓库根目录为本文件所在的 `source`，不是上一级工作区；GitHub 维护分支为 `main`。

| 任务 | 入口与依赖 | 验证 |
| --- | --- | --- |
| 桌面白屏与日志 | `app.py`；`src/common/desktop_runtime.py`；`static/index.html` 的 Windows 样式和导航 | `tests/test_desktop_runtime.py`；`tests/manual/check_desktop_navigation.py` |
| 定时执行失败 | `src/api/main.py` 的 `_execute_scheduled_task`、`_load_scheduled_jobs`、`run_scheduled_task_now`；`src/orchestrator/pipeline.py` | `tests/test_scheduled_execution.py`；`SCHEDULE_FAILURE_FIX.md` |
| 定时运行隔离、进度和取消 | `src/api/main.py`；`src/crawlers/scheduled_run.py`、`progress.py`、`browser_manager.py`；两平台抓取器及 `real_crawler.py` | `tests/test_scheduled_lifecycle.py`；`tests/test_comment_collection.py`；`tests/test_x_session_browser.py`；`tests/manual/check_result_summary.py` |
| 定时保存及去重 | `src/api/main.py` 的 `save_task_results_to_file`；`src/vocabulary/storage.py`；`src/common/database.py` | `tests/test_schedule_save.py`；`tests/test_schedule_dedup.py` |
| X 会话与搜索失败 | `src/crawlers/browser_manager.py`；`src/crawlers/twitter_url.py`；`src/crawlers/real_crawler.py` | `tests/test_x_session.py`；`tests/test_x_session_browser.py`（本地服务+真实 Chromium，支持 `VOCAB_TEST_BROWSER_EXECUTABLE`） |
| 搜索评论与数量 | `src/crawlers/comment_results.py`；`src/api/main.py` 搜索接口；`src/crawlers/twitter_url.py`、`src/crawlers/reddit_crawler.py` | `tests/test_comment_exports.py`；`tests/test_search_quantity.py` |
| 结果汇总界面 | `static/index.html` 的 `renderCrawlResult` | `tests/manual/check_result_summary.py` |
| 更新检查 | `src/common/version.py`；`static/index.html` 的 `manualCheckUpdate` | `tests/test_update_check.py`；`tests/manual/check_update_ui.py` |
| 版本发布 | `VERSION`、`release_notes.md`、`CHANGELOG.md`；`.github/workflows/release.yml`；`build_update_manifest.py` | 标签触发 Windows/macOS 测试及构建，两个安装包与 update.json 上传后公开 |

## 验证与限制

- 在仓库根目录运行 `.venv/bin/python -m pytest tests/ -q -m 'not integration and not interactive'`。
- `tests/manual/` 为可选 Playwright 页面测试脚本；通过 `--executable` 指定可用 Chromium。
- 定时执行回归使用模拟平台数据及临时数据库，不等同真实平台采集或 Windows 实机白屏验证。
- 发布与本机安装是不同动作；修改源码不会替换已安装客户端。发布前核对远端 main，避免覆盖并行修改。
- 具体操作授权以当前用户请求为准；不要从此索引推断发布授权。

## 后续排查

- X 搜索 0 条且页面转圈：见 `work/x-loading-diagnosis-2026-10-08.md`，包含实际网络故障、空结果误判及待确认项；结论以对应日期证据为准。

- 定时任务 40 分钟无进展弹窗：见 `work/scheduled-stall-diagnosis-2026-10-08.md`，涉及重入、跨循环浏览器复用、心跳粒度与取消/部分结果缺陷。
