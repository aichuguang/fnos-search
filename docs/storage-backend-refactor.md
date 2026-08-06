# 存储目标可插拔化设计与执行方案

> 状态：已实施并完成代码验收  
> 版本：v0.3  
> 日期：2026-08-06  
> 范围：把“入库资源最终存到哪”从移动云盘定制，改造为可配置、可选择、可扩展的存储目标体系。  
> 首批目标：移动云兼容、任意 rclone remote、本地目录；后续目标：夸克、天翼云及其他存储。

## 1. 决策摘要

本次改造采用渐进式方案，不重写现有入库、rclone、Organizer 和回调链路。最终模型按三条轴拆分：

1. **来源入口（Source / Ingress）**：资源从哪里来、如何预览和落到可读取位置，例如夸克分享、移动云分享、天翼分享、六盘离线。
2. **目标存储（Destination / Storage Driver）**：文件最终写到哪个已配置实例，例如移动云主盘、夸克备用盘、天翼归档盘、本地影视库。
3. **传输策略（Transfer Strategy / Executor）**：来源与目标之间如何搬运，例如原生转存、rclone 复制、本地中继后调用自定义上传 API。

`destination_id` 是稳定的业务标识，`driver` 是实现类型。二者不得混用。

```text
source adapter
    -> ingress/landing
    -> ImportPlanner
    -> destination profile + StorageDriver capabilities
    -> native_save | rclone_copy | relay_upload | manual_review
    -> destination staging
    -> Organizer（按配置可选）
    -> final path / STRM / 媒体库刷新
```

## 2. 目标与非目标

### 2.1 必须实现

- 一个部署可配置多个目标实例，包括多个同类型账号或多个 rclone remote。
- 新任务可以选择一个管理员允许的 `destination_id`。
- 同一资源可以分别入库到不同目标，不能被错误判重。
- 每条任务在创建时冻结来源、目标、传输策略和四类路径，运行期间不受后台配置修改影响。
- 现有移动云任务、历史 staging plan、route 分发、Worker 回调和重试语义必须继续工作。
- 未知驱动、未知目标、越界路径、源目标重叠必须明确失败，禁止静默回退到移动云。
- 本地目标只能写入管理员预先挂载和配置的根目录，访客不能提交路径。

### 2.2 本轮不做

- 一条任务同时复制到多个目标。后续如需要，通过父任务派生多个单目标子任务实现。
- 开放访客提交 remote、绝对路径、driver 或凭证。
- 首批即实现夸克、天翼非官方上传 API。优先验证通用 rclone 目标。
- 首批建立完整的连接、目标、步骤三套数据库表。目标注册表先复用现有高级配置持久化。
- 动态安装第三方 Python 插件。当前“可插拔”指项目内注册驱动和配置实例。

## 3. 当前实现基线

以下现状决定了改造必须采用版本化兼容，而不是直接替换：

- `classifiers/link_classifier.py` 把来源识别和目标线路绑定在 `quark_to_mobile`、`cloud139_direct` 等 route 中。
- `services/import_job_service.py` 依赖 route 分发 Quark、Cloud139 和通用 importer。
- `services/import_staging_service.py` 使用 `STAGING_PLAN_VERSION = 2`，并对 route、`storage_backend`、任务路径进行严格校验。
- `services/rclone_service.py` 会从数据库重新读取 staging plan，并比较 `_staging_run_identity()`，拒绝临时参数覆盖持久化计划。
- `scripts/fnos_rclone_worker.sh` 当前主要是 relay 链路：从源 remote 下载到本地缓存，再走 `cmcc_api` 或 rclone/WebDAV 上传。
- `config_persistence.py` 通过 `ADVANCED_CONFIG_SECTIONS`、`SECTION_SCHEMA`、`CATEGORY_SCHEMA` 和 `_unknown_advanced_config_paths()` 严格限制可保存字段。
- `repositories/job_command_repository.py` 除 `idempotency_key` 外，还会按 `source_url + category + 非终态` 查重；该查询当前不区分目标。
- `schema.py` 中 `resources` 的唯一索引是资源缓存约束，不等价于入库任务的目标级幂等。

## 4. 领域模型

### 4.1 目标实例与驱动

```python
@dataclass(frozen=True)
class StorageTarget:
    destination_id: str
    name: str
    driver: str
    enabled: bool
    guest_selectable: bool
    config: dict[str, Any]
    categories: dict[str, "TargetCategoryConfig"]


@dataclass(frozen=True)
class StorageCapabilities:
    supports_native_save: bool = False
    supports_rclone_dest: bool = False
    supports_relay_upload: bool = False
    supports_quota: bool = False
    supports_organizer_access: bool = False
    supports_atomic_rename: bool = False
```

同一个 `driver="rclone"` 可以存在任意多个目标实例：

- `quark_main`
- `quark_backup`
- `tianyi_archive`
- `s3_backup`

### 4.2 StorageDriver 接口

驱动只理解自己的目标配置，不能接收 `route`、`cloud139`、`sixpan` 等其他供应商配置。

```python
class StorageDriver(Protocol):
    kind: str

    def validate(self, target: StorageTarget) -> list[str]: ...
    def capabilities(self, target: StorageTarget) -> StorageCapabilities: ...
    def resolve_paths(
        self,
        *,
        target: StorageTarget,
        category_key: str,
        job_id: int,
        staging_enabled: bool,
        staging_dir_name: str,
    ) -> "DestinationPathPlan": ...
    def worker_spec(
        self,
        *,
        target: StorageTarget,
        paths: "DestinationPathPlan",
        strategy: str,
    ) -> "WorkerSpec": ...
```

注册表接口：

```python
def get_driver(kind: str) -> StorageDriver: ...
def registered_driver_kinds() -> frozenset[str]: ...
```

未知 `kind` 必须抛出配置错误。注册表不得创建默认驱动，不得回落 `cmcc_api`。

### 4.3 来源适配器

来源适配器继续复用现有 importer，但对 Planner 暴露统一的来源描述：

```python
@dataclass(frozen=True)
class SourceDescriptor:
    kind: str
    ingress: str
    connection_id: str
    namespace_id: str
    supports_native_save_to: frozenset[str]
    supports_rclone_source: bool
    supports_download: bool
```

`cloud139_direct`、`sixpan_offline` 属于 `ingress` 或 `native_save` 策略，不是 StorageDriver。

## 5. 配置模型

首版目标注册表保存在现有高级配置中，不立即增加 `storage_targets` 数据库表。

```yaml
storage:
  enabled: true
  plan_version: 3
  default_target: mobile_main

  targets:
    mobile_main:
      name: 移动云主盘
      driver: cmcc_api
      enabled: true
      guest_selectable: true
      namespace_id: cmcc:primary
      native_ingress: cloud139_direct
      host: "https://personal-kd-njs.yun.139.com/hcy"
      auth_mode: web_basic
      access_token: "<secret>"
      phone: "<secret>"
      upload_mode: rapid_first
      rename_mode: auto_rename
      categories:
        movie:
          final_root: 电影
          organizer_namespace_id: openlist:main
          organizer_root: /移动云盘A/电影
        tv:
          final_root: 电视剧
          organizer_namespace_id: openlist:main
          organizer_root: /移动云盘A/电视剧

    quark_archive:
      name: 夸克归档盘
      driver: rclone
      enabled: true
      guest_selectable: false
      namespace_id: rclone:quark_dst
      remote: quark_dst
      categories:
        movie:
          final_root: 影视/电影
          organizer_namespace_id: openlist:main
          organizer_root: /夸克归档/影视/电影

    local_library:
      name: 本地影视库
      driver: local_fs
      enabled: false
      guest_selectable: false
      namespace_id: local:library
      root: /storage-targets/local-library
      categories:
        movie:
          final_root: Movies
          organizer_namespace_id: openlist:main
          organizer_root: /本地影视库/Movies
```

配置约束：

- `destination_id` 使用小写字母、数字、下划线和短横线，创建后不可通过 UI 改名。
- `driver` 只允许注册表中已知值。
- rclone remote 名称、namespace 和相对路径分别校验，不能把整段 `remote:path` 当作未解析字符串透传。
- `config_path` 由部署级配置提供，不允许请求级或目标级任意指定宿主机路径。
- `guest_selectable` 只控制访客入口；管理员仍可在任务开始前选择所有启用目标。
- Organizer 启用且任务要求整理时，目标必须提供可用的 `organizer_namespace_id + organizer_root`。
- Organizer 未启用时，不强制目标提供 OpenList 路径。

### 5.1 旧配置兼容

新配置的解析优先级：

```text
任务中冻结的 execution_plan
  > 请求中的 destination_id
  > storage.default_target
  > 由旧 RCLONE_UPLOAD_BACKEND / CMCC_UPLOAD_BACKEND 物化出的 mobile_main
```

旧 `cmcc_upload`、`cloud139`、`rclone.remote_name`、分类路径字段继续作为兼容输入。新代码将它们物化成只读的 `mobile_main` 目标快照，并记录弃用 warning；首批版本不删除旧字段。

### 5.2 config_persistence 必改项

`storage.targets` 是动态 key，不能简单塞进现有扁平 `SECTION_SCHEMA`。需要：

1. 把 `storage` 注册进 `ADVANCED_CONFIG_SECTIONS`。
2. 给 `_unknown_advanced_config_paths()` 增加专用 `_validate_storage_config_paths()`，允许动态 destination ID，但严格验证每层字段。
3. 给 `sanitize_advanced_config()` 增加 storage 专用清洗，不允许未知 driver 字段静默保留。
4. 扩展动态 secret 路径识别、掩码、占位符保留和 `clear_secrets` 校验，覆盖 `storage.targets.<id>.<secret>`。
5. 配置导入、导出、merge、replace 四条路径都必须有测试。
6. 任何无效配置必须返回明确错误，不能保存一半，也不能静默剥掉整个目标。

## 6. 路径与命名空间契约

### 6.1 四类路径

v3 不再使用 `provider_target_path` 表达全部路径：

```python
@dataclass(frozen=True)
class PathRef:
    namespace_id: str
    path: str


@dataclass(frozen=True)
class ExecutionPaths:
    landing: PathRef
    staging: PathRef
    final: PathRef
    organizer: PathRef | None
```

- `landing`：来源入口写入的位置，例如夸克自动保存目录或六盘离线目录。
- `staging`：目标端任务级暂存目录。
- `final`：目标端分类最终目录。
- `organizer`：Organizer/OpenList 可见的目标暂存目录，可为空。

### 6.2 重叠判断

路径身份是：

```text
(namespace_id, normalized_path)
```

规则：

- 只有 namespace 相同，才判断相等、父子包含和源目标重叠。
- namespace 不同但路径字符串相等是合法的。
- 同 namespace 下，`landing` 与 `staging/final` 相等或互相包含时拒绝执行。
- 本地路径必须经过 `resolve()` 后仍位于目标配置根目录内。
- 拒绝空路径、`.`、`..`、NUL、控制字符和不安全平台路径段。
- rclone remote 重叠按 `(remote identity, normalized path)` 判断，不能只比较 path。

## 7. Import Plan v3

新任务创建时必须先解析目标、规划策略，再把完整计划持久化。Worker 只接受数据库中已经冻结的计划。

```json
{
  "version": 3,
  "job_id": 123,
  "source": {
    "kind": "quark",
    "ingress": "quark_autosave",
    "connection_id": "quark_source",
    "namespace_id": "rclone:MP"
  },
  "destination": {
    "id": "tianyi_archive",
    "driver": "rclone",
    "namespace_id": "rclone:tianyi_dst",
    "config_revision": 7
  },
  "transfer": {
    "strategy": "rclone_copy",
    "executor": "rclone",
    "delete_source_after_ack": true
  },
  "paths": {
    "landing": {
      "namespace_id": "rclone:MP",
      "path": "离线下载/电影/job-123"
    },
    "staging": {
      "namespace_id": "rclone:tianyi_dst",
      "path": "影视/_入库暂存/电影/job-123"
    },
    "final": {
      "namespace_id": "rclone:tianyi_dst",
      "path": "影视/电影"
    },
    "organizer": {
      "namespace_id": "openlist:main",
      "path": "/天翼归档/影视/_入库暂存/电影/job-123"
    }
  },
  "postprocess": {
    "organizer_enabled": true,
    "strm_refresh_prefix": "/电视剧",
    "media_refresh_enabled": false
  }
}
```

持久化要求：

- `import_jobs.destination_id`：用于查询、筛选和幂等。
- `import_jobs.plan_version`：用于明确选择 legacy 或 v3 执行路径。
- `raw_data.execution_plan`：冻结完整 v3 计划和目标必要配置快照。
- 计划中不得保存明文 token、密码或可复用凭证；Worker 通过稳定目标 ID 和受控配置解析密钥。
- 修改目标配置不会改变在途任务的路径、策略或 namespace 身份。

## 8. Planner 能力矩阵

目标驱动至少返回以下能力：

- `supports_native_save`
- `supports_rclone_dest`
- `supports_relay_upload`
- `supports_quota`

建议同时返回：

- `supports_organizer_access`
- `supports_atomic_rename`
- `supports_server_side_copy`
- `supports_checksum`

Planner 选择顺序：

1. 来源适配器与目标驱动确认同盘原生转存可用，选择 `native_save`。
2. 来源可作为 rclone source，目标支持 rclone destination，选择 `rclone_copy`。
3. 来源可下载，目标支持自定义上传，选择 `relay_upload`。
4. 无安全、可验证策略时进入 `manual_review` 或直接返回不支持，不能猜测执行。

初始能力矩阵：

| 来源 | 目标 | 首批策略 | 说明 |
|---|---|---|---|
| Quark | mobile_main | `relay_upload` | 保持现有 Quark → 本地 temp → CMCC API |
| Quark | rclone target | `rclone_copy` | 新增源 remote → 目标 remote |
| Quark | local_library | `rclone_copy` | 目标 locator 为受控本地路径 |
| Cloud139 | mobile_main | `native_save` | 保持现有官方直转，不启动 rclone Worker |
| Magnet/Torrent | Sixpan 当前空间 | legacy ingress | 首批保持现状 |
| 其他跨盘组合 | 任意 | `manual_review` | 待来源读取能力和目标写能力验证后开放 |

“目标可配置”不等于所有来源和目标立即形成全连接矩阵。UI 只能展示 Planner 实际支持的组合。

## 9. Worker 执行契约

### 9.1 两种 Worker 模式

必须明确区分：

#### rclone_copy

```text
source remote:path
    -> rclone copy/copyto
    -> destination remote:path 或受控本地路径
    -> 大小/校验确认
    -> 应用完成回调 ACK
    -> 删除来源
```

rclone 管理来源到目标的复制。目标可以是任意已配置 remote 或本地绝对路径。

#### relay_upload

```text
source remote:path
    -> rclone 下载到应用管理的 local temp
    -> custom uploader CLI 上传
    -> 目标端确认
    -> 应用完成回调 ACK
    -> 删除来源与本地缓存
```

当前 `cmcc_api` 属于 `relay_upload`。不能为了抽象统一而改变其缓存、待确认标记或认证失败语义。

`native_save` 不启动 rclone Worker，由来源适配器提交并跟踪结果。

### 9.2 WorkerSpec

```python
@dataclass(frozen=True)
class WorkerSpec:
    mode: Literal["rclone_copy", "relay_upload"]
    source: PathRef
    destination: PathRef
    uploader: str | None
    expected_job_id: int
    delete_source_after_ack: bool
```

Worker 环境变量由持久化 plan 派生，仍须通过数据库身份核对。身份至少包含：

```text
job_id
plan_version
source namespace + path
destination_id
destination namespace + staging path
transfer strategy
```

### 9.3 退出码与删源红线

现有 custom uploader 退出码继续作为稳定契约：

| 码 | 含义 | 行为 |
|---|---|---|
| 0 | 上传并确认成功 | 等待应用 ACK 后删源 |
| 1 | 通用失败 | 保留源和本地缓存 |
| 2 | 已提交、结果待确认 | 写 pending marker，不立即重传 |
| 11 | 认证配置错误 | 回调 `auth_config_error`，中止本轮 |
| 12 | 认证过期 | 回调 `auth_expired`，中止本轮 |
| 20 | 秒传未命中 | 按配置决定失败或后续完整上传 |
| 88 | rclone 100% 但未确认 | 按待确认处理 |
| 143 | 停止请求 | 保留未确认内容 |

任何执行器都必须遵守：

- 未校验目标文件，不删源。
- 完成回调未 ACK，不删源。
- 取消和超时不能把“未知结果”写成失败后立即重试。
- 11/12 不盲重试，不继续下载后续文件。
- 支持原子 rename 时先写临时名再改最终名；不支持时使用现有 pending marker 和大小复查机制。

## 10. API 与界面契约

### 10.1 请求 API

请求只接受：

```json
{
  "destination_id": "quark_archive"
}
```

禁止接受：

- `driver`
- `backend`
- `remote`
- `root`
- `absolute_path`
- 凭证或 token

服务端校验：目标存在、已启用、当前调用方可选、分类已配置、Planner 支持来源与目标组合。

### 10.2 管理后台

新增“存储目标”管理面：

- 目标列表：名称、destination ID、driver、启用状态、访客可选、默认目标、健康状态。
- 目标编辑：连接参数、分类路径、Organizer 映射、媒体库刷新映射。
- 连接检测：认证、目录读取、临时写入/删除能力、容量；检测不得污染正式目录。
- 能力展示：原生转存、rclone 目标、自定义上传、配额、Organizer 可见性。
- 删除保护：已被任务或订阅引用的目标只能禁用，不能直接删除配置。

访客页面：

- 只有一个可用目标时不展示选择器。
- 多个目标时展示管理员允许且 Planner 支持的目标。
- 显示“原生转存”“跨盘搬运”“本地入库”等真实方式，不能把跨盘搬运描述成秒转。

管理员可在外部提交尚未发生前修改 `destination_id`。进入 `provider_submitting` 或任何实际执行状态后，不允许原地切换目标；需要取消并创建新任务。

## 11. 幂等与持久化

### 11.1 任务去重

当前实际存在两层任务去重：

1. `idempotency_key` 查询。
2. `JobCommandRepository.create()` 的 `source_url + category + 非终态` 查询。

两层都必须加入目标身份：

```text
source_url + category + destination_id + selection fingerprint
```

改造要求：

- `ImportJobCreationService.idempotency_payload` 加入 `destination_id`。
- active-job 查询加入 `destination_id`。
- 旧任务 `destination_id IS NULL` 只与 legacy 默认目标互相判重。
- 同一来源、同一分类、不同目标必须允许并行存在。
- 同一来源、同一分类、同一目标仍遵循当前非终态去重规则。

### 11.2 请求与追更

- 访客请求在审批前必须保留 `destination_id`，不能在审批时重新套用当时的默认目标。
- 管理员修改目标后，新的 destination 必须进入最终 job 幂等载荷。
- 追更订阅必须固定目标；默认目标变化不应把同一剧集后续内容写到另一块盘。
- 首版可以在现有 request/subscription JSON 中冻结目标；需要后台筛选时再增加显式列和索引。

## 12. 历史兼容与版本门控

### 12.1 Plan Reader

同一入口按版本选择执行器：

```python
if plan_version >= 3:
    return validate_and_execute_v3(plan)
return validate_and_execute_legacy_v2(job)
```

规则：

- 不批量改写历史任务的 raw_data。
- v2/无版本任务继续使用现有 route、`provider_target_path`、`storage_backend` 和 rclone 身份规则。
- legacy 校验继续接受历史部署曾写入的 `cmcc_api`、`webdav`、`sixpan_offline` 以及已知旧别名；它们作为不透明 legacy 值处理，不注册成 v3 StorageDriver。
- v3 计划禁止写入 `cloud139_direct`、`sixpan_offline` 作为 destination driver。
- route 在迁移期保留为兼容 shim。历史 job 按 route 分发；v3 job 由 execution plan 分发。
- 重试、恢复、补偿和 Worker 启动入口必须全部走同一版本门控，不能只有“新建任务”支持 v3。

### 12.2 数据库迁移

迁移采用纯新增字段：

```sql
ALTER TABLE import_jobs ADD COLUMN destination_id TEXT;
ALTER TABLE import_jobs ADD COLUMN plan_version INTEGER;
CREATE INDEX IF NOT EXISTS idx_jobs_destination_id
    ON import_jobs(destination_id);
```

不回填历史任务的虚构目标，不修改其 plan。查询展示时可把 NULL 显示为“历史默认目标”。

迁移前必须使用现有 SQLite backup API 生成备份。项目的迁移版本防降级会拒绝旧程序直接读取更高版本数据库，因此发布回滚需要同时恢复升级前数据库备份，不能只换回旧镜像。

## 13. 分阶段实施

### 阶段 1：兼容骨架，零行为变化

目标：建立 v3 类型、目标注册表和配置能力，但所有现有任务行为保持不变。

主要文件：

- 新增 `fnos_media_import/storage/types.py`
- 新增 `fnos_media_import/storage/registry.py`
- 新增 `fnos_media_import/storage/drivers/cmcc_api.py`
- 新增 `fnos_media_import/storage/drivers/rclone.py`
- 修改 `fnos_media_import/config.py`
- 修改 `fnos_media_import/config_persistence.py`
- 修改 `config/config.example.yaml`

实施要求：

- 从旧配置物化 `mobile_main`。
- 注册动态 storage 配置、动态 secrets 和严格未知字段验证。
- 现有 `storage_paths.py` 暂时保留，驱动先委托旧函数，禁止立即搬走全部逻辑。
- 增加路径计划奇偶性测试，覆盖 `cloud139_target_path -> cmcc_parent_path -> mobile_target_path -> label` 回退链。

验收：现有配置导入导出不丢字段；不启用 v3 时所有现有测试和任务行为一致。

### 阶段 2：Plan v3 与目标级幂等

目标：新任务可冻结 v3 计划，但默认仍写移动云。

主要文件：

- 修改 `fnos_media_import/schema.py`
- 修改 `fnos_media_import/update_schema_migrator.py`
- 修改 `fnos_media_import/repositories/job_command_repository.py`
- 修改 `fnos_media_import/services/import_job_service.py`
- 修改 `fnos_media_import/services/import_staging_service.py`
- 新增 `fnos_media_import/services/import_planner.py`

实施要求：

- 添加 `destination_id`、`plan_version`。
- 默认目标解析为 `mobile_main`。
- v2/v3 双 reader 和 route shim 落地。
- 新建、重试、恢复、补偿全部统一按 plan_version 分发。
- 幂等和 active-job 去重加入 destination。

验收：新旧任务可并存、重启和重试；同资源不同目标不冲突；伪造 plan 无法通过 DB 身份校验。

### 阶段 3：通用 rclone_copy

目标：支持 Quark 来源写入任意已配置 rclone remote。

主要文件：

- 修改 `fnos_media_import/services/rclone_service.py`
- 修改 `fnos_media_import/services/rclone_environment.py`
- 修改 `fnos_media_import/services/rclone_directory_mapping.py`
- 修改 `scripts/fnos_rclone_worker.sh`
- 修改 Worker 控制和诊断接口

实施要求：

- WorkerSpec 区分 `rclone_copy` 与 `relay_upload`。
- rclone copy 支持不同 source/destination remote。
- overlap 检查升级为 `(namespace/remote, path)`。
- source 和 destination remote 均从持久化 v3 plan 派生。
- 保留 legacy relay/WebDAV 路径，先通过测试再合并重复实现。

验收：Quark → mobile 仍走 relay；Quark → rclone target 走 rclone_copy；跨 remote 校验、取消、回调和删源语义正确。

### 阶段 4：本地目标

目标：把本地文件夹作为受控 rclone destination。

主要文件：

- 修改 `docker-compose.yml`
- 修改 `compose.dev.yaml`
- 修改 `.env.example`
- 修改 rclone driver 和路径校验

实施要求：

- 使用独立挂载根，例如 `/storage-targets/<destination_id>`，不能复用应用数据库目录 `/data`。
- 只允许目标配置中的固定容器路径。
- rclone copy 完成后校验大小；支持时采用临时名 + rename。
- Organizer 启用时要求该本地目录通过 OpenList 或受支持的本地 Organizer 路径可见。

验收：路径穿越、软链接逃逸、未挂载目录、只读目录均安全失败；成功任务不会残留 partial 文件或误删源文件。

### 阶段 5：管理 UI 与请求级选择

目标：管理员配置多个目标，访客或管理员按策略选择目标。

主要文件：

- 修改 `templates/admin.html`
- 修改 `static/admin-settings.js`
- 新增或扩展 settings/storage API
- 修改 `static/public-submit.js`
- 修改公共提交准备、审批和 coordinator 服务

实施要求：

- API 只接受 destination ID。
- UI 只展示有效、启用、调用方可选且能力矩阵支持的目标。
- 目标健康检查使用专门诊断接口。
- 在任务实际提交前允许管理员调整目标，提交后锁定。

验收：单目标界面无额外干扰；多目标选择、审批、回显、失败提示、最近请求和任务详情一致。

### 阶段 6：扩展来源和原生转存

目标：逐步开放 Cloud139、Cloud189、Sixpan 等来源到更多目标。

实施原则：

- 先增加能力和契约测试，再开放 UI 组合。
- 同盘原生转存优先于跨盘搬运。
- 非官方 API 作为独立 driver/executor 实现，遵循退出码、认证、待确认和删源契约。
- 新来源或目标不得向 Planner 添加组合 route 常量。

## 14. 测试计划

### 14.1 配置

- 动态 destination ID、driver 字段和分类路径校验。
- 动态 secret 保存、掩码、清除、merge、replace、导入、导出。
- 未知 driver、未知字段和非法 remote 拒绝。
- 旧配置物化 `mobile_main` 的奇偶性。

### 14.2 Planner

- 来源 × 目标 capability 矩阵。
- `native_save > rclone_copy > relay_upload > manual_review` 选择顺序。
- execution plan 不含凭证。
- Organizer 开关对 organizer path 必填规则的影响。
- namespace 相同/不同情况下的路径重叠判断。

### 14.3 兼容与持久化

- v2、无版本、v3 任务并存。
- 历史 `storage_backend` 和 route 回放。
- v3 请求参数与数据库计划身份不一致时拒绝。
- 同资源同目标去重、不同目标不去重。
- 重启、重试、补偿和取消都按 plan_version 走正确分支。
- SQLite 多进程契约和迁移备份。

### 14.4 Worker

- relay_upload 保持现有 CMCC 成功、待确认、11、12、20、88、143 行为。
- rclone_copy remote → remote、remote → local。
- `(namespace, path)` overlap 防护。
- 目标已存在且大小相同/不同。
- 完成回调未 ACK 时保留来源。
- 取消、超时、进程重启和本地缓存恢复。

### 14.5 UI 与部署

- 单目标、多目标、禁用目标、无可用目标。
- 访客不可选择管理员目标，不能篡改 destination ID。
- Compose 本地挂载和 Worker 隔离。
- 构建、健康检查和全部现有回归测试。

## 15. 发布、观测与回滚

发布前：

1. 备份 SQLite 数据库和高级配置导出文件。
2. 固定镜像版本，同时更新 Web 和 Worker 容器。
3. 先保持 `storage.enabled=false` 或只启用 `mobile_main`，验证行为奇偶性。
4. 再启用一个测试 rclone 目标，用小文件跑完整路径。
5. 最后开放本地目标和访客选择。

必须记录的事件字段：

- `destination_id`
- `driver`
- `plan_version`
- `transfer_strategy`
- source/destination namespace（不含密钥）
- target path（脱敏后）
- capability 决策结果
- Worker mode 和退出码

回滚：

- 配置或执行器问题优先禁用目标，保留任务和来源文件。
- v3 任务失败不得自动按 legacy 移动云配置重跑。
- 数据库迁移后回滚旧镜像时，同时恢复升级前数据库备份。
- 不删除历史配置别名和 legacy reader，至少跨一个稳定发布周期后再评估清理。

## 16. 完成定义

本改造完成必须同时满足：

- 默认安装和旧安装升级后，未选择新目标时行为与当前移动云流程一致。
- 至少支持一个移动云目标、两个不同 rclone 目标和一个本地目标同时存在。
- 同一 driver 可配置多个独立 destination ID。
- 同资源可以分别提交到两个目标，任务和状态互不覆盖。
- Worker 能明确区分 `rclone_copy` 和 `relay_upload`。
- v2 历史任务能够继续恢复、重试和完成。
- 访客无法控制路径、remote、driver 和凭证。
- Organizer 路径要求随 postprocess 配置生效，不误杀合法同名跨 namespace 路径。
- 全部新增契约测试及现有回归测试通过。
- README、配置示例、部署挂载、升级和回滚说明同步完成。

## 17. 已锁定决策

以下结论不再作为实施阶段的开放问题：

1. `backend kind != destination instance`，配置和请求统一使用稳定 `destination_id`。
2. 来源、目标存储和传输策略三轴分离。
3. `cloud139_direct`、`sixpan_offline` 不注册为 v3 目标驱动。
4. 请求只提交 destination ID，不提交 backend/root。
5. 路径使用 `(namespace_id, normalized_path)` 判断身份和重叠。
6. 本地目标复用通用 rclone executor，不另建一套 cp/install 大文件链路。
7. v2 plan 与 route 保留兼容 reader，不批量改写历史任务。
8. Worker 同时保留 `rclone_copy` 和 `relay_upload` 两种模式。
9. 幂等和非终态查重必须包含 destination ID。
10. 首版每条任务只允许一个目标。

本文件是后续实施、评审和验收的基线。若实现过程中需要改变已锁定决策，应先更新本文档并记录原因、兼容影响与迁移方式。

## 18. 实施验收记录（2026-08-06）

六阶段代码已落地，并按 v2 历史任务、v3 新任务、重试、重启恢复、取消、回调、Organizer、公共提交、热榜首次入库和追更链路完成复核。验收时补充加固了以下生产边界：

- v3 计划同时绑定任务 ID、plan version、destination ID、来源/目标 namespace、任务级路径、driver、strategy 和 executor，任一身份不一致均失败关闭。
- 多个 `cmcc_api` 目标分别从 `destination_id` 解析凭据；运行时重载会同步刷新目标注册表。目标只有显式声明 `native_ingress: cloud139_direct`，才会与 Cloud139 来源适配器判定为同账号 `native_save`，因此目标 ID 与 namespace 均可独立改名。
- 公共提交未指定目标时，服务端解析默认目标并同样执行 `enabled + guest_selectable` 校验；非访客目标不能通过默认回落被选中。
- `native_ingress` 会冻结进 v3 execution plan。读取校验器要求它与 `source.ingress` 一致；仅为修复前已持久化的 `mobile_main + cmcc:primary` 原生计划保留兼容读取。
- `rclone_copy` 与 `relay_upload` 都消费冻结的删源策略；目标校验完成且应用回调 ACK 前不删除来源。
- 未启用 Organizer 暂存时，Worker 仍写入 final root 下的 `job-N` 任务目录，不会把数据遗留在 `_入库暂存`。
- 管理端连接检测对 rclone remote 执行目录读取、唯一临时目录创建/删除和容量探测；对本地目标执行受控挂载检查及唯一临时文件读写删除。
- 当前 CMCC SDK 仅能安全执行认证和根目录读取检测，尚无可确认无残留的删除探测和容量接口；界面将这两项明确标记为未支持，不计为虚假通过。

自动化验证结果：`unittest` 876 项通过，pytest 风格函数 9 项通过；Ruff、Python compileall、管理端 JavaScript 语法、Worker Bash 语法和 Git whitespace 检查通过。
