# 项目上下文索引

用途：桌面社交平台数据采集、评论导出和词库分析系统。
范围：仅索引已查阅的桌面启动、采集结果、定时任务和发布更新部分；不代表全仓库审查。
仓库根目录为本文件所在的 `source`，不是上一级工作区；GitHub 维护分支为 `main`。

| 任务 | 入口与依赖 | 验证 |
| --- | --- | --- |
| 桌面白屏与日志 | `app.py`；`src/common/desktop_runtime.py`；`static/index.html` 的 Windows 样式和导航 | `tests/test_desktop_runtime.py`；`tests/manual/check_desktop_navigation.py` |
| 定时执行失败 | `src/api/main.py` 的 `_execute_scheduled_task`、`_load_scheduled_jobs`、`run_scheduled_task_now`；`src/orchestrator/pipeline.py` | `tests/test_scheduled_execution.py`；`SCHEDULE_FAILURE_FIX.md` |
| 定时运行隔离、进度和取消 | `src/api/main.py`；`src/crawlers/scheduled_run.py`、`progress.py`、`browser_manager.py`；两平台抓取器及 `real_crawler.py` | `tests/test_scheduled_lifecycle.py`；`tests/test_comment_collection.py`；`tests/test_x_session_browser.py`；`tests/manual/check_result_summary.py` |
| 定时保存及去重 | `src/api/main.py` 的 `save_task_results_to_file` / `generate_export_filename`（任务名＋完成月日，同名加序号）；`src/collection/store.py` 的 `export_names` 固定历史下载名称；`src/vocabulary/storage.py`；`src/common/database.py` | `tests/test_schedule_save.py`；`tests/test_schedule_dedup.py` |
| X 会话与搜索失败 | `src/crawlers/browser_manager.py`；`src/crawlers/twitter_url.py`；`src/crawlers/real_crawler.py` | `tests/test_x_session.py`；`tests/test_x_session_browser.py`（本地服务+真实 Chromium，支持 `VOCAB_TEST_BROWSER_EXECUTABLE`） |
| Reddit 403 与打包 TLS | `src/common/network_runtime.py`、`reddit_diagnostics.py`；`.python-version`；发布工作流；`engine.py` 实时错误与访问拒绝暂停 | `tests/test_reddit_runtime.py`；`work/reddit-access-2026-10-23/README.md`；冻结程序只读搜索验证 |
| 评论效率、分轮队列与增量保存 | `src/collection/comment_queue.py`、`comment_session.py`；`store.py` 的 RowIndex/增量表；两平台评论入口 | `tests/test_comment_queue.py`；`tests/test_comment_efficiency.py`；`work/comment-queue-2026-10-26/README.md` |
| 评论补抓故障与进度 | `src/crawlers/comment_state.py`；两平台评论入口；`src/collection/store.py` 的 `run_progress`；`src/collection/api.py` 的 `summary`；历史记录轮询 | `tests/test_comment_reliability.py`；`tests/stress/test_comment_recovery_pressure.py`；`tests/manual/check_comment_recovery_ui.py`；`work/comment-recovery-2026-10-16.md` |
| 仅图片采集、下载与导出 | `src/collection/images.py`；`engine.py` 图片模式；`api.py` 导出和只补图片；两平台 `image_urls` | `tests/test_image_collection.py`；`tests/stress/test_image_pressure.py`；`tests/manual/check_image_collection_ui.py`；`work/image-collection-plan-2026-10-21.md` |
| 图片轻量遍历、同步下载与跨任务平台公平调度 | `engine.py` 的平台任务/Traffic 等待队列和图片流水线；`images.py` 的 image_records；两平台图片评论分支；`main.py` 等待心跳 | `tests/test_image_pipeline.py`；`tests/manual/check_comment_queue_browser.py`；`tests/manual/benchmark_image_pipeline.py`；`work/image-pipeline-2026-10-27/README.md`（v1.9.4） |
| 关键词独立目标与升级 | `src/collection/engine.py` 默认 `per_keyword`；`src/collection/migrations.py` 一次性升级旧搜索定时任务；`static/index.html` 目标公式预览 | `tests/test_keyword_target_migration.py`；`tests/test_collection_recovery.py` 的 6×2×50 场景；`tests/manual/check_collection_controls.py` |
| 搜索评论与数量 | `src/crawlers/comment_results.py`；`src/api/main.py` 搜索接口；`src/crawlers/twitter_url.py`、`src/crawlers/reddit_crawler.py` | `tests/test_comment_exports.py`；`tests/test_search_quantity.py` |
| 工作台完整交互与分阶段采集 | `static/index.html` 的 `setupWorkspace`、`setupGuidedFlows`、`setupLibraryWorkflows`；`src/collection/engine.py` posts/comments/images 阶段；`api.py` owner 历史筛选 | `tests/manual/check_workspace_design.py`、`check_workspace_flows.py`；`tests/test_collection_recovery.py`；`work/workspace-redesign-2026-10-22.md` |
| 结果汇总界面 | `static/index.html` 的 `renderCrawlResult` | `tests/manual/check_result_summary.py` |
| 更新检查 | `src/common/version.py`；`static/index.html` 的 `manualCheckUpdate` | `tests/test_update_check.py`；`tests/manual/check_update_ui.py` |
| 采集持久化与恢复 | `src/collection/engine.py`、`store.py`、`api.py`；`src/api/main.py` 各采集入口 | `tests/test_collection_recovery.py`；`tests/test_collection_api.py`；`tests/manual/check_collection_controls.py` |
| 定时规则与漏跑 | `src/collection/schedule.py`、`scheduler.py`；`static/index.html` 定时选择器与预览 | `tests/test_schedule_calendar.py`；Windows 显式依赖 tzdata |
| 审核解耦与预留接口 | `src/adapter/base.py`；`src/api/main.py` 的 `/api/audit/*`；`work/audit-interface-2026-10-15.md` | `tests/test_audit_interface.py`；旧任务兼容与页面回归 |
| 发布压测与安装包自检 | `tests/stress/test_release_pressure.py`；`src/common/self_test.py`；`app.py --self-test REPORT.json` | 压测结果与边界见 `work/release-pressure-2026-10-15.md` |
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

- P0/P1 实现、使用边界与下一阶段图文审核设计：`work/p0-p1-collection-and-multimodal-2026-10-15.md`。图文联合审核部分为方案，尚未实现。
