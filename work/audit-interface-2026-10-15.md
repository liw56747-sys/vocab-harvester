# 审核解耦与接入契约

日期：2026-10-15。状态：本地源码已修改，未发布或替换已安装客户端。

## 当前行为

- 移除内置 Xingpan/LLM 提示词、模型调用、备用模型和模拟审核实现；移除对应依赖和打包导入。
- 原“数据导入”页改为“审核接口”状态页，移除文件导入审核、分析上次抓取、模型配置、工作流选择与舆情规则输入。
- 手动和定时采集只采集、保存、导出。旧任务即使带有 `analyze=true`、旧工作流或已保存模型密钥，也不会启动审核。重新保存任务时将 `analyze` 置为 false 并清空旧工作流字段。
- 保留已有采集记录、词库、人工批准/拒绝和导出；不迁移或删除用户数据库。旧模型配置表和旧任务字段仅为兼容保留，不再读取或公开密钥。
- 词库分类筛选从已有词条读取，不再依赖旧审核工作流的硬编码分类。

## HTTP 接口

| 入口 | 行为 |
| --- | --- |
| `GET /api/audit/status` | `configured` 表示是否已注入外部实现；`contract_version="1"`；`automatic_audit=false`。这不是外部服务健康检查。 |
| `POST /api/audit/submit` | 显式提交数据，202 返回外部任务 ID；未接入时 503、`audit_not_configured`。 |
| `GET /api/audit/tasks/{task_id}` | 未完成返回 `pending`，完成返回 `completed` 和标准结果；未接入时 503。任务生命周期与未知 ID 的处理由适配器负责。 |
| 旧 `/api/model-config`、`/api/analyze-last`、`/api/import` | 返回 410、`legacy_audit_removed`；不读取旧配置、不上传文件审核、不生成候选词。 |

提交示例：

```json
{
  "posts": [{
    "platform": "twitter",
    "post_id": "post-123",
    "content": "待审核文本",
    "author": "example",
    "published_at": "2026-10-15T08:00:00+08:00",
    "raw_data": {
      "type": "post",
      "parent_id": "",
      "media_urls": ["https://example.test/image.jpg"]
    }
  }],
  "context": {"policy_version": "future-v1"}
}
```

帖子结构沿用 `ParsedPost`，`posts` 不可为空；`raw_data` 保留父帖、媒体引用等元数据，`context` 传递外部规则版本等可选上下文。接口不会下载媒体或把图片 URL 当作已完成图片审核。

完成结果沿用 `WorkflowResult`：包含 `task_id`、`keywords`、`source_posts`、`metadata`。候选词结构保留现有来源、证据、类别、风险分与建议动作字段，以兼容已有词库。当前结果协议尚未定义图片候选规则，不代表已经支持拦截图入库。

## 实现接入方式

实现 `src/adapter/base.py` 的 `WorkflowAdapter`：

- `submit(posts, *, context=None) -> task_id`
- `poll_result(task_id) -> WorkflowResult | None`
- 可选 `receive_callback(payload)`；当前未开放 HTTP 回调端点。
- `submit_and_wait` 为有超时限制的便捷方法。

接入代码显式设置 `src.api.main.app.state.audit_adapter = 外部适配器实例`。当前仓库不附带外部实现、远端地址或模型配置，也不从历史配置自动创建实例。后续需要确定外部服务协议后，再实现鉴权、上传、超时重试、持久化、回调验签和幂等。

HTTP 提交及轮询接口只转交审核请求与结果，**不会自动写入词库**。现有 `Pipeline(adapter=...)` 的显式 `analyze=True` / `process_posts` 是程序级接入能力，只有调用者主动注入实现才可使用；应用界面、采集和定时任务均不调用它。后续自动化联动及候选词/候选图库的验收、批准和落库需单独接入。

## 验证

- 完整回归：233 项通过，8 项独立服务/交互测试未执行。
- 新增契约回归：未接入状态、503、旧入口 410、空输入校验、上下文/媒体引用透传、轮询、等待超时、结果不自动入库。
- 旧审核任务兼容回归：采集与导出成功、不生成模拟候选。
- 两个真实 Chromium 脚本通过：审核状态导航、旧任务编辑保存、历史分类筛选、P0/P1 控件及三种采集流程的汇总和下载；无 JavaScript 错误。
- 未构建新安装包，未做 Win11 实机或真实外部审核服务测试。
