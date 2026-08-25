# fnos-search 架构与审计报告

> 分支：`faat_0806_入库流程改造`（HEAD `4dd4f84`）
> 成文日期：2026-08-25
> 范围：仅当前分支。未参考其他分支及其提交记录。

## 0. 阅读约定

每条结论都带 `文件:行号`，可直接回查。结论按验证强度分三级，请据此决定信任程度：

| 标记 | 含义 |
|---|---|
| **[实测]** | 在本机运行代码复现过，有可观察输出 |
| **[读码]** | 通读相关源码确认，逻辑上成立，未构造运行时复现 |
| **[待复核]** | 由子任务审计得出，行号未逐条独立回查，动手前请先验证 |

本轮基线 **[实测]**：`python -m pytest tests/ -q` → **1034 passed + 194 subtests passed，206.95 秒，零失败**；`ruff check .`（项目配置只开 `E9/F63/F7/F82`）全绿。所以下文问题清单里的条目，都是现有测试与 lint 覆盖不到的地方。

---

## 1. 系统全貌

### 1.1 是什么

飞牛（FNOS）NAS 上的影视统一入库维护系统。一句话链路：**搜资源 → 转存到自己的网盘 → 搬到 NAS → 识别改名归位 → 通知**。

### 1.2 进程角色

`process_role.py:6` 定义四种角色，`role_runs()` 决定某组件是否在本进程激活：

```
all | web | scheduler | worker
```

`all` 全开；其余精确匹配。生产用 compose 拆成两个容器 **[读码]** `docker-compose.yml`：

| 容器 | 角色 | 职责 |
|---|---|---|
| `fnos-media-import` | `web` | HTTP 服务，端口 5251，不跑 rclone |
| `fnos-rclone-worker` | `all` | rclone 进程 + 后台 worker + 调度器 |

两者共享 `APP_DATA_HOST_PATH`（同一 SQLite）与 `FNOS_RELAY_HOST_PATH`（`/data/relay` 中继目录）。web 通过 `RCLONE_WORKER_URL` 调 worker 的 `/api/internal/rclone/*`；worker 反向通过 `APP_CALLBACK_URL` POST 回 `/api/callback/rclone`。

这个「同一镜像、按环境变量分角色」的设计很省心，但带来一个后果：**两个容器都完整加载了全部代码与全部路由**，包括 worker 容器也暴露着管理后台。安全边界只靠不映射端口，没有代码层面的路由裁剪。

### 1.3 安全基线

- 一把根密钥 `FNOS_ROOT_KEY`（32 字节 hex），按用途 HKDF-SHA256 派生子密钥 **[读码]** `accounts/crypto.py:20-32`：
  `provider-account` / `provider-auth-session` / `app-session` / `source-record` / `openlist` / `notification`。
  盐是常量 `b"fnos-search:root-key:v2"`（`crypto.py:32`）。信封为 AES-256-GCM（`crypto.py:85`）。
- 容器以 uid/gid 10001 运行，`cap_drop: ALL` 后只加回 5 个文件属主相关 cap，`no-new-privileges` **[读码]** `docker-compose.yml`。
- 入口脚本 `scripts/container_entrypoint.py:144` 行，root 阶段修正挂载目录属主后 `setgroups/setgid/setuid` 降权。

---

## 2. 代码地图

后端 `fnos_media_import/` **90920 行** Python，测试 **33780 行 / 96 文件**。

### 2.1 分包规模与职责

| 包 | 行数 | 文件 | 职责 |
|---|---:|---:|---|
| `services/` | 21078 | 90 | 应用服务层，见 §2.3 |
| `organizer/` | 10034 | 7 | 识别、改名、归位、回滚 |
| `transfer/` | 7579 | 14 | V1 传输编排与执行器 |
| `repositories/` | 6713 | 22 | 数据访问 |
| `importers/` | 5196 | 7 | 网盘适配（天翼/夸克/移动/六盘/通用） |
| `storage/` | 4067 | 6 | 存储目标与初始化向导 |
| `accounts/` | 3791 | 6 | 凭证加密、登录会话状态机 |
| `providers/` | 3704 | 9 | 搜索源与 Provider 注册表 |
| `planning/` | 3667 | 6 | 计划推导与校验 |
| `openlist/` | 3058 | 6 | OpenList 连接、挂载、WebDAV |
| `notifications/` | 2076 | 11 | 通知渠道与调度 |
| `cmcc/` | 1990 | 5 | 移动云独立客户端与上传器 |
| `media/` | 881 | 2 | 媒体元数据 |
| `updater/` | 873 | 3 | 自更新 |
| `blueprints/` | 689 | 18 | 路由注册（极薄） |
| `domain/` | 608 | 2 | 领域类型 |
| `search/` | 558 | 5 | 搜索聚合 |
| `classifiers/` | 128 | 2 | 链接分类 |

**四个空包** `infrastructure/` `domains/` `bootstrap/` `api/` 各 0 行 **[实测]** —— 分层重构留下的空壳，建议删掉，否则会持续误导「代码应该放哪」。注意 `domain/`（有内容）与 `domains/`（空）仅差一个字母，是真实的踩坑点。

### 2.2 顶层文件

`app.py` **4183 行** 是最大的单体 **[实测]**。它不是路由文件，而是**整个应用的装配根**：构造全部服务、定义 `admin_required`、定义大部分 view 函数、注册 18 个 blueprint。`blueprints/` 只有 689 行，因为它们只做 `add_url_rule`，handler 全在 `app.py` 里以闭包形式定义。

其余：`public_web.py` 1576（访客面）、`v1_cutover.py` 1057（一次性切换预检）、`config.py` 911、`openapi.py` 887、`config_persistence.py` 871、`database.py` 815、`web_input.py` 681、`content_guard.py` 559、`schema.py` 527。

`http_client.py` 只有 **38 行** —— 全项目对外 HTTP 的公共底座却如此单薄，见 §6 优化建议。

### 2.3 services/ 的真实结构 [待复核]

90 个文件实际是 7 组，多数是两个巨型门面拆出的协作者：

- **任务生命周期（12）**：`import_job_service.py`(984) 创建/重试、`job_service.py`(480) 只读时间线、`job_cancellation_service.py`(596) 取消状态机、`execution_plan_guard.py`(344) V1 计划守卫
- **rclone 组（19）**：`rclone_service.py`(2540) 门面 + 18 个协作者
- **追更（22）**：`update_service.py`(2389) 门面 + 20 个协作者
- **访客（14）**：`public_import_job_coordinator.py`(729) 提交补偿协调器 + 提交流水线
- **后台 worker（7）**：`durable_worker_runtime.py`(559)、`event_retention_worker.py`(367) 等
- **热榜（3）**、**配置与诊断（12）**

**命名与职责不符**，改代码前务必注意：

| 文件 | 名字暗示 | 实际 |
|---|---|---|
| `import_service.py`(280) | 入库主逻辑 | 不是；真正创建在 `import_job_service.py` |
| `job_service.py`(480) | 任务服务 | 只做时间线渲染，不写库 |
| `rclone_service.py`(2540) | rclone 搬运 | 已不搬运（数据面归 `transfer/`），实为进程调度器 |
| `rclone_file_retry_service.py`(57) | 重试实现 | 薄转发，实现在 `rclone_service.start_file_retry` |

---

## 3. 数据模型

**54 张表 [实测]**，分散在两个文件 —— 这是本项目最容易踩的认知陷阱：

- `schema.py` 29 张：业务运行态（`import_jobs`/`job_events`/`organizer_*`/`rclone_*`/`update_*`/`guest_*`/`resources`/`search_cache`/`app_settings`/`scheduler_leases`/`rate_limit_buckets`）
- `database.py` 25 张：V1 多链路地基（`provider_accounts` 及其 `_revisions`、`openlist_connections`/`_mounts`、`storage_targets`、`source_records`、`import_execution_plans`、`transfer_executions`、`provider_auth_sessions`、`worker_tasks`、`notification_deliveries`、`trending_*`）

**25 个迁移版本 [实测]**。`database.py` 有一条重要注释：版本号 2–5 曾被早期开发版占用，继续复用会让已有库误判「已执行」而跳过真实升级 —— 所以迁移从 1 直接跳到 6。加迁移时必须往后追加。

### 3.1 不可变 revision 模式

凡是存密文的表都配一张 `_revisions`：`provider_account_revisions`、`storage_target_revisions`、`openlist_connection_revisions`、`source_record_revisions` 等。语义是**永不 UPDATE 密文，只追加新 revision**。这让凭证轮换可回溯，也避免了「改一半」的中间态。

### 3.2 三表传输模型

| 表 | 语义 |
|---|---|
| `import_jobs` | 队列 + 对外状态投影（单向映射，`dispatch.py:1087`） |
| `import_execution_plans` | **不可变**计划，写入即算 `plan_hash`，读取时校验（`execution_repository.py:95/114`） |
| `transfer_executions` | 可变执行行：phase / receipt / attempts / resume_at / 三个 verified 标志 / claim 租约 |

拆分目的：计划一旦冻结，任何配置改动都不能改变已提交语义；执行行用 `row_version` 做 CAS；队列只是唤醒通道。**这是本项目最值得保留的设计，不要合表。**

### 3.3 租约表

`scheduler_leases` 被 5 个调度器共用（`event-retention` / `search-cache-maintenance` / `trending-discovery-scheduler` / `update-subscription-scheduler` / `notification-daily-digest`），靠 `INSERT ... ON CONFLICT ... WHERE owner_id=excluded.owner_id OR expires_at<=excluded.updated_at` 原子抢占 **[待复核]** —— 正确的乐观租约实现。

---

## 4. 核心流程

### 4.1 对外任务状态

`constants.py:34-49` 定义 16 个 job 状态：

```
created → provider_submitting → checking → submitted → waiting_transfer
→ transferring → waiting_openlist → waiting_organizer → organizing
→ confirming → review → refreshing → done
终态另有：failed / unsupported / cancelled
```

### 4.2 传输 17 阶段状态机 [待复核]

`transfer/__init__.py:86-122` 定义内部阶段，守卫表在 `execution_repository.py:30-49`：

```
planning ─┬─(needs_source_landing)→ source_materializing → source_waiting_visible ─┐
          └─(native)→ native_submitting → native_waiting ──────────┐              │
                              (receipt.completed 直接跳过)          │   transferring ←┘
                                                                   ↓        ↓
                                                          target_verifying
                                ┌──────organizer_enabled───────────┤
                          organizing                        finalizing（relay 跳过）
                                └──────────→ final_verifying ←──────┘
                                                   ↓
                          ack_pending ─(delete_source_after_ack)→ cleanup_pending → completed
                                     └─(否)──────────────────────────────────────────↑
异常 → retry_wait（有界退避回原阶段）/ manual_review / cancel_pending → cancelled / failed
```

每个阶段白名单都含自身（自环），`transferring`/`native_waiting`/`organizing`/`ack_pending` 等靠 `needs_wait` 反复唤醒。

### 4.3 执行器选路 [待复核]

`dispatch.build_executors`（:113-173）按 `plan.transfer.strategy` **单选，不做能力探测降级**：

| 策略 | 执行器 | 适用 |
|---|---|---|
| `native_save` | NativeTransferExecutor | 同 Provider 分享 → 同 Provider（强制 `source_kind==dest_kind`） |
| `native_offline` | 同上 | 磁链/种子 → **仅 sixpan** |
| `rclone_copy` | RcloneTransferExecutor | 跨盘 OpenList→OpenList/本地，**禁止 mobile/tianyi** |
| `relay_upload_provider` | ProviderRelayExecutor + CmccMobile/TianyiCloud Uploader | 跨盘 → mobile/tianyi |

relay 路径的关键约束：rclone 只负责下载到 `/data/relay/job-N`，**最终写入必须由 Provider 原生 API 上传器完成**，`destination_staging` 被显式禁止（`transfer/__init__.py:398`）。这是因为天翼/移动的 WebDAV PUT 会 405。

### 4.4 提交栅栏与 ACK 门 [待复核]

防重复转存的核心机制。进入 `source_materializing`/`native_submitting`/`transferring` 且无 receipt 时，**先持久化** `submission_fence={state:prepared, operation_key}` 再调外部 API（`dispatch.py:332-367`）。崩溃重启后若 fence 仍是 `prepared` 而 receipt 缺失 → 直接转 `manual_review`（`dispatch.py:335-339`），**绝不重试**。成功后置 `committed`（:398-410）。

ACK 三重门：计划快照必须等于冻结计划、`final_verified` 必须为真（`dispatch.py:1144-1148`）、清理还要 `target_verified and app_acked`（`cleanup.py:231`）。

### 4.5 失败三态 [待复核]

不是「成功/失败」二态，而是三态 —— 这个设计值得保留：

- **可重试**：`code in RETRYABLE_ERROR_CODES`（`__init__.py:151-161`），7 个瞬时类错误码
- **确定失败**：不可重试码 → 直接 `failed`
- **结果未知**：`RESULT_UNKNOWN` → `manual_review`，**永不自动重提**

退避按**阶段独立预算** `phase_attempts`（max 3），延迟 `min(300, max(2, 5*2^attempt))`。

### 4.6 登录会话状态机

`accounts/auth_sessions.py` 1436 行，4 个 handler **[读码]**：

| Handler | 行 | 方式 |
|---|---|---|
| `ManagedAccountAuthHandler` | :97 | 托管凭证 |
| `SixpanDeviceCodeAuthHandler` | :152 | 设备码 |
| `TianyiPasswordAuthHandler` | :433 | 密码 + RSA + 验证码 |
| `QuarkQrAuthHandler` | :640 | 扫码 |

`_safe_provider_message`（:26）对异常做脱敏，防 token/IP 泄进 UI。

### 4.7 计划错误分类

`planning/__init__.py` 定义 14 个 `PlanningError` 子类 **[读码]**：`SourceResolutionError`、`SourceAccountUnavailable`、`DestinationRequired`、`TargetNotFound/Disabled/NotSelectable/Incompatible`、`MagnetTargetRestricted`、`NativeTransferUnavailable`、`SourceLandingUnavailable`、`OpenlistUnavailable`、`UnsupportedPlanVersion`、`PlanValidationError`。细粒度错误类型让前端能给出准确提示，是好设计。

`plan_validator.py` 1539 行是 planning 包里最大的文件，比 `planner.py`(919) 还大 —— 校验比推导更复杂，说明约束繁多。

---

## 5. HTTP 面与鉴权

**207 条路由 [实测]**（实际枚举 Flask url_map 得出）：

| 类别 | 数量 | 鉴权 |
|---|---:|---|
| 管理 API `/api/*` | 162 | `admin_required`，注册时统一包裹 |
| 访客 API `/api/public/*` | 16 | 无需登录，有限流 + 可选验证码 + 体积限制 |
| 内部 API `/api/internal/*` | 13 | Bearer token + `hmac.compare_digest` |
| **回调 `/api/callback/rclone`** | **1** | **无** ← 见 §7.1 |
| 页面与探针 | 15 | 混合 |

鉴权模型澄清 **[实测]**：`grep '@admin_required'` 返回 0 次，容易误判「管理接口没保护」。实际是在注册 blueprint 时统一包裹：

```python
# blueprints/jobs.py:22
view_func=context.admin_required(context.handlers[endpoint])
```

这种写法**覆盖是完整的**，且比逐个装饰器更难漏。`admin_required` 定义在 `app.py:1250`，只检查 `session["admin_logged_in"]`。

唯一的全局 `before_request`（`app.py:1171`）只管公开接口的 JSON 体积上限，不做鉴权。

访客面防护 **[读码]**：限流 `rate_limit_service`（SQLite 桶）、客户端 IP 加盐哈希（`_hash_client_ip`）、简单验证码（`public_web.py:473-505`，`hmac.compare_digest` 比对）、请求体 256KB 上限、`content_guard.py` 559 行做内容风险评估。

---

## 6. 问题清单

### 6.1 高危

#### H1. `/api/callback/rclone` 完全无鉴权 **[实测]**

**这是本轮最严重的问题，已运行时确证。**

`blueprints/callbacks.py` 全文 22 行，只有 `add_url_rule`，无任何鉴权：

```python
# blueprints/callbacks.py:16-21
blueprint.add_url_rule(
    "/api/callback/rclone",
    endpoint="rclone_callback",
    view_func=context.rclone_callback,   # 裸 handler
    methods=["POST"],
)
```

handler（`app.py:3878`）直接把 body 喂给业务：

```python
def api_rclone_callback():
    body, status_code = callback_service.handle(request.get_json(silent=True) or {})
```

**实测复现**：用 Flask test client 不带任何认证头 POST 该端点，请求**没有被 401 拦住**，而是一路执行进业务逻辑，直到 `repositories/rclone_repository.py:505` 的 SQL 插入因外键约束失败才返回 500。同一进程内 `/api/internal/rclone/status` 不带 token 则干脆返回 401。worker 脚本 `http_callback_post`（`scripts/fnos_rclone_worker.sh:377`）发送的 payload 里也**不含任何 secret 或 Authorization 头**。

**为何危险**：默认值是 `http://127.0.0.1:5251/...`（`config.py:907`），单机时不可达外部。但独立 Worker 架构下 `APP_CALLBACK_URL` **必须**改成跨容器地址，此时端点就暴露在容器网络中。回调能推进状态机、触发 finalizer/Organizer、以及来源删除 ACK —— 伪造 `category_done` 可导致误删来源文件。

**修复方向**：复用 `rclone_worker_control.py` 已有的 Bearer + `compare_digest` 模式（`RCLONE_WORKER_CONTROL_TOKEN` 已存在，直接复用即可），并校验 `run_id` 确实属于该 job 的活跃 run。

#### H2. 两处 `TERMINAL_PHASES` 定义不一致，导致 failed/manual_review 无法恢复 **[实测]**

```
core TERMINAL_PHASES : ['cancelled', 'completed', 'failed']            # transfer/__init__.py:106
repo TERMINAL_PHASES : ['cancelled', 'completed', 'failed', 'manual_review']  # execution_repository.py:11
diff                 : ['manual_review']
```

矛盾点（实测打印白名单）：

```
whitelist[failed]        : ['failed','manual_review','native_submitting','planning',
                            'retry_wait','source_materializing','target_verifying','transferring']
whitelist[manual_review] : ['manual_review']
```

`failed` 的白名单**明确允许**转回 `retry_wait`/`planning` 等 —— 设计意图显然是「失败可人工重试」。但 `save()` 里有一道终态断言（`execution_repository.py` save 开头）：

```python
if execution.phase in TERMINAL_PHASES and next_phase != execution.phase:
    raise ExecutionConflictError(f"terminal execution cannot transition: ...")
```

因为仓库层的 `TERMINAL_PHASES` 含 `failed`，这道断言会把白名单允许的重试路径全部拦死。`reactivate()` 还额外显式拒绝 `manual_review`。结果：**failed 任务无法人工重试，manual_review 人工复核后没有任何出口**。

**修复方向**：让两处定义统一（推荐仓库层去掉 `failed`，`manual_review` 单独用 operator-only 的绕过路径），或给 `reactivate` 开专用通道。改动时务必先补测试 —— 现有 1034 个用例没有覆盖到这条路径。

#### H3. `ack_pending → final_verifying` 不在白名单，可能活锁 **[待复核]**

实测确认 `ack_pending -> final_verifying allowed? False`。据审计，`orchestrator.py:756` 在恢复到 `ack_pending` 但 `final_verified=False` 时会尝试转回 `final_verifying`，save 抛冲突 → deferred 5s → 无限循环。**动手前请构造复现验证。**

#### H4. `delete_source_after_ack=False` 时中继目录永不清理 **[待复核]**

据审计 `orchestrator.py:769-772`：relay 任务在该配置下直接 `ack_pending → completed`，跳过 `cleanup_pending`，`/data/relay/job-N` 残留。中继目录会暂存完整视频文件，长期运行会占满磁盘。建议**本地缓存清理与来源删除解耦**，始终走 `cleanup_pending`。

#### H5. ACK 失败无重试上限 **[待复核]**

据审计 `orchestrator.py:775`：`ack_service` 返回 `acked=False`（如计划快照不一致）时不走 `_handle_failure`，`phase_attempts` 不增长，延迟恒为 5s，无限轮询。应计入阶段预算，超限转 `manual_review`。

#### H6. 事件表保留策略只覆盖 5 张表 **[待复核]**

`event_retention_worker.py:30` 的 `EVENT_RETENTION_TABLES` 不含 `rclone_runs` / `update_runs` / `update_candidates` / `update_seen_items` / `update_path_snapshots`，这些表**全库无 DELETE**。`update_seen_items` 每扫一集写一行，`rclone_runs` 每次搬运一行 —— NAS 上长期运行会无界增长。

#### H7. 计划推导在 SQLite 写锁内执行 **[待复核]**

`import_job_service.py:236` 的 `BEGIN IMMEDIATE` 事务内包含 `_plan_import` + `_validate_plan`。若 `PlanValidator` 触达 OpenList/网盘目录探测（**需确认是否发起外部 IO**），写锁会被网络往返长时间独占，阻塞所有写请求。建议把推导移到事务外，事务内只做插入与哈希校验。

### 6.2 中危 [待复核]

| 位置 | 问题 | 建议 |
|---|---|---|
| `dispatch.py:491-501` | 心跳线程 `join(timeout=2)` 超时后仍存活并 bump `row_version`，末尾 save 冲突 → 已提交的外部操作 receipt 未落库 → 下轮 fence=prepared → 误转 manual_review | 续租前检查 stop 标志，超时即视为 lost |
| `dispatch.py:412` + `execution_repository.py:471` | `snapshot.pop("claim")` 使租约在写入瞬间释放，同 handler 内二次 save 会因「未被认领」失败 | claim 独立列 |
| `provider_relay_executor.py:256-304` | `metadata["step"]="upload"` 只在整次 upload 返回后才持久化，上传中崩溃后重启会重新 `_upload` → manifest=uploading → unknown → 必转人工 | 进入上传前先持久化一次 receipt |
| `provider_relay_executor.py:68` / `dispatch.py:87` | 认证熔断 `_halted_relay_account_ids` 是**进程内内存 set**，多 Worker 互不可见，重启即失效 | 落库到账号级熔断状态 |
| `mobile_relay_executor.py:252` / `tianyi_uploader.py:535` | 目标枚举硬上限 10000 文件 / 深度 32，**静默截断** → 大目录校验少算 → VERIFY_MISMATCH | 超限返回 unknown 而非部分结果 |
| 全 `transfer/` | 无任何超时与限流：`upload_local_dir` / rclone poll / mount list 均无 timeout，心跳无限续租 | 加超时，租约设最大续租次数 |
| `dispatch.py:302` | 每次唤醒重建全部装配：`validate_plan` + `build_executors` + `plan()` sha256 每 5s 重跑，长任务上万次 | `needs_wait=False` 时本地循环推进，缓存装配 |
| `callback_service.py:101` | 回调无重放/幂等键，同一 `(run_id,status,filename)` 可重复处理；脚本重试 3 次 | 加唯一约束或 seen-callback 集合去重 |
| `rclone_service.py:1653` | `_run_script` 逐行读 stdout **无整体超时** | 加 wall-clock deadline |
| `rclone_service.py:1314` | shutdown 只 cancel Timer，从不 join worker_thread，daemon 线程被硬杀 → `rclone_runs` 留在 running | 有界 join |
| `rclone_service.py:2058` | `_append_log` 把完整 rclone stdout 写入 `rclone_events`，**WebDAV url/user 会出现在输出中** | 加脱敏正则，至少过滤 `http(s)://user:pass@` |
| `durable_worker_runtime.py:274` | 心跳 lost 后仍等 handler 自然返回，期间另一 worker 已 claim → 同一副作用并发执行 | transition 前用 owner CAS |
| `settings_service.py:117` | `_advanced_update_lock` 是进程内锁，多进程角色下配置热更新无跨进程互斥 | 用 lease 或 CAS revision |
| `update_run_coordinator.py:36` | `subscription_locks` 字典只增不删 | WeakValueDictionary |
| `update_scheduler.py:97` | `status()` 两处 `except Exception: pass` 全吞异常，DB 故障时 UI 显示「正常」 | 至少记 `last_error` |
| `rclone_worker_client.py:194` | `retry_safe` 对 `POST /runtime/reload` 生效，重载超时后重试可能重复重建运行时 | 改为非重试或让 worker 幂等 |

### 6.3 低危

| 位置 | 问题 | 验证 |
|---|---|---|
| 全项目 | 放宽到全部 `F` 规则有 **68 处**：58 未使用 import、9 未使用变量、1 空 f-string | [实测] |
| `infrastructure/` `domains/` `bootstrap/` `api/` | 4 个空包，`domain/` vs `domains/` 仅差一字母 | [实测] |
| `dispatch.py:1023` | Windows 跨盘 `commonpath` 抛 ValueError 未捕获 → 冒泡为 unknown → manual_review | [待复核] |
| `dispatch.py:294,639` | `cleanup_completed_during_retry` 定义后从未使用 | [待复核] |
| `execution_repository.py:407-424` | `_operation_key_aliases` 每次 save 遍历全 phase 构造别名集合 | [待复核] |
| `tests/` 目录 | 残留 `_multilink_v1_*.db` / `-shm` / `-wal` 测试产物 | [实测] |

### 6.4 需要验证的隐患 [待复核]

`execution_repository.claim()` 经 `save()` 时会跑 `_validate_receipt_fence`。若历史行的 `receipt_json` 非空而 `snapshot_json` 缺 `receipt`，claim 将永久返回 None，worker 无限 deferred。当前写路径两者同步，**仅旧版本遗留行可能触发** —— 升级老库前值得写个体检脚本扫一遍。

---

## 7. 优化建议

### 7.1 最该拆的文件

| 文件 | 现状 | 建议 |
|---|---:|---|
| `organizer/service.py` | **8111 行**单类 | 全项目最大的技术债。按已有方法边界可切出：任务队列与 worker 接入、扫描与解析、TMDB 匹配、映射编辑与批量操作、apply 执行与回滚（`rollback_run:4094`）、目录稳定性判定（`reconcile:6316`/`fully_settled:6325`） |
| `app.py` | 4183 行装配根 | view 函数下沉到 blueprint，装配逻辑拆成若干 `wire_*` 模块 |
| `rclone_service.py` | 2540 行 | 清理规格推导（~330 行）移入已存在的 `rclone_cancelled_task_cleanup_service.py`；命名解析（1770-1990）独立；descriptor 校验（2067-2540）独立。剩 ~700 行才是真正的进程调度门面 |
| `update_service.py` | 2389 行 | TMDB 排期判定（~300 行）与 OpenList 路径推导（~450 行）是独立纯逻辑域，可无损抽出；尾部 40 个模块级 helper 并入 `update_values.py` |
| `storage/setup_service.py` | 2192 行 | 初始化向导，按步骤拆 |
| `plan_validator.py` | 1539 行 | 按约束族分组 |

### 7.2 可提取的重复 [待复核]

- **`LeasedPeriodicWorker` 基类**：`EventRetentionWorker` / `SearchCacheMaintenanceWorker` / `TrendingDiscoveryScheduler` / `NotificationScheduler` 四者结构完全一致（stop_event + daemon thread + 非阻塞 `_run_lock` + lease acquire/release + `_loop`），提取基类可消掉约 200 行重复，并统一租约释放语义 —— 目前 `EventRetentionWorker.shutdown` 故意不释放而其他三个释放，是不一致的。
- **job 栅栏三函数**：`_normalized_status` / `_safe_generation` / `_has_safe_not_started_provider_fence` 在 `job_cancellation_service.py:581`、`public_import_job_coordinator.py:696`、`import_job_service.py:952` **逐字重复**，应收进 `execution_plan_guard.py`。
- **`UpdateService.__init__` 130 行装配**：20 个协作者都用「构造时注入一堆 callable」，可用 `UpdateRunContext` dataclass 承载共享依赖压缩。
- **10 个薄封装文件**合计不足 500 行且各只有 1 个调用方（`rclone_worker_runtime.py` 24 行、`update_scheduler_runtime.py` 21 行、`rclone_process_controller` 43、`rclone_worker_command` 40、`rclone_run_state` 47、`update_run_coordinator` 41、`update_source_planner` 47、`update_values` 55、`rclone_run_completion_service` 55、`rclone_directory_mapping` 58），可折叠为对应门面的私有模块。

### 7.3 `http_client.py` 需要重写 **[读码]**

38 行的公共 HTTP 底座支撑着全部网盘调用，缺失：重试策略（`urllib3.Retry` on `HTTPAdapter`）、分离的 `(connect, read)` 超时元组、`get_json` 的 JSON 解析保护、显式代理策略。这是很多「偶发失败」的根因所在。

### 7.4 缺失的横向能力

- **按 Provider 的并发/限流**：完全没有。只有进程数和一个进程内熔断。网盘 API 都有速率限制，建议按账号信号量 + 令牌桶，跨进程共享。
- **密钥轮换**：`FNOS_ROOT_KEY` 无轮换路径。`crypto.py:112` 的 `_decrypt_with_candidates` 已经支持多候选密钥解密，管道是通的，只差 `FNOS_ROOT_KEY_PREVIOUS` 与读时懒惰重加密。
- **auth session 回收**：`provider_auth_sessions` 表里存着加密的明文密码，TTL 只在读时惰性判定，表无界增长。需要后台 reaper。

---

## 8. 值得保留的设计（勿误改）

改动这些地方前请先理解其存在理由，它们多数是生产事故换来的：

1. **三表传输拆分 + `plan_hash` 冻结**（`execution_repository.py`）—— 唯一能让配置变更不污染在途任务的机制。
2. **提交栅栏 fence=prepared 先落库再调外部 API**（`dispatch.py:332-367`）—— 唯一能让崩溃后不重复转存的机制。fail-closed 到 `manual_review` 是**故意的**，不要"优化"成自动重试。
3. **失败三态而非二态**（`__init__.py:151-161`）—— `RESULT_UNKNOWN` 独立成态，避免了「不确定是否已转存」时的盲重试。
4. **`import_jobs.idempotency_key UNIQUE` + 单事务五步写入** —— 失败零孤儿。
5. **`scheduler_leases` 的 ON CONFLICT 原子抢占** —— 正确的乐观租约。
6. **`job_cancellation` 的 CAS + 失败关闭** —— 对 `provider_submitting` 和缺栅栏的历史任务拒绝取消，是对的。
7. **`rclone_webdav_config_service` 用 stdin 喂参数**给 `rclone config create --obscure`，避开了命令行注入。
8. **HKDF 按用途派生子密钥**（`crypto.py:20-32`）—— 一把根密钥但密钥域隔离。
9. **relay 路径禁止 `destination_staging`**（`transfer/__init__.py:398`）—— 因为天翼/移动 WebDAV PUT 会 405，必须走 Provider 原生上传。
10. **`webhook.py` 的 SSRF 防护与 HMAC 签名**。
11. **receipt 只允许在 4 个交接点清空**（`execution_repository.py:18-25`），其余消失一律 fail-closed。

---

## 9. 开发建议

### 9.1 环境与命令 **[实测]**

```bash
# 全量测试（约 3.5 分钟）
python -m pytest tests/ -q          # 1034 passed + 194 subtests

# 注意：未装 pytest-timeout，带 --timeout 会直接报参数错误
# lint（项目只开 E9/F63/F7/F82）
ruff check .
```

Windows 开发用 `.venv`（Python 3.12.13，pytest 9.1.1）；容器是 Python 3.11。CI（`.github/workflows/docker-publish.yml`）只在 tag 推送时跑，门禁为 `pytest -q` + `ruff check .` + `node --check` 静态 JS + `docker compose config`。

### 9.2 前端现状（重要）

**这个分支上 `frontend/` 是空壳** **[实测]**：只有 `__tests__` 和 `router` 两个目录，无任何 `.vue` 文件，且整个目录未被 git 跟踪。真正在运行的前端是：

- `templates/` 6 个 Jinja 页面：`admin.html`、`admin_login.html`、`submit.html`、`request_status.html`、`notification_action.html`、`swagger.html`
- `static/` 23 个 JS 文件共 **14040 行**原生 JS（无构建步骤）

所以在本分支做前端改动就是改 Jinja + 原生 JS，不涉及 npm 构建。

### 9.3 动手顺序建议

若要在本分支继续开发，我建议按这个顺序处理上面的问题：

1. **先补 H1 回调鉴权** —— 影响面清晰、改动小（复用已有 token 机制）、风险最低，且是唯一的运行时确证漏洞。
2. **再修 H2 终态定义不一致** —— 但**必须先补测试**，覆盖 failed 重试与 manual_review 恢复两条路径。现有 1034 个用例完全没碰这里，改完没有安全网。
3. **H4 中继目录清理** —— NAS 磁盘会真实被占满，属于运维可见问题。
4. **H6 表保留策略** —— 同上，长期运行必然遇到。
5. **H3/H5 需先构造复现**再动手，我未独立验证。
6. **拆 `organizer/service.py`** 放到最后，且拆分应在有充分测试的前提下逐块进行。

### 9.4 已知的认知陷阱

- 表定义分散在 `schema.py`（29）与 `database.py`（25），共 54 张，别只数一个文件。
- 迁移版本 2-5 是禁用的历史坑位，加迁移往后追加。
- `domain/`（有内容）vs `domains/`（空包）。
- `import_service.py` 不是入库主逻辑；`rclone_service.py` 不再搬运数据。
- 管理接口鉴权在 blueprint 注册时包裹，`grep '@admin_required'` 返回 0 不代表没保护。

---

## 10. 本轮未覆盖

诚实说明边界，以下部分只做了结构统计，未做深度审计：

- `organizer/service.py` 8111 行的内部逻辑（识别准确性、回滚正确性、目录稳定性判定）
- `importers/` 三家网盘的协议细节与签名算法
- `cmcc/` 上传器状态机
- `notifications/` 渠道实现
- `updater/` 自更新链路
- `static/` 14040 行前端 JS
- 数据层 54 张表的索引与查询效率
- `search/`、`providers/` 搜索聚合与排序

其中「传输链路」与「服务层」两块是完整审计的（§6 的多数条目来自此），标注 `[待复核]` 的行号建议动手前回查一次。
