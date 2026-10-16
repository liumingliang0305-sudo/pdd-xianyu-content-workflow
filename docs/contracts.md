# Python 接口与数据约定

所有接口都是本项目计划实现的内部业务 API，统一前缀 `/v1`。当前没有实际部署地址。

## 1. 请求约定

- n8n 通过服务凭据调用 Python；人工审核另外使用可识别用户的鉴权上下文。
- 所有写接口带 `Idempotency-Key`，唯一范围为调用主体、HTTP method、路径和 key。
- key 对应规范化请求体摘要；相同请求返回已有记录，同 key 不同请求体返回 409。
- 执行中的 run 状态更新另带 `X-Run-Lease`。人工审核和补充资料不借用工作流租约，而是使用当前版本前置条件。
- 货币使用整数分；业务时间保存 UTC，界面显示 Asia/Shanghai。文本未知值用 null，不用 0。
- n8n 只处理 JSON 和文件引用。文件下载由鉴权端点或短期链接提供，长期记录存 object key 而非会失效的签名 URL。

幂等键按“逻辑操作”生成：导入使用 request ID；阶段任务使用 run ID、stage、input revision、config revision；生成尝试使用 run ID、上下文摘要和尝试序号；导出使用 approval ID。重试同一逻辑操作保留 key。heartbeat 每个续租周期、claim 每次调度、人工每次编辑各使用新 key；不能用一个固定 key 导致续租或领取请求永久重放旧结果。

导入示例（占位符，不是可访问的测试商品）：

```json
{
  "request_id": "import-20261016-001",
  "items": [
    {
      "client_item_id": "row-1",
      "source_url": "<用户提供的真实拼多多链接>",
      "selected_sku_id": null
    }
  ],
  "selling_profile_id": "profile-example",
  "policy_revision": "policy-v1"
}
```

同批次按规范商品标识和选定 SKU 去重并返回重复行映射。跨批次允许用户主动重新采集；仅在已配置的快照时效内复用资料，不能永久跳过同一个 URL。

## 2. 接口清单

| 方法与路径 | 功能 | 成功返回 |
|---|---|---|
| `POST /batches` | 校验并持久化输入批次 | 202，batch ID、每行 run ID 或错误、重复行映射 |
| `GET /batches/{id}` | 批次进度与各商品状态 | 200，汇总和分页 runs |
| `POST /runs/claim` | 原子领取可执行 run；限制全局并发 | 200，run + lease；无任务返回空数组 |
| `GET /runs/{id}` | 阶段、状态、版本和恢复信息 | 200 |
| `POST /runs/{id}/heartbeat` | 延长执行租约 | 200，lease expiry |
| `POST /runs/{id}/defer` | 保存 next_check_at 并释放租约，不取消 job | 200 |
| `POST /runs/{id}/jobs` | 提交采集、搜索、匹配或导出任务 | 202，同幂等 key 返回同一 job |
| `GET /jobs/{id}` | 查询后台任务 | 200，状态、result ID、覆盖率、错误类型 |
| `POST /runs/{id}/advance` | 校验阶段结果，提交状态转移 | 200，新状态/阶段 |
| `GET /runs/{id}/generation-context` | 返回可信事实、卖家条件、参考内容、版本 | 200；前置条件不足 409 |
| `POST /runs/{id}/generation-attempts` | 预留一次模型调用与预算 | 201，attempt ID 和生成快照摘要 |
| `PUT /generation-attempts/{id}/result` | 保存模型输出或错误，验证并生成草稿版本 | 200，validation 状态、问题、draft ID |
| `POST /runs/{id}/inputs` | 提交 SKU、销售条件、候选确认等 | 200，新 revision、恢复阶段 |
| `PUT /drafts/{id}` | 人工编辑形成新草稿版本并重新校验 | 200，新 revision；旧批准失效 |
| `POST /drafts/{id}/reviews` | 对准确版本审批 | 200，approved/rejected；冲突 409 |
| `GET /runs/{id}/exports` | 查询已生成文件引用 | 200，manifest 和下载引用 |
| `POST /maintenance/reconcile` | 处理未投递任务、过期租约和未知模型结果 | 200，动作计数 |
| `POST /execution-errors` | 持久化 n8n 脱敏错误 | 202 |

`jobs.stage` 取 `collect_source | discover | enrich | match_rank | export`。`generate` 由 n8n 调模型，不进入 Python 任务队列。

job 示例：

```json
{
  "job_id": "job-example",
  "run_id": "run-example",
  "stage": "enrich",
  "status": "succeeded",
  "result_id": "result-example",
  "input_revision": "source-v1",
  "coverage": {
    "discovered": 30,
    "detail_requested": 10,
    "detail_succeeded": 8,
    "views_available": 5
  },
  "warnings": ["部分候选浏览量缺失"],
  "error": null
}
```

## 3. Run 状态机

`status` 与 `stage` 分开保存。状态包括：

| 状态 | 含义 | 可执行的后续动作 |
|---|---|---|
| queued | 已入库，等待领取 | claim |
| running | 已有有效租约，正在某阶段 | heartbeat、advance、defer、pause/fail |
| needs_input | 缺 SKU、事实或人工判断 | inputs，建立新版本或明确决定 |
| needs_login | 当前账号需要人工恢复 | 人工恢复登录后，经验证再重新排队 |
| awaiting_review | 文案待审，可能有 blocking issues | 编辑、审核；有阻塞项不能 approve |
| approved | 指定草稿版本已批准 | claim，进入 export |
| rejected | 审核拒绝 | 编辑或明确请求重新生成 |
| completed | 当前批准版本已导出 | 查看导出；再次编辑会开启新修订周期 |
| failed | 重试预算耗尽或不可恢复错误 | 明确重试/修改后重新排队 |
| canceled | 用户取消 | 不再自动恢复 |

defer 将运行状态恢复为 queued，保留 stage/job ID 和 next_check_at。暂停或失败由 job 结果及 advance 的服务端规则决定。首版后台管理可提供取消动作，具体取消 API 在实现阶段补齐。

阶段顺序：`collect_source → discover → enrich → match_rank → generate → review → export`。

人工选择“只根据源商品生成”时保存 bypass decision 和操作者，才可以跳过闲鱼阶段。已确认候选与不足 3 个参考样本可以生成，但需展示覆盖限制。

## 4. 数据表建议

| 表 | 核心字段与约束 |
|---|---|
| batches | id、request ID、操作者、policy revision、created_at |
| runs | batch ID、source URL、status、stage、revision、lease、next_check_at、last error |
| jobs | run ID、stage、input/config revision、status、attempt、result ID、queue delivery status |
| source_snapshots | source item ID、SKU、字段快照、采集时间、来源证据、内容摘要 |
| candidate_snapshots | 闲鱼 item ID、query、描述、指标原文/值/精度、采集时间 |
| matches | source revision、candidate snapshot ID、exact/possible/different、证据和规则版本 |
| assets | 来源 URL、content hash、MIME、尺寸、object key、用途、rights status 与凭据引用 |
| generation_attempts | 输入摘要、模型与提示词版本、调用状态、token usage、成本、原始输出引用 |
| drafts | run ID、revision、三版结构化文案、源/参考 revision、validation、内容 hash |
| reviews | draft revision、选中 variant、图片 ID、审核人、决定、批准内容 hash |
| exports | approval ID、manifest hash、文件引用、生成时间 |
| execution_errors | run ID（如可识别）、n8n execution ID、脱敏错误摘要 |

索引：runs 的 status/next_check_at，jobs 的 run/stage/revision，候选的 item ID/采集时间，assets 的 content hash。禁止只用商品 URL 作为所有版本的唯一键。

## 5. 事实与指标结构

```json
{
  "fact_id": "source.material",
  "field": "material",
  "value": "304 不锈钢",
  "source_type": "source_product",
  "source_revision": "source-v1",
  "evidence_ref": "evidence-example",
  "confirmed": true
}
```

候选指标分别保存：

```json
{
  "views": {"value": null, "raw_text": null, "precision": "unknown", "missing_reason": "not_exposed"},
  "wants": {"value": 12, "raw_text": "12人想要", "precision": "exact", "missing_reason": null},
  "observed_at": "2026-10-16T02:00:00Z"
}
```

示例均为虚构，不是采集结果。对 n8n 返回的资料必须限制长度；完整响应或截图只保存必要证据，去除账户和买家信息。

## 6. 幂等与恢复

### 工作流租约

- run 租约初始 10 分钟；轮询 job 时每 30 秒续租，其他阶段至少每分钟有状态请求。
- 任何可能超过租约的同步调用须改异步或缩短超时。模型调用超时应小于租约并预留余量。
- 更新租约会产生 fencing token；旧执行持旧 token 的写入返回 409，不能覆盖新执行。
- Python job 结果按输入版本独立保存；迟到结果可保存但不能自动推进已变化的 run。

### Python 消息队列

- 业务 job 先入库，队列投递状态同时记录。API 返回 202 表示已持久化，不代表已经执行。
- 提交 Redis 失败或进程在中间崩溃时，reconcile 重发未确认投递的 job ID。
- 消息可能重复；worker 按 job ID 取得执行租约，已完成任务直接返回原结果。
- 保存结果成功但消息确认失败，也不得重复下载、覆盖资产或建立重复导出。
- 临时文件先写临时路径，最终按内容 hash 或结果版本原子归档。

### 模型调用

外部模型无法仅靠本地幂等实现绝对只调用一次。generation attempt 先持久化并预留预算；成功后保存输出与成本。

若调用中断且不知道供应商是否已执行，标为 `unknown`，冻结该次预算并交人工判断。后续确认供应商结果，或用户明确接受可能重复费用后才重新调用。没有证据时不得报告零费用。

### 批准与导出

批准绑定 source revision、draft hash、选中 variant 和资产列表。导出前重新核对；任一变化使批准失效。导出唯一键由 approval ID 决定，同一批准重复执行返回原导出。

## 7. 错误分类与预算

| code | 是否自动重试 | 行为 |
|---|---|---|
| NETWORK_TRANSIENT / UPSTREAM_5XX | 有限重试 | Python 默认最多 3 次总尝试，退避；耗尽后失败 |
| RATE_LIMITED | 有上限 | 尊重可取得的等待指示，暂停账号调度，不能持续请求 |
| LOGIN_REQUIRED / VERIFICATION_REQUIRED | 否 | needs_login，等待人工处理 |
| PERMISSION_DENIED | 否 | 记录权限问题，停止该渠道 |
| SOURCE_NOT_FOUND / REMOVED | 否 | 明确失败，保留链接和时间 |
| SKU_REQUIRED / NO_CONFIRMED_MATCH / METRIC_UNAVAILABLE | 否 | needs_input |
| DRAFT_INVALID | 最多修正一次 | 保存失败证据；仍不通过则人工编辑 |
| BUDGET_EXCEEDED | 否 | 暂停并显示已用预算 |
| STALE_REVISION / LEASE_LOST | 否 | 停止当前执行，不覆盖新版本 |

按每件商品限制搜索数、详情数、图片数、下载字节和模型尝试；按批次限制总模型预算。具体金额待模型选定后配置，不能用未经确认的单价估算生产成本。

## 8. 网络与凭据边界

- URL 接收器只接受允许的平台域名；每次短链跳转重新检查域名、解析后的 IP 和协议，拒绝内网、环回及云元数据地址。下载图片使用单独验证的 CDN 域名范围，不能接受任意外部 URL。
- 浏览器会话由 Python 侧专用目录保管并限制权限，按账号隔离，不进入 Git、模型上下文或 n8n execution data。
- Webhook 开启鉴权；API 在私网部署，服务凭据最小化权限。审核角色与工作流服务角色分开。
- 限制图片大小、内容类型与重定向；平台图片地址失效不影响已归档的合法可用素材。
- 日志和备份均不包含明文密钥；n8n 凭据加密密钥单独备份，不上传仓库。
