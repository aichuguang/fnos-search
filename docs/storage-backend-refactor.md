# 存储后端 V1 定稿说明

> 本文描述当前唯一受支持的存储与转存模型。完整业务约束见
> [V1.0 多链路设计文档](./V1.0多链路设计文档.md)。

## 1. 唯一真源

以下资源只从数据库新版模型读取，不再从 `config.yaml`、环境变量或任务中的旧路径字段恢复：

- Provider 账号、账号修订和激活选择；
- OpenList 连接、挂载及其修订；
- 保存位置（storage target）与绑定；
- 搜索来源 + 分类到保存位置的 assignment；
- `plan_schema_version = 1` 的不可变执行计划。

后台“网盘入库”是上述资源的唯一维护入口。高级配置只保存 PanSou、TMDB、AI、Organizer、
飞牛媒体库刷新等非身份运行参数。

## 2. 配置边界

部署环境只负责进程启动、持久化目录和一把数据加密根密钥：

```env
FNOS_ROOT_KEY=<64 位 hex>
FNOS_PROCESS_ROLE=web|scheduler|worker|all
```

正式 Compose 固定 Web 容器为 `web`，后台 Worker 容器为 `all`。Provider Cookie、Token、账号密码、
OpenList 地址和凭证、保存目录、remote、mount 均不得放入环境变量或高级配置。

`FNOS_ROOT_KEY` 通过 HKDF-SHA256 按用途派生 Flask 会话、账号、OpenList、授权会话、来源快照和通知子密钥。
应用会话签名密钥不再单独配置或持久化；Worker 控制令牌只用于进程认证，两者都不是额外的数据加密主密钥。

## 3. V1 资源模型

核心数据库资源：

- `provider_accounts` / `provider_account_revisions` / `provider_account_selections`；
- `provider_auth_sessions`；
- `openlist_connections` / `openlist_mounts` 及修订表；
- `storage_targets` / `storage_target_revisions` / `storage_target_bindings`；
- `storage_assignments`；
- `import_source_records`；
- `import_execution_plans`；
- `transfer_executions`。

在途任务使用计划冻结的账号、目标、挂载、连接和路径修订；管理员切换当前账号或目标只影响新任务。

## 4. 路由与策略

访客只提交资源和选集，不得提交账号、目标、remote、mount 或绝对路径。服务端根据已启用的
`search_source_id + category_key` assignment 解析唯一目标，并生成下列策略之一：

| 场景 | 策略 |
| --- | --- |
| 同 Provider 原生转存 | `native_save` |
| 磁链进入六盘 | `native_offline` |
| 可读来源进入非移动目标 | `rclone_copy` |
| 非移动来源进入移动云 | `relay_upload_mobile` |

约束：

- 同源必须使用 Provider 原生能力，禁止静默降级为跨盘复制；
- 磁链只允许六盘原生离线；
- `rclone_copy` 必须冻结 `source_landing`、`destination_staging` 和 `destination_final`；
- `relay_upload_mobile` 必须冻结 `source_landing`、`local_temp` 和 `destination_final`；
- 路径、能力或修订缺失时任务进入审核/拒绝，不从当前配置猜测。

## 5. 执行计划

运行时只接受：

```json
{
  "plan_schema_version": 1,
  "job_id": 123,
  "destination_id": "target-id",
  "strategy": "rclone_copy",
  "paths": {
    "source_landing": {},
    "destination_staging": {},
    "destination_final": {},
    "organizer": {}
  }
}
```

计划以 canonical JSON 和 hash 写入 `import_execution_plans`，并在任务 `raw_data.execution_plan`
保存同一份 V1 快照供只读状态展示。执行器必须使用数据库计划真源并重新校验 hash、版本、任务 ID、
策略、执行器和必需路径。

缺少有效 V1 计划的历史任务不得启动 rclone、Organizer、回调确认、六盘轮询或自动重试；系统应将其
转入 `review_required` / `rejected`，由管理员基于新版配置重新提交。

## 6. OpenList 与本地路径

- OpenList 运行时连接从数据库解析，热重载后读取最新激活连接；
- target 必须绑定明确的 connection/mount/revision 和 storage identity；
- Provider 原生路径、OpenList 虚拟路径、rclone remote path、本地 relay path 不得混用；
- 本地目标仅允许位于 `/storage-targets` 的受控挂载；
- 移动云中继仅允许位于 `/data/relay/job-<id>` 的任务目录；
- 写入前必须执行规范化、realpath containment 和符号链接/重解析点防护。

## 7. 一次性硬切换

本版本不提供以下兼容层：

- 旧 Provider 全局配置或环境变量回退；
- 旧 OpenList 全局 URL/Token；
- 旧分类保存路径和自动物化目标；
- V2/V3/V4 execution plan 或 `staging_plan` 回放；
- 旧 API 路由和旧单进程角色变量；
- `v1`、`ol:v1` 或旧通知密文解密。

数据库 migration 文件继续保留，以保证结构升级历史可追踪；保留 migration 不代表保留旧运行时行为。

升级前必须备份 `data/`。已有部署应把原账号主密钥的值原样改名为 `FNOS_ROOT_KEY`；若数据库中仍是
不受支持的旧密文格式，必须在后台重新登录账号、重新保存 OpenList 和通知凭据。

## 8. 最低验收

- 缺根密钥或进程角色时启动失败；
- 账号、OpenList、目标和 assignment 均可在后台配置并完成预检；
- 访客无法覆盖 assignment；
- 新任务只生成 V1 计划，计划 hash 可重复验证；
- 历史无效计划不会触发任何外部副作用；
- 四种策略均覆盖主流程、幂等重试、取消和失败转审核；
- Web/Worker 共用数据库与 relay，但仅 Worker 直接执行 rclone；
- `/readyz` 区分进程健康与业务尚未配置，不因没有保存位置阻止 Web 启动。
