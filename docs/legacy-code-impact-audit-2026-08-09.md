# 旧代码影响审计报告（基线与整改闭环）

- 审计日期：2026-08-09
- 审计分支：faat_0806_入库流程改造
- 审计基线：c454fa3
- 审计对象：当前工作区已暂存的迭代快照，共 240 个文件变更，约 44,003 行新增、22,050 行删除
- 基线结论：高风险技术债，需要分阶段切断；以下第 1～9 节保留实施前证据，当前结论以第 10 节整改复核为准

> 本文前半部分是 2026-08-09 实施前的历史基线，不代表当前工作树仍存在对应问题。

## 1. 执行摘要

本次检查确认：仓库里不只是存在“没有删除的死代码”，而是同时存在三种情况。

1. **旧语义仍参与当前业务决策。** 最明显的是旧 route 字符串仍参与搜索排序、公开页文案、后台任务展示和部分旧任务判断；而 V1 新任务已经把 target_route 写成 provider:source_form，两套契约正在漂移。
2. **旧实现仍是新架构的执行核心。** mobile、quark、tianyi、sixpan 四个内置 Provider 的 V1 执行仍统一经过 LegacyNativeAdapter，转存、轮询、校验、取消和来源物化都受旧 importer 接口影响。
3. **新旧装配并存。** RuntimeBuilder 先构造旧形态服务和空壳 importer，app.py 再修改对象内部字段，把它们接到 V1；当前主入口能工作，但任何漏掉二次接线的新入口、测试入口或后续重载路径，都可能得到半装配运行时。

综合判断：

- 当前不能把问题简单理解为“删掉几个没人调用的文件”。
- 第一优先级应是让旧字段和旧实现不再影响新任务，再删除兼容壳。
- 数据库迁移、旧配置拒绝、历史任务 fail-closed 诊断等代码属于升级安全层，必须与业务旧实现区分，不能一并删除。

## 2. 审计方法与可信度

本次采用以下方式交叉检查：

- 使用 rg 检索旧版本、legacy、retired、deprecated、compatibility、旧 route 常量及生产引用。
- 使用 Python AST 做类方法引用、Database façade 调用和模块引用的启发式分析。
- 检查 app.py、RuntimeBuilder、Provider Registry、Planner、Transfer、Storage、前端脚本之间的真实接线。
- 在独立 Python 进程中检查模块导入图和冷导入耗时。
- 执行 compileall、Ruff 和 59 个定向测试。

结论分为三类：

- **已确认当前影响**：存在明确生产调用或运行时接线。
- **高可信删除候选**：没有生产引用，但仍需由回归测试兜底。
- **历史兼容层**：只服务升级、历史数据或诊断，必须设置退役条件后再删。

本次没有生产数据库、线上日志和真实 Provider 账号，因此无法确认历史任务数量，也没有执行外部云盘真实转存。涉及历史数据的删除必须先做数据盘点。

## 3. 风险总览

| 编号 | 等级 | 发现 | 当前影响 |
| --- | --- | --- | --- |
| LEG-01 | 高 | 旧 route 语义仍参与搜索、页面和任务判断 | 已直接影响当前搜索排序、展示语义，并与 V1 新任务字段格式冲突 |
| LEG-02 | 高 | LegacyNativeAdapter 仍承载四个 Provider 的正式执行 | 所有内置 Provider 的核心转存生命周期仍依赖旧 importer 协议 |
| LEG-03 | 高 | RuntimeBuilder 构造旧壳，app.py 事后二次接线 | 运行时正确性依赖接线顺序，存在半装配和未来入口漏接风险 |
| LEG-04 | 中 | V3 Storage 包在导入 V1 服务时被自动加载 | 旧 registry、types 和两个 driver 每次随 storage 包加载 |
| LEG-05 | 中 | ImportService 保留不可达的旧 Provider 提交链 | 形成虚假依赖，维护者可能修改到不会执行的旧路径 |
| LEG-06 | 中 | retired planner/staging façade 仍被生产代码使用 | 已退役模块无法真正删除，旧 API 继续成为调用入口 |
| LEG-07 | 中 | Deprecated DatabaseDomainMixin 仍是主要数据库 API | 生产层大量通过旧 façade 调用，仓储拆分尚未完成 |
| LEG-08 | 中 | app.py 兼容 re-export、未注册 handler 和测试依赖锁住旧边界 | 扩大导入图，重复实现继续存在，测试阻止模块退役 |
| LEG-09 | 中 | 新旧竞争实现和重复 Protocol 并存 | ActiveProviderImporter、真实探测服务未接线，接口真源不唯一 |
| LEG-10 | 低 | 临时审计/测试文件已进入暂存区 | 污染提交记录，并可能进入镜像或发布包 |

## 4. 详细发现

### LEG-01：旧 route 字符串仍影响当前行为

**证据**

- fnos_media_import/classifiers/link_classifier.py:56-120 仍为来源返回 quark_to_mobile、cloud139_direct、cloud189_direct、sixpan_offline，并附带旧线路说明。
- fnos_media_import/services/public_resource_detail_service.py:50-77 将该 route 直接返回公开资源详情。
- fnos_media_import/search/aggregator.py:240-324 使用 route tier 给搜索结果增加最多 20 分，并影响重复结果保留顺序。
- static/public-submit.js:133 与 static/public.js:261-268 使用旧 route 生成“快速入库、云端处理、自动入库”等文案。
- static/public-search.js:198-252 使用 route 识别 Quark、139、天翼和 SixPan 候选。
- static/admin-jobs.js:345-353 只按 sixpan_offline 判断“仅媒体库刷新”任务；static/admin-jobs.js:661 又按 quark_to_mobile 判断中转路径展示。
- fnos_media_import/services/job_cancellation_service.py:445-453 仍以 sixpan_offline 作为历史 SixPan 任务的兜底判断。该分支同时检查 submitted、external_task_id 和 source_type，更接近应暂留的历史兼容，而不是新任务的正式路由逻辑。
- V1 新任务实际在 fnos_media_import/services/import_job_service.py:322-340 创建，并在同文件 872-873 将 target_route 写为 provider_kind:source_form，例如 quark:share、sixpan:magnet。

**影响**

1. 搜索排序仍基于旧“线路便利度”，而 V1 的真实目标由数据库 assignment 和执行计划决定。搜索结果可能因为已经失效的旧路由假设而被提前。
2. 公开页展示的是分类器推测的旧线路，不是管理员 assignment 最终选出的目标和策略，用户可能看到错误的“入库方式”。
3. 新任务使用 sixpan:magnet 后，后台仅判断 sixpan_offline 的刷新提示可能不再出现。
4. target_route 同一字段同时表示旧执行路线和新来源描述，字段契约已经失去单一含义。

**建议**

- 将来源分类结果收敛为 provider_kind、source_form、search_source_id，不再让 route 参与业务决策。
- 搜索排序改用明确的 source/provider 能力和管理员配置，不读取旧 route 字符串。
- 页面展示从冻结执行计划读取 strategy、destination_id、destination_provider_kind。
- 历史任务确需识别旧 route 时，集中放入单独的 legacy_job_compat 模块，并只对 plan_version 为空或旧版本的任务生效。

### LEG-02：LegacyNativeAdapter 仍是四个 Provider 的核心

**证据**

- fnos_media_import/app.py:3697-3737 使用 LegacyNativeAdapter 注册 mobile、quark、tianyi、sixpan。
- fnos_media_import/transfer/native_executor.py:371-923 的适配器内部包含大量 self.kind 分支。
- mobile、quark、tianyi、sixpan 在账号检测、native_save、native_offline、poll、verify、cancel、materialize 等阶段走不同旧接口翻译。
- fnos_media_import/providers/base.py 已定义正式 ProviderAdapter 协议，但仓库内没有四个内置 Provider 各自的正式 Adapter 实现。

**影响**

- 新协议表面已经建立，核心执行实际仍由一个巨型兼容适配器完成。
- 新增 Provider 或修改单一 Provider 时容易影响其他分支。
- Provider 能力声明与旧 importer 是否恰好具有某个方法之间存在隐式耦合。
- LegacyNativeAdapter 不能删除，因为当前生产运行时仍直接依赖它。

**建议**

依次实现 MobileProviderAdapter、QuarkProviderAdapter、TianyiProviderAdapter、SixPanProviderAdapter。每个适配器独立实现账号检测、提交、轮询、校验、取消和能力声明；四个适配器全部接入并通过契约测试后，再删除 LegacyNativeAdapter 中对应分支，最后删除类本身。

### LEG-03：运行时依赖二次接线

**证据**

- fnos_media_import/runtime_builder.py:197-253 构造 ActiveQuarkImporter、空配置 Cloud139Importer、TianyiImporter、SixPanOfflineImporter 和旧形态 ImportService。
- fnos_media_import/app.py:3879-3929 的 _wire_v1_runtime_build 随后修改 job_creation/job_retry 的 source_resolver、destination_resolver、plan_import、validate_plan、execution_repository、enqueue_transfer 等内部字段。
- fnos_media_import/app.py:1932-1954 的当前热重载路径会调用 rewire，因此主入口暂时有保护；但正确性仍依赖调用者记得执行第二阶段。

**影响**

- RuntimeBuild 在 build 返回时并不是一个可立即使用的完整 V1 对象图。
- 单元测试、新 CLI、后台脚本或未来启动入口若只调用 RuntimeBuilder.build，会得到不可用或行为不同的服务。
- 热重载增加任何新依赖时，必须同时维护 builder 和 rewire 两处。

**建议**

- RuntimeBuilder 构造函数直接接收 V1 resolver、planner、validator、execution repository 和 enqueue callback。
- build 返回前完成全部依赖注入，并增加“构建后立即可创建 V1 任务”的测试。
- 删除 app.py 对服务内部字段的逐项赋值，保留一次性注册和生命周期管理。

### LEG-04：旧 V3 Storage 子系统仍自动加载

**证据**

- fnos_media_import/storage/types.py:1 明确标注为 v3 领域类型。
- fnos_media_import/storage/registry.py 自动注册 cmcc_api 和 rclone driver。
- fnos_media_import/storage/__init__.py:1-10 无条件导入旧 registry 和旧 types。
- 当前 V1 StorageTargetService 使用 fnos_media_import/domain/models.py 中的新 StorageTarget，而不是旧 storage/types.py。
- 生产代码对旧 registry 的直接使用基本不存在；tests/test_storage_local_target.py 仍锁定旧 driver API，StorageTargetProbeService 也仍依赖旧类型。

**动态验证**

- 仅导入 fnos_media_import.storage.target_service，就同时加载 storage.registry、storage.types、storage.drivers.rclone、storage.drivers.cmcc_api。
- 本机一次独立进程导入约 0.101 秒，共加载 126 个模块。

**影响**

- V1 目标服务无法与 V3 driver 生命周期解耦。
- 旧 driver 的导入副作用和依赖会进入所有使用 storage 子包的进程。
- 测试仍把旧 API 当作正式能力，导致旧模块长期无法退役。

**建议**

- 将 storage/__init__.py 改为无副作用入口，不再自动注册旧 driver。
- 若仍需短期保留，将旧实现移动到 storage/legacy_v3，并禁止 V1 模块从该包导入。
- 先迁移或删除 tests/test_storage_local_target.py 和旧探测服务对 storage.types 的依赖，再删除 registry/drivers/types。

### LEG-05：ImportService 旧提交链仍保留

**证据**

- fnos_media_import/services/import_service.py:134-683 仍包含 _submit_quark_job、_submit_cloud139_job、_submit_generic_job、_generic_importer、_provider_submission_ready、_write_provider_result 等旧流程。
- ImportService.__init__ 仍把旧回调传给 ImportJobCreationService 和重试服务。
- fnos_media_import/services/import_job_service.py:191-216 仍接收并保存 target_path、staging_plan、submit_quark、submit_cloud139、submit_generic，但当前 V1 创建路径不读取这些字段。
- AST 启发式引用分析显示，ImportService 中约 21 个方法只属于旧调用链或没有生产可达入口。

**影响**

- 构造函数显示的依赖与真实执行依赖不一致。
- 开发者可能修复 _submit_*，但线上任务实际已经通过 V1 Transfer 执行，修改不会生效。
- 旧回调仍可被未来误接回主链，重新引入已废弃的 Provider 直提交流程。

**建议**

先增加 V1 创建、重试、转存、完成回调的契约测试，再删除未读取的构造参数和旧 _submit_* 链。保留仍被 SixPan 完成同步、媒体刷新等现行流程调用的方法，不按文件整体删除。

### LEG-06：retired planner/staging façade 仍在生产使用

**证据**

- fnos_media_import/services/import_planner.py:1 自称 former planner 的 V1-only compatibility façade。
- fnos_media_import/app.py 仍导入 execution_plan_from_job 和 validated_execution_plan_from_job；后者在 fnos_media_import/app.py:1221、1323、1612、1649、1687 使用。
- execution_plan_from_job 被导入但没有生产调用。
- fnos_media_import/services/import_staging_service.py:1 自称 retired staging façade。
- fnos_media_import/app.py:1371 仍通过该 façade 使用 map_execution_path_to_organizer；ImportStagingService 和其余旧 staging API 基本没有生产引用。

**影响**

- 已退役模块仍是正式依赖，模块边界无法收敛。
- 调用者难以判断应该使用 fnos_media_import/planning/job_plan.py 还是 fnos_media_import/services/import_planner.py。

**建议**

- 将 validated_execution_plan_from_job 调用直接迁到 fnos_media_import/planning/job_plan.py。
- 将 map_execution_path_to_organizer 移到当前路径模型所属模块。
- 删除未使用 import，再删除 façade 和对应兼容测试。

### LEG-07：Deprecated Database façade 仍被广泛调用

**证据**

- fnos_media_import/repositories/database_domain_mixin.py:1 明确标记 Deprecated。
- 该类运行时动态注入约 102 个 callable，其中约 89 个是公开兼容方法。
- fnos_media_import/database.py:31 的 Database 仍继承 DatabaseDomainMixin。
- AST 启发式扫描在生产 Python 中找到 239 处对 61 个已弃用 mixin 方法的直接调用。
- 调用最集中于 organizer/service.py、services/update_service.py、services/rclone_service.py 和 app.py。
- tests/test_database_domain_mixin_compat.py 明确要求当前 Database 继续保持旧 MRO 和转发行为。

**影响**

- Repository 已拆分，但服务层仍看见一个全能 Database 对象。
- 依赖边界不清，测试难以只替换一个仓储。
- 只要生产调用和兼容测试存在，DatabaseDomainMixin 就不能安全删除。

**建议**

按模块迁移，而不是一次性重写：

1. OrganizerService 注入 OrganizerRepository。
2. Rclone 服务注入 RcloneRepository。
3. Update 服务注入 UpdateRepository、UpdateRunRepository 和查询仓储。
4. app.py 只在 composition root 创建仓储，不把 Database façade 传入业务服务。
5. 生产调用归零后删除兼容测试和 DatabaseDomainMixin 继承。

### LEG-08：app.py 兼容表面继续锁住旧 API

**证据**

- fnos_media_import/app.py:173-295 从 media_serializers、path_planning、public_web、web_input 批量 re-export 私有 helper。
- tests 中仍有 10 处 from fnos_media_import.app import，其中多数导入私有 helper 而不是直接导入真实所属模块。
- fnos_media_import/app.py:4896-4984 至少定义了 14 个没有注册正式路由的旧 handler。
- fnos_media_import/app.py:5195-5202、5300-5301、5329 仍把其中 11 个 handler 以 legacy_* key 传给 Blueprint。
- jobs、media、rclone Blueprint 只按正式 route 表读取 handler，完全不消费 legacy_* key。
- 独立进程导入 fnos_media_import.app 三次耗时约 1.53-1.61 秒，每次加载约 861 个模块。该数字不是全部由 re-export 造成，但说明 app 模块已经成为非常宽的依赖入口。

**影响**

- 已迁移 helper 的旧导入路径继续被测试固化。
- 重复 handler 看起来像仍有路由，容易让维护者误判。
- 导入 app 只为测试一个纯函数，会加载完整应用依赖图。

**建议**

- 先迁移测试到 helper 的真实模块。
- 删除未消费的 legacy_* handler map 项和无引用闭包函数。
- app.py 的公开 API 最终只保留 create_app 及明确的应用入口。

### LEG-09：竞争实现和重复接口真源

**证据**

- fnos_media_import/providers/active.py 中的通用 ActiveProviderImporter 没有生产引用；运行时仍使用 Quark 专用 ActiveQuarkImporter。
- fnos_media_import/services/storage_target_probe_service.py 只有测试引用，没有接入生产管理 API。
- 当前 StorageTargetService.test_target 只做结构校验；孤立的 ProbeService 会执行真实 rclone/CMCC 读写检测，两者同名“测试目标”却语义不同。
- fnos_media_import/planning/__init__.py:232-244 和 fnos_media_import/providers/base.py:9-24 各定义了一套 ProviderAdapter Protocol。
- DefaultAdapterRegistry 只是 ProviderRegistry 的兼容包装，但 app.py 仍使用旧名称。

**影响**

- 新实现可能被误认为已经上线，实际生产仍走旧实现。
- 同一能力存在两种接口和两种测试语义，后续修改容易只改到其中一边。

**建议**

- 对 ActiveProviderImporter 和 StorageTargetProbeService 做明确决策：接入并替换旧实现，或删除，不继续保留孤立代码。
- ProviderAdapter 只保留 fnos_media_import/providers/base.py 一处定义。
- composition root 直接使用 ProviderRegistry；若保留错误转换，改成独立适配函数而不是再定义一个 Registry 类型。

### LEG-10：暂存区包含临时文件

当前暂存区包含：

- .test-tmp/psdir/x
- .test-tmp/pydefault-5a1f55a8/x
- _audit_tmp_20260808/probe.txt

.gitignore 只忽略 audit_tmp_local/，没有覆盖 .test-tmp/ 和 _audit_tmp_*。

建议在提交前从暂存区和工作树移除这些文件，并补充最小忽略规则，避免测试运行再次污染提交。

## 5. 删除分类

### 5.1 可以优先删除

以下项目风险较低，但仍应在同一提交中执行回归测试：

- app.py 中 Blueprint 不读取的 legacy_* handler map 项。
- 仅被上述无效 key 引用、没有正式路由的旧 handler。
- app.py 中未使用的 execution_plan_from_job import。
- 暂存区中的三个测试/审计临时文件。

### 5.2 必须先迁移再删除

- 旧 route 字符串及其前端、搜索、任务判断消费者。
- LegacyNativeAdapter。
- RuntimeBuilder 之后的 app.py 二次接线。
- ImportService 的旧 Provider 提交链和未读取回调。
- import_planner.py、import_staging_service.py façade。
- storage V3 registry、types、drivers 及锁定它们的测试。
- DatabaseDomainMixin 和 Database 上的旧转发 API。
- app.py 私有 helper re-export。
- 重复 ProviderAdapter Protocol、DefaultAdapterRegistry 兼容名。

### 5.3 当前必须暂留的升级兼容层

以下代码即使包含 legacy、old、migration 等字样，也不应按死代码直接删除：

- 数据库 schema migrations、旧表补列、迁移版本记录和升级前备份。
- config.py 对已删除配置文件字段和旧环境变量的显式拒绝。
- config_persistence.py 对数据库中存量高级配置的一次性清理。
- execution_plan_guard.py 对旧任务的 fail-closed 拒绝和人工审核诊断。
- fnos_media_import/planning/job_plan.py 对 plan_version 和冻结计划完整性的严格校验。
- 历史 Cloud139 submitted 任务对账、历史 route 识别等逻辑，直到生产数据确认旧任务已经清零。

这些兼容层的正确退役方式是：增加使用计数或数据查询，设定保留窗口，确认连续一个版本周期为零后删除。

## 6. 推荐整改顺序

### 阶段 1：先停止旧语义影响新任务

1. 禁止旧 route 参与搜索排序和重复结果选择。
2. 公开页与后台页改读 source_form、Provider、执行计划 strategy 和 destination。
3. 将历史 route 判断限制在旧 plan_version 任务。
4. 补充契约测试：新任务不得生成 quark_to_mobile、cloud139_direct、cloud189_direct、sixpan_offline。

### 阶段 2：完成 Provider 和运行时硬切换

1. 实现四个内置 ProviderAdapter。
2. 替换 LegacyNativeAdapter 注册。
3. RuntimeBuilder 直接构建完整 V1 对象图。
4. 删除 app.py 的字段级 rewire。

### 阶段 3：删除业务旧链和 façade

1. 删除 ImportService 未读取回调和旧 _submit_* 链。
2. 迁移 planner/staging façade 调用。
3. 接入或删除 ActiveProviderImporter、StorageTargetProbeService。
4. 删除未注册 handler 和 app.py helper re-export。

### 阶段 4：收敛持久层

1. Organizer、Rclone、Update 分模块注入明确 Repository。
2. 将 Deprecated mixin 生产调用从 239 处降为 0。
3. 删除 MRO 兼容测试、DatabaseDomainMixin 继承和动态转发。

### 阶段 5：数据确认后退役历史兼容

发布前后至少执行以下盘点：

- 按 target_route、plan_version、status 聚合 import_jobs。
- 统计 plan_version 为空或不等于当前版本的非终态任务。
- 统计旧 route 字符串对应任务最后更新时间。
- 统计旧配置清理和旧任务拒绝事件的最近命中时间。

只有旧数据为零且观察窗口满足要求，才删除历史任务识别、一次性配置清理和对应迁移分支。

## 7. 建议验收标准

- 新建任务只使用 provider:source_form 或后续明确拆分的字段，不再写入四个旧 route 值。
- static 目录中不再以旧 route 值决定操作、文案或路径展示。
- SearchAggregator 不读取 item.route 做排序。
- app.py 不再注册 LegacyNativeAdapter。
- RuntimeBuilder.build 返回对象无需额外 rewire 即可完成 V1 创建与执行。
- 导入 fnos_media_import.storage.target_service 不再加载 storage.registry 和旧 drivers。
- ImportJobCreationService 不再接收 submit_quark、submit_cloud139、submit_generic 等废弃回调。
- 生产代码对 DatabaseDomainMixin 公开方法的直接调用为 0。
- 测试不再从 fnos_media_import.app 导入已迁移的私有 helper。
- 暂存区和发布包不包含 .test-tmp、_audit_tmp_*。

## 8. 本轮验证结果

| 验证 | 结果 |
| --- | --- |
| python -m compileall -q fnos_media_import | 通过 |
| python -m ruff check fnos_media_import tests | 通过，All checks passed |
| 55 个 V1、Storage、OpenAPI 定向测试 | 通过 |
| 4 个 DatabaseDomainMixin 兼容测试 | 使用工作区固定临时目录后 4/4 通过 |
| 原始 59 测试合并命令 | 其中 2 个测试因 Windows tempfile 目录 ACL 无法创建/清理而报错，不是断言失败 |
| Storage 导入图 | 确认 V1 target_service 会加载旧 registry/types/drivers |
| app 冷导入 | 约 1.53-1.61 秒，约 861 个模块 |

测试限制：

- 未连接真实 Quark、移动云、天翼、SixPan、OpenList 或 rclone 远端。
- 未执行完整全量测试套件；仓库现有 verification.md 已记录此前更大范围验证，本报告只做与旧代码退役相关的定向复核。
- 未读取生产数据库，因此历史兼容层的实际命中量仍需上线前查询。
- DatabaseDomainMixin 定向测试在 .audit-test-db 下生成了被现有 *.db 规则忽略的临时数据库；自动清理命令被执行环境拦截，文件未进入 Git 暂存区。

## 9. 最终结论

当前最大的风险不是“旧代码占空间”，而是**旧语义、新数据模型和新执行协议同时存在，并且边界没有完全切断**。

建议立即处理 LEG-01、LEG-02、LEG-03：

1. 先让旧 route 不再影响当前搜索和页面。
2. 再让四个 Provider 脱离 LegacyNativeAdapter。
3. 最后让 RuntimeBuilder 一次性构造完整 V1 运行时。

完成这三项后，再删除 ImportService 旧链、retired façade、DatabaseDomainMixin 和 app.py 兼容导出，风险会显著低于现在直接大规模删代码。

## 10. V1 硬切换整改复核（2026-08-09）

用户确认采用完整整改、立即硬切换、删除旧任务、删除 API 兼容字段、同步结构化来源字段和单次原子发布后，本文基线发现已在代码提交 `2500a34` 中完成闭环。

| 基线项 | 当前状态 | 复核证据 |
| --- | --- | --- |
| LEG-01 旧 route 影响业务 | 已关闭 | 任务/API 使用 `source_provider_kind + source_form + search_source_id`；搜索不按线路便利度加分；静态测试拒绝四个旧 route 值 |
| LEG-02 `LegacyNativeAdapter` | 已删除 | 四个正式 Provider Adapter 已注册并通过契约测试；Mobile 与 Tianyi 均提供冻结账号修订绑定的原生 API relay uploader |
| LEG-03 Runtime 二次接线 | 已删除 | `RuntimeBuilder.build()` 直接返回完整 V1 对象图；热重载构造完整新 build 后原子替换，不再字段级 monkey-patch |
| LEG-04 Storage V3 导入副作用 | 已删除 | registry、types、drivers、ProbeService 与旧测试退役；`storage/__init__.py` 无副作用 |
| LEG-05 ImportService 旧提交链 | 已删除 | 旧 `_submit_*`、未读取回调和旧 Provider 结果写入逻辑退役 |
| LEG-06 planner/staging façade | 已删除 | 计划读取进入 `planning/job_plan`，路径映射进入当前路径模块；旧 façade 文件不存在 |
| LEG-07 Database façade/mixin | 已删除 | `RepositorySet` 与业务窄 Repository slice 已接线；`DatabaseDomainMixin` 和 MRO 兼容测试删除 |
| LEG-08 app handler/re-export | 已删除 | 未注册 handler、私有 helper re-export 与依赖它们的旧测试退役 |
| LEG-09 重复实现/Protocol | 已收敛 | Composition Root 直接注册 `ProviderRegistry`；正式 Adapter/Importer 边界成为唯一真源 |
| LEG-10 临时暂存文件 | 已清理 | `.test-tmp`、`_audit_tmp_*` 和已删除的临时测试不进入最终提交 |

### 10.1 数据与契约

- 新增 v29 `v1_job_contract_hard_cutover`：重建 `import_jobs`，引入 `source_provider_kind`、`source_form`，删除 `target_route`。
- 只保留合法 canonical V1 execution plan；旧版、缺失、JSON 损坏、hash 不符或来源字段缺失的任务及执行关联直接删除。
- 硬切换 CLI 提供 `--dry-run` 与 `--apply`；应用模式获取独占锁、生成备份、迁移并执行外键检查，输出脱敏 JSON 摘要。
- API/OpenAPI 与前端不提供 `route`、`target_route` 或任务语义 `source_type` 别名。

### 10.2 Provider relay 固定结论

移动云和天翼都不是 WebDAV/rclone 上传目标，而是直连 Provider API：

```text
冻结来源 -> OpenList 来源落地 -> rclone 下载到受控 local_temp
         -> Mobile/Tianyi 原生 API uploader -> 原生结果校验 -> ACK -> 清理
```

Shell 中删除的是脚本内嵌的旧上传后端，不是 Python Provider relay。两个 Provider 均要求绑定冻结账号/连接/目录修订的 `upload_verified` 与 `organizer_mutation_verified` 双证明，WebDAV writable 不能冒充原生 API 能力。

### 10.3 最终自动化证据

- Windows tempfile ACL 测试方案下全量 unittest：`983/983` 通过。
- 安装 pytest 后全量运行：`992 passed, 174 subtests passed`。
- `compileall`、Ruff、UTF-8 源码测试、Git Bash `bash -n` 和所有修改 JavaScript 的 `node --check` 通过。
- 代码阶段提交后，文档阶段暂存区已再次通过 `git diff --cached --check`；无 whitespace error。

### 10.4 尚未完成的外部发布门槛

当前环境未访问真实 Provider 账号，也未执行真实外部上传。发布前仍必须使用非生产账号完成四 Provider 只读探测、对应传输、Mobile/Tianyi 原生 API relay、OpenList、Organizer、取消和恢复验证。详细步骤见 [V1 v29 硬切换发布与回滚手册](./v1-hard-cutover-deploy.md)。
