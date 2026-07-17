# v1.6.4 更新日志

## 🐛 Bug 修复

### 修复关键词搜索处「加速模式」复选框重复显示

关键词搜索区域内有两个标签完全相同的「加速模式」复选框（`user-block-resources` 和 `block-resources-toggle`），导致 UI 冗余且行为不一致。

**修复：** 移除多余的 `block-resources-toggle` 复选框，统一使用搜索栏内的 `user-block-resources`。

---

## ✨ 新功能

### 用户主页抓取新增排序方式与加速模式

用户主页抓取功能之前只有「每用户条数」和「同时抓取评论」两个选项，本次新增：

- **排序方式切换** — 新增「热门 / 最新」排序切换按钮，**默认选中「最新」**（用户主页天然按时间倒序展示）
- **加速模式** — 新增「加速模式（屏蔽图片/CSS，页面加载更快）」复选框，与关键词搜索保持一致
- **后端适配** — `TwitterUrlFetchRequest` 模型新增 `sort_by` 参数（默认值 `"live"`），前端 API 调用同步传递排序和加速模式参数

---

## 📁 修改文件

| 文件 | 修改内容 |
|---|---|
| `static/index.html` | 删除多余的 `block-resources-toggle`；用户主页区域新增排序切换（默认最新）、加速模式复选框；新增 `setTwitterSortBy()` 函数；`startTwitterFetch()` 传递 `sort_by` 参数并使用新 ID |
| `src/api/main.py` | `TwitterUrlFetchRequest` 新增 `sort_by: str = "live"` 字段 |
| `VERSION` | `1.6.3` → `1.6.4` |
