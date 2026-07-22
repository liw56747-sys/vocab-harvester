# v1.6.9 更新日志

## ✨ 重大功能：定时任务接入真实抓取

此前定时任务的抓取流水线使用的是 MockCrawler（生成模拟数据），无法真正抓取 X/Twitter、Reddit 的真实内容。原因在于真实抓取需要登录 Cookie，而 Cookie 此前只存在前端 localStorage，后台定时任务（无前端）拿不到。本次彻底打通：

### 1. Cookie 服务端持久化
- 新增 `platform_cookies` 表（存于 `~/.vocab-harvester/vocab.db`，随更新/重启保留）
- 在「数据抓取」页保存 Twitter/Reddit Cookie 时，**自动同步一份到服务端**，供后台定时任务使用
- 新增接口 `POST /api/platform-cookies`（保存）、`GET /api/platform-cookies`（状态查询）

### 2. 定时任务真实抓取
- 定时任务执行时从数据库读取各平台 Cookie，调用与手动搜索**相同的真实抓取器**（`TwitterCookieFetcher` / `RedditCookieFetcher`）
- 真实抓取在**独立后台线程 + 新事件循环**中执行（Playwright 与主事件循环隔离，复用手动搜索的成熟模式）
- 抓取结果统一映射为 `ParsedPost`，交给 Pipeline 复用「历史去重 → 黑词分析 → 落盘导出」全流程

### 3. 缺 Cookie 优雅降级
- 某平台未配置 Cookie 时**跳过该平台并记录**（写入任务状态 / 日志），其他有 Cookie 的平台正常抓取
- 若所选平台全部缺 Cookie，任务标记为 `failed` 并给出清晰提示：「请在数据抓取页配置并保存 Cookie」

---

## ⚠️ 使用说明

- 定时任务真实抓取的前提：**先在「数据抓取」页配置并保存对应平台的 Cookie**（保存动作会自动同步到服务端）
- 与手动搜索共用同一套 Cookie，配置一次即可
- 真实抓取仍受平台反爬、Cookie 有效期、代理可用性影响，与手动搜索一致

---

## 🔧 技术实现

- `src/common/database.py`：新增 `platform_cookies` 表
- `src/api/main.py`：Cookie 存取助手 + `/api/platform-cookies` 接口；`_execute_scheduled_task` 重写为「读库 Cookie → 后台线程真实抓取 → PrefetchedCrawler 交给 Pipeline」；缺 Cookie 跳过/失败处理
- `src/crawlers/real_crawler.py`（新增）：`crawl_twitter` / `crawl_reddit` 真实抓取封装、dict→ParsedPost 映射、`PrefetchedCrawler`
- `static/index.html`：保存 Cookie 时调用 `syncCookieToServer()` 同步到服务端
- 新增 6 个单元测试（映射 / 时间解析 / 预取爬虫 / Cookie 持久化 / upsert），全套 **67 passed**

---

## 📁 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/common/database.py` | 新增 `platform_cookies` 表 |
| `src/api/main.py` | Cookie 接口与存取助手；定时任务改为真实抓取（后台线程）；缺 Cookie 跳过/失败 |
| `src/crawlers/real_crawler.py` | 新增：真实抓取封装 + ParsedPost 映射 + PrefetchedCrawler |
| `static/index.html` | 保存 Cookie 时同步到服务端 |
| `tests/test_real_crawl.py` | 新增：映射 + Cookie 持久化 6 项测试 |
| `VERSION` | `1.6.8` → `1.6.9` |

---

## 📌 前序版本回顾

- **v1.6.8** — 定时任务历史去重（跨次去重 + 重复标记）
- **v1.6.7** — 修复黑词未写库 + 搜索空引用报错
