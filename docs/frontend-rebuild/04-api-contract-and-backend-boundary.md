# 04｜API 契约与后端边界

## 1. 后端策略

本轮保留现有后端，不新建第二套业务服务。允许修改后端的范围仅包括：

- 修复影响新旧前端的接口 Bug；
- 补齐 OpenAPI；
- 增加稳定、脱敏的 API 字段；
- 统一明显不一致的错误字段时保持兼容；
- 提供 Vue SPA 静态文件和 History Fallback；
- 增加前端构建、缓存和版本信息；
- 增加接口契约测试。

禁止顺带修改：

- SQLite Schema；
- v29 硬切换逻辑；
- Provider 凭证模型；
- Planner 策略；
- Transfer 状态机；
- rclone/relay 业务语义；
- Organizer 文件操作语义；
- Job 幂等规则；
- Worker 队列和租约模型。

## 2. 当前契约问题

### 2.1 OpenAPI 不完整

实际 Flask 应用约有：

- 160 条路径；
- 193 个 HTTP 操作。

当前 OpenAPI 只有：

- 117 条路径；
- 136 个操作；
- 缺少 57 个操作。

缺少的关键 API 包括：

```text
GET    /api/admin/providers
GET    /api/admin/provider-accounts
POST   /api/admin/provider-accounts
GET    /api/admin/provider-accounts/{account_id}
PATCH  /api/admin/provider-accounts/{account_id}
DELETE /api/admin/provider-accounts/{account_id}
POST   /api/admin/provider-accounts/{account_id}/test
POST   /api/admin/provider-accounts/{account_id}/activate
POST   /api/admin/provider-accounts/{account_id}/disable

POST   /api/admin/provider-auth-sessions
GET    /api/admin/provider-auth-sessions/{session_id}
POST   /api/admin/provider-auth-sessions/{session_id}/verify
DELETE /api/admin/provider-auth-sessions/{session_id}

GET    /api/admin/openlist/connections
POST   /api/admin/openlist/connections
PATCH  /api/admin/openlist/connections/{connection_id}
DELETE /api/admin/openlist/connections/{connection_id}
POST   /api/admin/openlist/connections/{connection_id}/test

GET    /api/admin/openlist/mounts
POST   /api/admin/openlist/mounts
PATCH  /api/admin/openlist/mounts/{mount_id}
DELETE /api/admin/openlist/mounts/{mount_id}
POST   /api/admin/openlist/mounts/{mount_id}/test
POST   /api/admin/openlist/mounts/{mount_id}/confirm-identity
GET    /api/admin/openlist/mounts/dirs

GET    /api/admin/storage-setup
POST   /api/admin/storage-setup/accounts
GET    /api/admin/storage-setup/accounts/{account_id}/folders
PUT    /api/admin/storage-setup/openlist
DELETE /api/admin/storage-setup/openlist/{connection_id}
POST   /api/admin/storage-setup/locations
PATCH  /api/admin/storage-setup/locations/{destination_id}
DELETE /api/admin/storage-setup/locations/{destination_id}
PUT    /api/admin/storage-setup/routes
POST   /api/admin/storage-setup/preflight

GET    /api/admin/storage-targets
POST   /api/admin/storage-targets
GET    /api/admin/storage-targets/{destination_id}
PATCH  /api/admin/storage-targets/{destination_id}
DELETE /api/admin/storage-targets/{destination_id}
POST   /api/admin/storage-targets/{destination_id}/test
POST   /api/admin/storage-targets/{destination_id}/disable
GET    /api/admin/storage-targets/{destination_id}/bindings

GET    /api/admin/source-assignments
PUT    /api/admin/source-assignments
GET    /api/admin/routing-matrix
POST   /api/admin/settings/all
PUT    /api/admin/notifications
```

阶段 0 必须为这些接口增加 OpenAPI paths、请求体、响应、错误码和权限声明。

### 2.2 响应 Envelope 不完全统一

当前接口可能出现：

```json
{"success": true, "items": []}
```

```json
{"success": true, "data": {}}
```

```json
{"ok": true, "checks": {}}
```

错误可能使用：

```json
{"success": false, "code": "...", "message": "..."}
```

或者：

```json
{"success": false, "error_code": "...", "message": "...", "trace_id": "..."}
```

迁移期间不要求一次修改全部后端响应。前端通过 `api/client.ts` 和 Feature Adapter 统一转换。

## 3. 前端标准错误模型

```ts
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly traceId?: string
  readonly fieldErrors: Record<string, string>
  readonly retryable: boolean
  readonly retryAfter?: number
  readonly details?: unknown
}
```

字段解析优先级：

```text
code = payload.code
    || payload.error_code
    || HTTP_<status>

message = payload.message
       || payload.error
       || HTTP 默认文案
```

Trace ID 在错误页面和复杂操作 Dialog 中显示为：

```text
诊断编号：00367628ee3f4e4c80cdd0068847fbdb
```

不得默认把原始响应 JSON 全部展示给用户。

## 4. 认证与 CSRF

### 4.1 Session 初始化

管理端启动：

```text
GET /api/admin/session
  -> logged_in=false：进入 /admin/login
  -> logged_in=true：保存 user 摘要和 csrf_token
```

请求必须使用：

```ts
credentials: 'same-origin'
```

### 4.2 登录

```text
POST /api/admin/login
Body: { username, password }
```

成功后：

- 清除旧 Query Cache；
- 更新 Session Store；
- 导航到用户原目标或 `/admin/overview`；
- 不把密码写入 Store、日志或 LocalStorage。

### 4.3 写请求

所有 `/api/admin/*` 非 GET/HEAD/OPTIONS 请求，除登录/退出等后端豁免路径外，发送：

```text
X-CSRF-Token: <session.csrf_token>
```

规则：

- 页面不直接读取 Token；
- API Client 根据 `csrf: true` 自动添加；
- Mutation 遇到 401：清空 Session，跳转登录；
- Mutation 遇到 CSRF 403：提示 Session/页面状态可能过期；
- 不自动重试有副作用 Mutation，避免重复创建、重试或取消；
- GET 可在 Session 刷新后安全重试一次，但必须防循环。

### 4.4 退出

```text
POST /api/admin/logout
```

无论接口是否成功，前端都应清理本地 Session/Query Cache 并返回登录页；失败时可以附带提示。

## 5. API Client 接口

```ts
export interface RequestConfig<TBody = unknown> {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: TBody
  query?: Record<string, string | number | boolean | null | undefined>
  headers?: HeadersInit
  signal?: AbortSignal
  csrf?: boolean
  timeoutMs?: number
}

export async function requestJson<TResponse, TBody = unknown>(
  path: string,
  config?: RequestConfig<TBody>,
): Promise<TResponse>
```

实现要求：

- Query 参数使用统一函数编码；
- Body 为普通对象时自动 JSON；
- Body 为 FormData 时不手工设置 Content-Type；
- 处理 204；
- 检查 `response.ok`；
- 错误响应不是 JSON 时保留 HTTP 状态和短文本摘要；
- 使用 AbortController 实现可选超时；
- 不记录 Secret Body；
- 只允许同源相对路径，避免 API Client 被用作任意代理。

## 6. Feature API 模块

### 6.1 Auth

```text
GET  /api/admin/session
POST /api/admin/login
POST /api/admin/logout
GET  /api/admin/security/status
```

### 6.2 Dashboard / Requests / Jobs

```text
GET    /api/admin/dashboard
GET    /api/admin/requests
GET    /api/admin/requests/{request_id}
POST   /api/admin/requests/{request_id}/approve
POST   /api/admin/requests/{request_id}/reject
POST   /api/admin/requests/{request_id}/cancel

GET    /api/admin/jobs
GET    /api/admin/jobs/{job_id}
POST   /api/admin/jobs/{job_id}/retry
POST   /api/admin/jobs/{job_id}/cancel
DELETE /api/admin/jobs/{job_id}
POST   /api/admin/jobs/batch-retry
```

列表 Query 必须封装分页和筛选，不允许页面手工拼接 URL。

### 6.3 Provider Accounts / Auth Sessions

路径见第 2.1 节。前端 Adapter 必须区分：

- Provider Catalog；
- Account Record；
- Account Selection/Active；
- Auth Method；
- Auth Session；
- Test Result；
- Field Errors。

Auth Session 错误需要保留：

- `stage`；
- `provider_kind`；
- `auth_method`；
- `http_status`；
- `retryable`；
- `field_errors`；
- `trace_id`。

### 6.4 OpenList / Storage

使用 Storage Setup 聚合 API 作为页面主入口，细粒度 API 用于特定操作或后续拆分。

目录接口必须支持 AbortSignal，返回节点经 Adapter 转换为统一 `FolderNode`。

Provider 根目录不能由组件猜测，应从 Catalog/Manifest 字段读取：

- Mobile：`/`；
- Quark：`0`；
- Tianyi：`-11`；
- 未来 Provider：由后端 Manifest 返回。

### 6.5 Organizer

```text
GET    /api/admin/organizer/tasks
POST   /api/admin/organizer/tasks/scan
GET    /api/admin/organizer/tasks/{task_id}
DELETE /api/admin/organizer/tasks/{task_id}
POST   /api/admin/organizer/tasks/{task_id}/rebuild
PATCH  /api/admin/organizer/tasks/{task_id}/mappings/{mapping_id}
POST   /api/admin/organizer/tasks/{task_id}/mappings/batch
POST   /api/admin/organizer/tasks/{task_id}/approve
POST   /api/admin/organizer/tasks/{task_id}/apply
POST   /api/admin/organizer/tasks/{task_id}/skip
POST   /api/admin/organizer/tasks/{task_id}/retry
GET    /api/admin/organizer/runs
POST   /api/admin/organizer/runs/{run_id}/rollback
```

### 6.6 rclone / Logs / Media

```text
GET  /api/admin/rclone/status
POST /api/admin/rclone/start
POST /api/admin/rclone/stop
GET  /api/admin/rclone/logs
GET  /api/admin/rclone/runs
GET  /api/admin/rclone/events
GET  /api/admin/rclone/files
POST /api/admin/rclone/files/{event_id}/retry
GET  /api/admin/rclone/check
GET  /api/admin/rclone/webdav-config
POST /api/admin/rclone/webdav-config
POST /api/admin/rclone/webdav-config/test

GET  /api/admin/system/logs
GET  /api/admin/system/events
GET  /api/admin/system/task-logs

GET  /api/admin/media/libraries
GET  /api/admin/media/running
GET  /api/admin/media/refresh-logs
POST /api/admin/media/refresh
```

### 6.7 Updates / Trending

保持现有路径，Feature API 分别封装订阅、运行、候选和调度，不得合并为一个 `adminApi.ts`。

### 6.8 Notifications / Settings / Profile

上传头像、Logo 使用 FormData；高级配置导入导出需要文件下载/上传辅助函数；Secret 值不回显。

### 6.9 Public

```text
GET  /api/public/config
GET  /api/public/captcha
POST /api/public/search
POST /api/public/detect
POST /api/public/manual/preview
GET  /api/public/resources/{public_id}/detail
GET  /api/public/resources/{public_id}/files
POST /api/public/sixpan/parse
POST /api/public/btbtla/resolve
POST /api/public/submit
GET  /api/public/request/{token}
GET  /api/public/trending
```

公开接口返回内容必须经过运行时校验或防御性 Adapter，尤其是多 Provider 文件节点。

## 7. 前端 ViewModel 适配

示例：后端 Provider Account 不应直接进入组件。

```ts
export function toProviderAccountView(raw: ApiProviderAccount): ProviderAccountView {
  return {
    id: raw.account_id,
    providerKind: raw.provider_kind,
    displayName: raw.display_name || raw.identity?.display_name || '未命名账号',
    active: Boolean(raw.active),
    disabled: Boolean(raw.disabled),
    configured: Boolean(raw.configured),
    revision: Number(raw.current_revision || 0),
    lastTest: toAccountTestView(raw.last_test),
  }
}
```

好处：

- 组件不处理历史字段；
- 后端字段变化只影响 Adapter；
- 测试可固定 ViewModel；
- 中文 Label、状态 Tone 和按钮能力集中处理。

## 8. OpenAPI 补齐标准

每个 Path 至少包含：

- Summary；
- Tags；
- Security；
- Path/Query 参数；
- Request Body；
- 200/201/204；
- 400/401/403/404/409/422/429/500 中实际可能返回的状态；
- 成功 Schema；
- 标准错误 Schema；
- 字段描述；
- Secret/脱敏说明。

新增 CI 契约测试：

1. 构造临时应用获取 `url_map`；
2. 规范化 Flask `<int:id>` 为 OpenAPI `{id}`；
3. 比较所有公开、管理和系统 HTTP 操作；
4. 排除 `static` 和明确不公开的内部 Worker 路由；
5. 发现未记录路由时测试失败。

内部 `/api/internal/*` 是否进入公开 OpenAPI 可单独决定，但必须有内部契约测试，不能无人维护。

## 9. 类型生成策略

### 阶段 A：OpenAPI 补齐前

- 手工维护迁移 Feature 所需 `Api*` 类型；
- 使用真实测试 Fixture；
- Zod 校验高风险接口；
- 不生成全量 SDK。

### 阶段 B：OpenAPI 补齐后

使用 `openapi-typescript` 生成：

```text
src/api/generated/schema.d.ts
```

规则：

- 生成文件不手工编辑；
- Feature API 仍负责请求和 ViewModel 转换；
- 不让页面直接操作 `paths[...]` 复杂类型；
- CI 检查生成结果是否与 `/openapi.json` 同步；
- OpenAPI 变化必须同时更新前端适配和测试。

## 10. 轮询与重试契约

| 场景 | 建议间隔 | 终止条件 | Mutation 自动重试 |
|---|---:|---|---|
| Provider Auth Session | 1–3 秒，后端提示优先 | success/failed/expired/cancelled | 否 |
| 运行中 Job | 2–5 秒 | Job 终态 | 否 |
| Organizer Task | 3–5 秒 | 终态/人工审核 | 否 |
| rclone 状态 | 2–5 秒 | 页面离开或停止 | 否 |
| Request Status | 5–10 秒 | 完成/失败/取消 | 否 |
| Dashboard | 15–30 秒 | 页面离开 | 不适用 |

GET 网络失败可以有限重试；写操作失败不得自动重复。

## 11. 文件上传与下载

### 上传

- 头像；
- 站点 Logo；
- 高级配置导入；
- 可能存在的其他配置文件。

使用统一 `requestFormData()`，显示上传进度仅在 Fetch 能力不足且产品确实需要时再引入 XHR，不为所有请求改用第二套客户端。

### 下载

- 高级配置导出；
- 日志下载（若后端提供）；
- 诊断文件。

统一处理 Blob、文件名和错误 JSON，不允许把错误响应当文件保存。

## 12. 后端最小改动任务

### 必做

1. 修复 `/api/public/config` 在可选 Provider 未激活时 500。
2. OpenAPI 补齐 57 个缺失操作。
3. 增加 url_map 与 OpenAPI 对齐测试。
4. 增加 SPA 静态托管和 History Fallback。
5. 为静态构建版本增加缓存和版本信息。

### 建议做

1. 标准错误同时返回 `code`，保留旧 `error_code` 一段兼容期。
2. 所有 5xx 保留 `trace_id`。
3. 分页接口统一返回 `page`、`per_page`、`total` 或明确 cursor。
4. 时间统一 ISO 8601 UTC，前端本地化显示。
5. Provider Manifest 明确目录浏览根、定位器模式和字段定义。

### 暂不做

- 全量响应改成统一 `{data, meta}`；
- 新建 `/api/v2`；
- 替换 Flask Session；
- 引入 JWT；
- 引入独立 BFF；
- 修改任务和数据库契约。

## 13. API 验收清单

- [ ] 新前端不存在直接 `fetch`；
- [ ] Session 和 CSRF 集中处理；
- [ ] Mutation 不自动重试；
- [ ] 401 能返回登录且保留原目标；
- [ ] 错误显示 Code、Message 和可选 Trace ID；
- [ ] 后端字段错误能绑定表单；
- [ ] OpenAPI 与实际路由一致；
- [ ] 自动生成类型可重复生成且 Git 干净；
- [ ] Provider 未配置是正常不可用态，不是 500；
- [ ] API Client 不记录 Secret；
- [ ] 所有目录和轮询请求支持取消；
- [ ] 文件上传、下载和普通 JSON 不混用错误解析逻辑。

