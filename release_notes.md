# v1.7.3 更新日志

本次一次性处理三件事：定时任务导出评论、去重后补足到设定条数、Windows 稳定性加固。

## ✨ 1. 定时任务导出评论（单独成行）
此前定时任务勾选"同时抓取评论"后，导出表格里看不到评论内容——评论在"抓取→转换→落盘"链路中被丢弃了。现已修复：
- 抓到的评论作为**独立行**写入表格，新增 `type` 列区分「帖子/评论」，评论行以 `parent_id` 关联所属帖子；
- Twitter 回复、Reddit 评论均正确导出，与手动搜索导出保持一致；
- 分析模式（抓取+分析）也导出完整明细，不再受 50 条采样限制。

## ✨ 2. 历史重复：丢弃并严格补足到设定条数
此前重复数据仅被标记（`duplicate` 列）仍展示。现改为：
- **直接丢弃**历史重复数据，不再展示，移除 `duplicate` 列；
- **严格迭代补足**：去掉重复后自动继续抓取，补足到任务设定的条数；
- 当平台**已无更多新结果**时，直接输出当前已抓到的结果（不无限重试）。

## 🛡️ 3. Windows 客户端稳定性加固
针对交互过程中"无响应/崩溃"，做代码层最佳缓解：
- **关闭界面 WebView2 的 GPU 硬件加速**（仅 Windows）——规避显卡驱动/GPU 合成导致的原生崩溃；本软件为表单+表格界面，不需要 GPU 加速，体验基本无差别；
- 评论抓取并发标签页 5 → 3，降低内存/标签压力；
- 定时抓取改用专用单线程执行，降低 Chromium 子进程在池化线程中运行的不稳定性；
- 打包补回 `real_crawler` 模块（保证 Windows 定时任务真实抓取可用）。

---

## 🔧 技术实现
- `src/crawlers/real_crawler.py`：评论映射为独立 ParsedPost（`raw_data.type="comment"` + `parent_id`）；新增 `seen_ids` 去重 + 迭代补足（最多 4 轮 / 单词上限 300 条兜底，无更多新结果即停）。
- `src/vocabulary/storage.py`：新增 `get_seen_post_ids()`（主线程一次性取全量已见 id 传入抓取层）。
- `src/api/main.py`：`_execute_scheduled_task` 抓取前载入 seen_ids、抓取后记录新帖 id、`dedup_ctx=None`、专用单线程 executor；`save_task_results_to_file` 增 `type/parent_id` 列、移除 `duplicate` 列。
- `src/orchestrator/pipeline.py`：`crawled_posts`/`sampled_posts` 增加 `type/parent_id`，两种模式都输出完整明细。
- `app.py`：Windows 下设置 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--disable-gpu`。
- 测试：新增 real_crawler 去重/补足/评论映射单测 + 更新落盘导出测试，全套 **73 passed**。

---

## 📁 修改文件
| 文件 | 修改内容 |
|---|---|
| `src/crawlers/real_crawler.py` | 评论单独成行、seen_ids 去重 + 严格迭代补足 |
| `src/vocabulary/storage.py` | 新增 `get_seen_post_ids()` |
| `src/api/main.py` | 定时任务去重/补足接线、导出列调整（type/parent_id，移除 duplicate） |
| `src/orchestrator/pipeline.py` | crawled_posts/sampled_posts 增 type/parent_id |
| `src/crawlers/twitter_url.py` | 评论并发标签页 5→3 |
| `app.py` | Windows 关闭 WebView2 GPU 加速 |
| `build.spec` / `build_mac.spec` | hiddenimports 补回 real_crawler |
| `VERSION` | `1.7.2` → `1.7.3` |

---

## 📌 前序版本回顾
- **v1.7.2** — 定时任务完成弹窗提示
- **v1.7.1** — 手动搜索自动同步 Cookie 到服务端
