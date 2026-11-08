# Agent MCP 接口（`/mcp`）

本文件是 Agent 能力面的**单一事实来源**。改动工具、作用域或返回结构时，必须同步更新这里。

## 1. 定位

当前协议能力版本为 **0.3.0**。具备执行权限的 Agent 可以按用户规则自主审核、整理与恢复，不强制每次请人确认；身份、路径、冲突与计划指纹校验仍保留。升级后客户端应重新获取 `tools/list`；工具总数仍为 35，不增加跳过身份校验或全库清洗入口。

`/mcp` 把系统已有的能力（搜索、入库、整理、追更订阅、运行状态）以 JSON-RPC 2.0 暴露给外部 Agent。
它**不是**管理后台 API 的镜像：只暴露经过挑选、面向任务的能力；设置、账号凭证、备份、高级配置一律不对外。

## 2. 启用

```bash
# .env
FNOS_AGENT_ENABLED=true
AGENT_MAX_CONCURRENCY=2          # 同时执行的工具调用上限，保护 Web 线程
```

未开启时 `/mcp` 返回 404，管理后台「Agent 接入」页会提示“Agent 接口未启用”而不是加载失败。
接口只在 `web` 角色挂载。

> 出现 `加载失败：The requested URL was not found` 说明当前进程里根本没有注册 Agent 路由，
> 通常是这个开关没打开（或镜像还是旧版本），不是页面坏了。

## 3. 传输与端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/mcp` | JSON-RPC 2.0，单条或批量 |
| GET | `/mcp` | 能力探测（无需令牌） |
| DELETE | `/mcp` | 无状态传输，返回 204 |
| OPTIONS | `/mcp` | 返回允许的方法 |

- **无状态**：不返回 `Mcp-Session-Id`，每次 POST 独立。
- **JSON 响应**，不启用 SSE：长连接会占住 gunicorn 线程。
- 支持方法：`initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`、
  `resources/list`、`resources/read`、`prompts/list`、`prompts/get`、`logging/setLevel`。

```bash
curl -s http://NAS:5251/mcp \
  -H 'Authorization: Bearer fnos_agent_xxx' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## 4. 令牌、档位与作用域

令牌格式 `fnos_agent_<random>`，数据库只保存 SHA-256 摘要；明文仅在创建时输出一次。
可以在管理后台「系统配置 → Agent 接入」里创建、撤销和查看审计，也可以用 CLI：

```bash
docker exec -it fnos-media-import python -m fnos_media_import.agent.cli create --name hermes
docker exec -it fnos-media-import python -m fnos_media_import.agent.cli create --name hermes --profile operator
docker exec -it fnos-media-import python -m fnos_media_import.agent.cli create --name hermes --profile full --expires-at 2026-12-31T23:59:59+08:00
docker exec -it fnos-media-import python -m fnos_media_import.agent.cli list
docker exec -it fnos-media-import python -m fnos_media_import.agent.cli revoke --id 2
docker exec -it fnos-media-import python -m fnos_media_import.agent.cli audit --limit 20
```

| 档位 | 作用域 | 能做什么 |
|---|---|---|
| `read`（默认） | `search` `import.read` `organize.read` `schedule.read` `ops.read` | 只能查询 |
| `operator` | read + `import.write` `organize.write` `schedule.write` | 可提交入库、改映射、重新识别、管订阅；**不能直接放行整理或回滚** |
| `full` | operator + `organize.apply` | 可按用户的 Agent 规则自主审核、执行真实改名/移动、恢复与回滚 |

`--scopes` 可以精确覆盖档位。默认令牌只读；直接执行整理/回滚必须有 `organize.apply`。提交入库、运行订阅仍会进入系统已有的后台业务流程，其自动化策略由系统配置控制，不能把 `operator` 理解为所有业务都没有文件副作用。

## 5. 工具清单（35 个）

### 发现（`search`）

| 工具 | 作用 |
|---|---|
| `search_resources` | 聚合搜索，返回带 `public_id` 的候选 |
| `get_resource` | 资源详情、入库能力、顶层文件（`inspection.items[].fid`） |
| `list_resource_files` | 展开某个目录（`fid` 默认 `"0"`） |
| `detect_link` | 识别链接类型与是否支持自动入库 |
| `resolve_tmdb` | TMDB 识别，返回候选与 `selection_token` |

### 入库（`import.read` / `import.write`）

| 工具 | 作用 | 风险 |
|---|---|---|
| `list_jobs` / `get_job` | 入库任务列表与详情（含 `row_version`） | read |
| `get_import_request` | 提交单详情与关联任务 | read |
| `submit_import` | 提交入库，支持 `dry_run` | write |
| `approve_import_request` | 批准待审核提交单 | write |
| `reject_import_request` | 拒绝提交单（必须给原因） | destructive |
| `retry_job` | 重新排队失败任务 | write |
| `cancel_job` | 取消进行中任务（必须 `confirm=true`） | destructive |
| `confirm_job_not_submitted` | 复核后按原计划重新提交（必须 `confirm`、`row_version`、`note`） | destructive |

`submit_import` 用令牌鉴权代替访客验证码，但**不跳过**内容风控、重复提交检查与入库协调。
`dry_run=true` 只做参数与链接识别，不创建提交单。

提交成功后会返回 `request_id`、`job_id`、`request_status`（由 Agent bridge 从提交编号反查，
公共访客响应本身不含内部编号）。`job_id` 为空表示提交停在待审核，需要 `approve_import_request`。

`get_job` 会带上 `transfer_execution`：`phase`、`row_version`、`review.required`、
`review.can_resume`、`review.can_confirm_not_submitted`、`review.guidance` 与三个核对路径。
`confirm_job_not_submitted` 需要的 `row_version` 就在这里。

### 整理（`organize.read` / `organize.write` / `organize.apply`）

| 工具 | 作用 | 风险 |
|---|---|---|
| `list_organizer_tasks` / `get_organizer_task` | 任务、分页文件/映射/阻塞项、身份、目标根、审核原因和计划摘要 | read |
| `update_organizer_mappings` | 改 TMDB ID、片名、季集、状态和受约束的目标路径；返回新的 `plan_hash` | write |
| `apply_organizer_task` | `execute=true` 单次执行身份整理；默认预览，兼容 `plan_hash` 执行 | destructive |
| `retry_organizer_task` | 默认仅重新识别；`execute=true` 可重新识别或续跑，额外需要 `organize.apply` | write / 执行分支 destructive |
| `rollback_organizer_run` | 回滚批次；`confirm=true` 表示调用方明确意图，Agent 可按用户规则提供 | destructive |

**`apply_organizer_task` 的执行与预览：**

- **自主执行：** `{"task_id": 123, "execute": true}` 单次调用即可执行，无须预先调用预览或提交 hash。Agent 按用户规则审核证据，哪些情况需要询问人也由这些规则决定。
- **预览：** 显式 `execute=false` 始终只预览（即使同时给了 hash）；`execute/plan_hash` 两者都不提供也只预览。返回 `confirmation_required: true`、`human_confirmation_required: false`、`can_apply` 和计划摘要，**不会执行**。`confirmation_required` 是兼容预览流程的字段，不代表协议强制人类逐次批准。
- **兼容 hash 流程：** Agent 也可自行取得摘要，再带同一 `plan_hash` 执行；不要求额外等待人的回复。计划变化时返回 `PLAN_CHANGED`，需重新读取并判断，而不是盲目重发旧 hash。

摘要包括身份/新增别名、`canonical_dir`、首次/追更意图、作品根/季目录/manifest 写入数量和目标样本；
`targets_total/targets_truncated`、`identities_total/identities_truncated` 明确标记样本不等于全部。
存在身份错误、`need_edit/conflict` 或旧删除状态时，`can_apply=false` 并说明原因；自主执行也不能绕过这些检查。

后台 `waiting_review` 或界面上的“人工确认”表示业务待审核，不是“只能由人类操作”。具备相应权限的 AI 可按用户规则修正映射，再调用 `apply_organizer_task(execute=true)`；仅在用户规则要求或确实无法判断时询问人。

`plan_hash` 覆盖 revision、分类、根路径、映射身份/季集、规范化身份上下文以及按顺序排列的操作和 manifest 新旧内容摘要。
操作数据库 ID、执行进度和时间戳不属于指纹。`execute=true` 在调用内读取当前计划并绑定其 revision 和 hash；旧 hash 流程绑定调用方提供的指纹。批准事务内重新读取数据库核验，
批准后的操作摘要还会在持锁写盘前复核，避免同 revision 的操作变化悄悄改变已确认计划。

**修改映射的三条规则：**

- 必须修正身份、季集或目标冲突的真实原因；`status=ready` 不是绕过闸门。允许的状态为 `ready/need_edit/conflict/skipped`，不再允许 `delete_ad`。
- 只给 `target_name` 会同步推导 `target_path`，避免“改了名字但按旧路径移动”。
- `target_name` 必须是单个文件名；手写目标路径仍必须符合冻结媒体库、作品身份、季集和后缀规则。

单条支持 `tmdb_id/title/year/season/episode/media_type/target_name/target_path/status`；批量只支持 `tmdb_id/title/season`。
TMDB ID 不传保留；单条 `null` 显式解绑，批量 `null` 拒绝；季号 0–99，集号 1–2000，媒体类型仅 `movie/tv`。

**详情分页：** `get_organizer_task` 支持 `files_page/mappings_page/blocked_page`（默认 1），共用 `per_page`（默认 50、上限 100）。
分别返回 `files_pagination/mappings_pagination/blocked_pagination`，包含 `total/page/per_page/pages/has_more`。
文件使用真实 `path/name/parent_path/ext/size/season/episode`；映射提供完整路径、TMDB ID、附件标记及审核原因，不暴露整个 raw_data。
详情中的身份来自当前映射；列表只能读取缓存身份时标记 `identity_source=cached`。

**重试的两种模式：**

- `execute=false`（默认）：持久化审核标记并使旧批准失效，通过独立预览队列运行 `auto_apply=false`。即使重启或旧的自动扫描任务随后运行，也不会仅因这次重扫授权而移动文件；后续可由具备执行权限的 Agent 按用户规则放行。
- `execute=true`：额外需要 `organize.apply`（`full`）；可重新识别后执行，或根据现有执行记录续跑。不通过重建计划抹去部分执行或回滚证据。
- 仅重扫模式遇到已有执行/回滚记录会返回 `EXECUTION_CONFIRMATION_REQUIRED`。根据证据选择执行恢复，或用 `rollback_organizer_run(confirm=true)` 表达回滚意图；这些调用可由获授权 Agent 自主完成。
`skip_organizer_task` 已移除，不提供原名直通入口。`queued=true` 只代表排队，不代表整理完成。

**谁能执行移动：** 只有带 `organize.apply` 的令牌（`full` 档位）。`operator` 令牌看不到该工具，
调用会被 `SCOPE_DENIED` 拒绝。自主执行不扩大令牌权限，也不取消计划绑定：旧 hash 不匹配，或调用内计划发生变化时仍拒绝执行。

### 定时任务（`schedule.read` / `schedule.write`）

| 工具 | 作用 | 风险 |
|---|---|---|
| `list_subscriptions` / `get_subscription` | 订阅、最近运行、候选与事件 | read |
| `create_subscription` | 创建追更订阅，支持 `dry_run`（返回 `next_run_at`） | write |
| `update_subscription` | 修改调度/来源/质量条件 | write |
| `set_subscription_status` | `enabled` / `paused` / `archived` | write |
| `run_subscription_now` | 立即执行一次追更 | write |
| `delete_subscription` | 永久删除订阅（必须 `confirm=true`） | destructive |

`create_subscription` 必须传 `resolve_tmdb` 得到的 `tmdb_id`——服务端会回查 TMDB 校验标题与首播信息。

`update_subscription` 的“清空”语义：只有**出现**在某次调用的字段才会被写入，因此显式传
`include_keywords: []` 或 `quality_profile: ""` 可以清掉原有配置；不传则保持原值。
`sources: []` 会恢复成默认综合搜索来源。

### 媒体库与热榜

`search_library` 走飞牛影视的 `/v/api/v1/search/list?q=`，返回每条命中媒体的
`type`、`ancestor_name`（所属库）、`local_seasons` / `local_episodes` 与 `in_library`。
`in_library=true` 表示媒体库里已有本地文件；TV 只有季集数量大于 0 才算，电影条目存在即算。
建议在 `search_resources` 之前先查一次，避免重复入库。

| 工具 | 作用 | 作用域 | 风险 |
|---|---|---|---|
| `list_media_libraries` | 飞牛媒体库列表、分类映射与运行中的刷新任务 | `ops.read` | read |
| `search_library` | 在飞牛媒体库里按名称检索，返回本地季/集数量与 `in_library` | `ops.read` | read |
| `refresh_media_library` | 按分类触发媒体库刷新 | `organize.write` | write |
| `list_trending_candidates` | 每日热榜候选 | `schedule.read` | read |
| `import_trending_candidate` | 采纳候选并创建首次入库；返回 `job_id` | `import.write` | write |
| `subscribe_trending_candidate` | 以 TMDB 身份把候选转成追更；返回 `subscription_id` | `schedule.write` | write |

### 运维（`ops.read`）

`get_system_status`、`get_scheduler_status`。

## 6. 返回约定

- 每个结果包在 `{"content":[{"type":"text","text":...}], "structuredContent": {...}, "isError": bool}`。
- **字段是投影后的子集**，不是数据库整行；`raw_data` 这类大字段不会返回。
- 列表统一带 `pagination`：`{total, page, per_page, has_more, pages}`。
- `list_resource_files` 用 `limit` + `offset` 翻页，返回 `has_more` 与 `next_offset`，不会只截断不给入口。
- 写工具返回 `{success, message, ...}` 加必要的对象状态与下一步提示。
- 任务状态使用真实枚举：`created` / `provider_submitting` / `checking` / `submitted` /
  `waiting_transfer` / `transferring` / `waiting_openlist` / `waiting_organizer` / `organizing` /
  `confirming` / `review` / `refreshing` / `done` / `failed` / `unsupported` / `cancelled`。
  整理任务：`scanning` / `waiting_review` / `manual_confirmed` / `auto_approved` / `executing` /
  `done` / `skipped` / `cancelled`。

## 7. 资源与提示词

只读资源（`resources/read`）：

| URI | 内容 | 需要作用域 |
|---|---|---|
| `fnos://config/categories` | 分类 key、标签与目录映射 | `search` |
| `fnos://status/system` | 聚合健康状态 | `ops.read` |
| `fnos://status/scheduler` | 追更调度器状态 | `ops.read` |

`resources/list` 只列出当前令牌有权读取的资源，`resources/read` 会再次校验，
因此不能绕过工具的作用域直接读运维状态。

提示词（`prompts/get`）：`ingest_by_title`、`organize_pending`、`follow_series`。
它们只返回工作流文本，不产生任何副作用。

## 8. 安全边界

1. **作用域双重校验**：`tools/list` 按作用域过滤，`tools/call` 再次校验。
2. **最小权限默认**：新令牌只读；移动文件需要 `full` 档位。
3. **计划绑定**：自主 `apply(execute=true)` 绑定当次计划，旧流程匹配传入的 `plan_hash`；批准事务内核验完整快照，写盘前核验操作摘要，变化时返回 `PLAN_CHANGED`。
4. **调用方意图**：取消任务、复核重提、回滚批次、拒绝提交、删除订阅仍保留各自的确认参数或原因要求。Agent 可按用户规则提供这些参数，不等于每次必须询问人；规则要求或证据不足时才升级询问。
5. **重扫与执行分权**：默认重试只重扫，不会因延迟或重启意外放行；`execute=true` 执行恢复必须额外具备 `organize.apply`，并遵守现有恢复记录与身份校验。
6. **限流**：每令牌每分钟默认 60 次，超出返回 429 + `Retry-After`。
7. **并发保护**：`AGENT_MAX_CONCURRENCY` 控制同时执行的工具数，满返回 503 + `SERVER_BUSY`。
8. **审计**：每次调用写入 `agent_tool_calls`，只保存参数摘要（SHA-256 前 32 位）。
9. **脱敏**：输出不返回提取码、Cookie、令牌。

## 9. 错误码

工具级错误以 `isError: true` 返回，结构为 `{code, message, retryable, hint, details?}`：

| code | 含义 |
|---|---|
| `INVALID_PARAMS` | 参数缺失、越界或未知枚举 |
| `SCOPE_DENIED` | 令牌没有该作用域 |
| `CONFIRMATION_REQUIRED` | 需要调用方提供 `confirm=true` 明确意图；Agent 可按用户规则提供 |
| `PLAN_CHANGED` | 计划已变化，旧 `plan_hash` 失效 |
| `REVIEW_REQUIRED` | 身份、目录或清单需审核，不自动重试 |
| `EXECUTION_CONFIRMATION_REQUIRED` | 仅重扫不能覆盖已有执行/回滚记录，应选择有权限的执行恢复或回滚入口 |
| `METADATA_UNAVAILABLE` | TMDB 读取失败，检查服务后重试，不自动降级 local |
| `TEMPORARILY_UNAVAILABLE` | 已知暂时性失败，可参考 retry_after_seconds 等待 |
| `NOT_FOUND` | 任务 / 订阅 / 资源不存在或已过期 |
| `REJECTED` / `STATE_CONFLICT` | 领域层拒绝或状态冲突 |
| `SEARCH_UNAVAILABLE` | 搜索源不可用，可重试 |
| `LIBRARY_NOT_CONFIGURED` | 飞牛影视地址/账号没配全，去「系统设置 → 飞牛影视」补齐 |
| `LIBRARY_AUTH_FAILED` | 飞牛登录失败：地址不可达或账号密码不对 |
| `LIBRARY_UNAVAILABLE` | 媒体库检索请求失败，`details.upstream_code` 是上游返回码 |
| `PROVIDER_UNAVAILABLE` | 网盘账号未激活 |
| `SERVER_BUSY` / `RATE_LIMITED` | 并发或频率受限 |
| `INTERNAL_ERROR` | 未预期失败，详情看 Web 日志 |

未知错误和审核/计划冲突默认 `retryable=false`。只有已明确分类的暂时故障允许建议退避，不能把所有失败都当作可循环重试。

协议级错误走 JSON-RPC `error`：`-32700` 解析失败、`-32600` 非法请求、`-32601` 未知方法、
`-32602` 参数错误（含未知工具）、`-32001` 未授权。

## 10. 客户端接入

```json
{
  "mcpServers": {
    "fnos-search": {
      "type": "streamable-http",
      "url": "http://192.168.1.10:5251/mcp",
      "headers": { "Authorization": "Bearer fnos_agent_xxx" }
    }
  }
}
```

## 11. 本地验证

```bash
python -m pytest tests/test_agent_mcp_protocol.py tests/test_agent_mcp_blueprint.py \
                 tests/test_agent_token_store.py tests/test_agent_surface_wiring.py \
                 tests/test_agent_write_tools.py -q
python -m ruff check fnos_media_import/agent
```

## 12. 本轮部署注意

本轮不增加表、环境变量或令牌作用域，沿用 v30。更新 Web 和 Worker 的同一版本代码后重启，并让 MCP 客户端刷新工具清单。
不要混跑新 Agent Web 与不识别审核标记的旧 Worker。升级后重新获取工具定义；默认预览和旧 `plan_hash` 调用仍兼容，新增自主执行须显式传 `execute=true`。`operator` 不会因升级获得执行权限，是否询问人由用户的 Agent 规则决定。
本轮验证均使用本地 mock / 临时 SQLite，未部署或操作真实媒体库。身份整理规则见 [organizer-identity.md](organizer-identity.md)。
