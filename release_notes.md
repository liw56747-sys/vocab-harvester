# v1.6.7 更新日志

## 🐛 重要问题修复

### 1. 修复黑词提取结果未写入数据库的严重 Bug（自 v1.5.7 起的回归）

**问题：** `VocabManager` 中存在**两个同名的 `ingest` 方法**——第二个（v1.5.7「自动去重功能」提交时引入）覆盖了真正写库的第一个。由于 Python 中后定义的方法会覆盖前者，导致实际生效的 `ingest` **只做去重、从不调用 `storage.upsert()`**。

**后果：** 「抓取数据 + 分析黑词并提取」模式（定时任务、数据导入）提取出的黑词**根本没有写入数据库**，因此：
- 词库管理页面看不到定时任务分析出的黑词
- 重启软件后这些词也不存在（因为压根没落库）

**修复：**
- 删除重复的 `ingest` 方法，恢复真正写库的实现（黑词持久化到 `vocab.db`）
- 将自动去重逻辑（post_id 精确去重 + 内容相似度去重）抽取为独立的 `_dedup_posts()` 方法，并在 `ingest` 开头调用——**去重与黑词入库两个能力现在同时正常工作**
- 验证：分析模式定时任务执行后，词库总数从 0 增至 20，确认黑词已持久化；`vocab.db` 为持久化文件，重启后保留

### 2. 修复关键词搜索报错：`null is not an object ('block-resources-toggle')`

**问题：** v1.6.4 删除了重复的「加速模式」复选框 `block-resources-toggle`，但搜索相关 JS 仍引用该已删除元素，导致点击「开始搜索」时抛出 `null is not an object (evaluating 'document.getElementById('block-resources-toggle').checked')`，搜索直接失败。

**修复：** 将单条搜索、批量搜索、结果渲染中 3 处对 `block-resources-toggle` 的引用统一改为现存的关键词搜索加速模式复选框 `user-block-resources`。

---

## 🔧 技术实现

- `src/vocabulary/manager.py`：移除覆盖性重复 `ingest`；新增 `_dedup_posts()`；`ingest` 开头执行去重后再逐条 `storage.upsert()` 写库
- `static/index.html`：3 处 `getElementById('block-resources-toggle')` → `getElementById('user-block-resources')`
- `tests/test_dedup.py`：去重测试改为直接校验 `_dedup_posts()`（原测试针对已废弃的旧 ingest 行为）
- 全套测试 **57 passed**

---

## 📁 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/vocabulary/manager.py` | 删除重复 `ingest`（黑词未写库根因）；新增 `_dedup_posts()`；恢复黑词持久化 |
| `static/index.html` | 修复搜索报错：`block-resources-toggle` → `user-block-resources`（3 处） |
| `tests/test_dedup.py` | 去重测试改为校验 `_dedup_posts()` |
| `VERSION` | `1.6.6` → `1.6.7` |

---

## 📌 前序版本回顾

- **v1.6.6** — 定时任务「仅抓取数据」完整导出抓取结果 + 可选 CSV/xlsx 格式
- **v1.6.5** — 定时任务新增「执行目标」（仅抓取 / 抓取+分析）+ 界面深度美工改造
