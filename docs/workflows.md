# n8n 工作流与节点设计

> 范围说明：本文保留为后续完整工作流方案。当前 MVP 01 已缩小为拼多多商品资料与图片采集，实际代码、启动方式和验证状态见 [MVP 实现文档](mvp-implementation.md)。本文中的“尚未实现”等历史表述针对原方案交付时点，不作为当前首版状态。

这是一份节点搭建蓝图，尚未导出为可导入的 n8n JSON。实际实施时，在固定的 n8n 版本中搭建、导出并去除凭据。下列业务端点由本项目 Python 服务实现，不是拼多多或闲鱼官方 API。

## 1. 节点总览

| 节点 | 用途 | 是否承担核心业务规则 |
|---|---|---|
| Webhook | 接收批次导入、补充资料、审核操作 | 否；请求需鉴权，Python 再做授权和数据校验 |
| Respond to Webhook | 返回 batch ID、run ID 或审核结果 | 否 |
| Schedule Trigger | 定期领取任务、处理恢复事项 | 否 |
| Edit Fields (Set) | 显式传递上下文和整理字段 | 否 |
| HTTP Request | 调用 Python 业务 API | 否 |
| IF / Switch | 按业务状态、阶段、错误类型分流 | 只编排 |
| Loop Over Items | 批次迭代，初始 batch size 为 1 | 只编排 |
| Execute Sub-workflow / 子工作流触发器 | 复用单商品和异步任务等待流程 | 只编排 |
| Wait | 等待 30 秒后再次查询任务 | 否 |
| Basic LLM Chain + Chat Model | 按固定提示词生成草稿 | 只生成，模型供应商实施时选择 |
| Structured Output Parser | 将模型输出限制为约定结构 | 结构约束；Python 仍再次校验 |
| Error Trigger | 捕获未处理的执行失败 | 否 |
| Stop And Error | 将非预期失败转交错误流程 | 否 |
| Code（可选） | 小规模 JSON 整理、展示字段拼装 | 不运行采集，不存登录态 |

首版不需要 AI Agent 节点，不需要在 n8n 中执行 Python shell，也不直接写业务数据库。

## 2. WF-01：导入商品批次

```text
Webhook POST /imports（Header/JWT Auth）
  → Edit Fields：保留 request_id、items、selling_profile_id
  → HTTP Request：POST /v1/batches，携带 Idempotency-Key
  → Switch：成功 / 参数错误 / 服务异常
  → Respond to Webhook：202 + batch_id / 4xx / 503
```

- Python 先校验请求，再在事务中持久化批次和 runs；确认保存后才返回 202。
- n8n 不在返回 202 后依赖同一次执行继续处理；WF-02 从持久化队列领取，避免响应后崩溃导致任务丢失。
- 相同 request ID 和相同请求返回原批次；同 key 不同内容返回 409。
- 原型用 JSON 链接数组。CSV 上传可在后续增加 Extract from File 节点，最终仍调用同一导入 API。

## 3. WF-02：调度及恢复

默认每分钟运行一次，时区设置 Asia/Shanghai。初期全局只允许一个商品流水线运行。

| 顺序 | 节点 | 输入与行为 |
|---|---|---|
| 1 | Schedule Trigger | 周期启动 |
| 2 | HTTP Request | `POST /v1/maintenance/reconcile`，恢复未投递 job、检查过期租约；不自动解除人工暂停 |
| 3 | HTTP Request | `POST /v1/runs/claim`，limit=1；原子领取可执行 run，返回 lease token |
| 4 | IF | 没有可领取任务则结束 |
| 5 | Execute Sub-workflow | 调用 WF-03，携带 run ID、lease token、当前阶段；选择等待子流程完成 |

`claim` 在业务库中限制全局活动数，定时执行发生重叠也不会重复领取。过期租约仅在确认可恢复后重新分配；新租约令旧执行的状态更新失效。

`reconcile` 会识别已开始但结果未知的模型调用，转为 `needs_input`，而不是自动再付费生成。浏览器 job 可按幂等记录继续查询。人工点击 Retry Execution 也必须重新经过 claim；不直接重放整个业务流水线。

## 4. WF-03：单商品主流程

入口使用子工作流触发器接收明确 JSON 字段。每次从 Python 获取当前 run，按保存的阶段继续。分支清单：

| 阶段 | n8n 节点 | Python 工作与输出 | 失败/缺失分支 |
|---|---|---|---|
| `collect_source` | Execute Sub-workflow → WF-04 | 拼多多采集、规范化、资产归档 | `needs_login` 暂停；SKU 未选 `needs_input` |
| `discover` | Execute Sub-workflow → WF-04 | 从已确认资料生成关键词、闲鱼搜索并去重 | 登录问题暂停；无候选等待人工选择 |
| `enrich` | Execute Sub-workflow → WF-04 | 在预算内读取候选详情、图片和指标 | 部分失败保留成功项，输出覆盖率 |
| `match_rank` | Execute Sub-workflow → WF-04 | 同款规则判定、候选排序、选参考样本 | 同款不确定/浏览量全缺，进入 `needs_input` |
| `generate` | HTTP Request → Basic LLM Chain → Parser → HTTP Request | 获取生成上下文、登记模型尝试、保存输出并做检查 | 输出不合格最多修正一次；结果未知需人工决定是否重试 |
| 人工审核 | HTTP Request 后结束本次执行 | 草稿置 `awaiting_review` | 不在 n8n 中无限 Wait 审批 |
| `export` | Execute Sub-workflow → WF-04 | 校验批准版本，生成 CSV/ZIP | 审批过期则回到审核；成功 `completed` |

每个成功阶段调用 `POST /v1/runs/{id}/advance`，明确传入预期阶段和结果 ID。服务端校验前置结果、版本和租约，再更新下一阶段。`Switch` 继续处理新的阶段或结束。

### 生成节点展开

```text
HTTP Request：GET /v1/runs/{id}/generation-context
  → HTTP Request：POST /v1/runs/{id}/generation-attempts（预留费用和次数）
  → Basic LLM Chain + Chat Model + Structured Output Parser
  → HTTP Request：PUT /v1/generation-attempts/{id}/result
  → Switch：pass / correctable / blocked
      pass → HTTP Request advance → awaiting_review → 结束
      correctable 且尚有预算 → 携带检查问题修正一次 → 再提交
      blocked 或预算耗尽 → 保存问题 → awaiting_review（禁止批准）→ 结束
```

模型报错和 Parser 报错也应显式保存到 generation attempt。不得只依靠全局 Error Trigger，否则无法区分未调用、已计费但丢失响应、输出解析失败。

保留 `run_id`、`source_revision`、`reference_revision`、`prompt_version`、`attempt_id`、lease token 等上下文，不能假设模型节点会原样透传输入。可通过明确引用前序节点输出并使用 Edit Fields 恢复。

## 5. WF-04：提交和等待 Python 异步任务

```text
子工作流输入 run_id、stage、revision、lease_token
  → HTTP Request：POST /v1/runs/{id}/jobs（固定幂等键）
  → HTTP Request：GET /v1/jobs/{job_id}
  → HTTP Request：POST /v1/runs/{id}/heartbeat
  → Switch job.status
      succeeded → 返回 result_id、coverage、warnings
      queued/running/retrying → IF 超过等待预算？
          否 → Wait 30 秒 → 回到 GET job
          是 → 标记延后检查、释放 run 租约 → 返回 deferred
      needs_login/needs_input → 将 run 暂停 → 结束
      failed → 记录阶段失败 → 结束
```

- 一次等待窗口初始设为 15 分钟；超时表示“稍后继续检查”，不是证明 Python job 失败。
- Python job 有自己的阶段超时和重试上限。n8n 恢复时继续查询同一 job ID，不新建相同采集任务。
- 每次请求超时建议初始设为 30 秒，模型节点上限单独配置；实施时依据部署和模型调整。
- n8n 只重试短暂 HTTP 连接故障；业务 job 的重试由 Python 负责，避免两层相乘。
- HTTP 写入必须带幂等键。网络超时后按相同 key 查询/重发，不能重新生成 key。
- 若运行租约返回 409（被新执行接管），当前 n8n 执行立即停止更新业务状态。

## 6. WF-05：补充资料、人工审核与导出

使用受保护的自建审核页调用 Webhook；首版也可用内部 API 客户端完成。审核页属于待实现部分，不把 n8n 编辑器当作商品运营后台。

### 补充资料

`Webhook /run-inputs → HTTP Request POST /v1/runs/{id}/inputs → Respond to Webhook`

支持选定 SKU、补充真实销售条件、确认 possible 候选、提供候选链接、明确接受热度未知或只用源商品生成。服务端根据修改范围建立新 revision、废弃受影响结果并回到正确阶段。补充资料不等于审核批准。

### 审核

`Webhook /reviews → HTTP Request POST /v1/drafts/{id}/reviews → Respond to Webhook`

请求包括 reviewer identity、draft revision、选中的文案版本、图片 ID 列表和 approve/reject 决定。身份来自鉴权上下文，不能信任请求正文自报的审核人。

- 有 blocking issues、素材权限不明确、事实或草稿版本过期时返回 409/422，不能批准。
- approve 后置 `approved`、下一阶段 `export`；由 WF-02 领取后执行。
- reject 后置 `rejected`；人工修改或明确请求再生成后才能继续。
- 图片或文案有任何编辑都会生成新草稿版本，必须重新审核。
- ZIP 只包含批准的一个文案版本、批准图片、CSV 与来源清单，不打包原始 Cookie 或完整网页响应。

## 7. WF-06：错误记录

`Error Trigger → Edit Fields（execution ID、错误摘要）→ HTTP Request POST /v1/execution-errors`

- Error Trigger 是未处理异常的兜底；可预期的登录、缺字段、预算耗尽由主流程正常分支处理。
- 错误通知默认只记录内部待办。本项目不默认给飞书、邮件或其他人发送消息。
- 错误流程本身失败时由 WF-02 的过期租约恢复兜底；业务库不得依赖错误回调一定送达。
- 记录的是脱敏摘要，不记录 Cookie、Authorization、完整商品 HTML 或全部模型输入。

## 8. 不默认添加的节点

- Postgres：业务表由 Python API 管理，避免 n8n 绕过状态校验。
- Execute Command：浏览器和文件处理在 Python worker 内执行。
- 社交平台发送或发布节点：不属于本期范围。
- 图片生成节点：先验证素材来源和商品一致性，当前只处理已有图片。

## 9. 节点资料

- [HTTP Request](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.httprequest/)
- [Webhook 与响应模式](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/)
- [Wait](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.wait/)
- [流程控制与子工作流](https://docs.n8n.io/build/flow-logic.md)
- [错误处理](https://docs.n8n.io/build/flow-logic/handle-errors-gracefully.md)

本文中的等待时长、并发数、接口路径和业务状态是项目设计选择，不是 n8n 的默认值或平台服务承诺。
