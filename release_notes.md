## v1.5.7 更新日志

### 🎯 新功能：抓取数据自动去重

#### 核心实现
- **双层去重机制**：
  1. 优先使用 `post_id` 精确去重
  2. 无 `post_id` 时通过内容相似度去重
- **维度控制**：
  - 按关键词去重：保留最近30天历史数据
  - 按用户主页去重：保留最近90天历史数据
- **智能过滤**：
  - 同一关键词/用户主页内容自动过滤
  - 不同关键词可抓取相同内容
- **配置灵活**：
  - 通过 `config/settings.yaml` 控制去重开关
  - 动态调整保留周期

#### 数据库变更
- `posts` 表新增字段：
  - `post_id` (TEXT UNIQUE)
  - `keywords` (TEXT)
  - `user_id` (TEXT)
- 新增索引：
  - `idx_posts_keywords`
  - `idx_posts_user_id`
  - `idx_posts_created_at`

#### 安装包

| 平台 | 文件 | 说明 |
|------|------|------|
| macOS | vocab-harvester-1.5.7.dmg | 双击打开，拖入 Applications |
| Windows | vocab-harvester-1.5.7-setup.exe | 双击安装向导 |

> 如果下载速度慢，可以尝试使用代理或镜像加速。