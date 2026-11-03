# V1.0 多链路改造验证记录

更新时间：2026-08-09
工作区：`C:\WorkSpace\fnos-search`
分支：`faat_0806_入库流程改造`
验证代码提交：`2500a34`

## 1. 本轮目标

本轮针对当前 Git 工作树中 V1.0 多链路改造导致的全量测试回归进行收尾，原则如下：

- 只保留 V1 hard cutover，不恢复 V2/V3/v4 执行兼容；
- 保留现有未提交业务改造，不执行 reset、checkout 或 clean；
- 旧测试不得迫使生产代码重新暴露废弃的 route、staging plan 或 importer 入口；
- 对当前 Windows 沙箱的 tempfile ACL 问题与真实代码失败分开验证。

## 2. 已修复问题

### 2.1 旧执行测试迁移

- 删除只验证 V2/V3/v4 已废弃入口、无法表达 V1 契约的测试；
- Organizer、rclone callback、恢复、暂存和路径测试改用数据库冻结的 V1 execution plan；
- callback/ACK 测试不再依赖 `raw_data.staging_plan` 或旧 route 分发；
- 事件保留测试使用 `import_execution_plans` 和 `transfer_executions` 构造可恢复任务，避免旧计划被错误当作当前执行真源。

### 2.2 测试顺序污染

`tests.test_config_cutover_v1` 原先调用 `load_config()` 时会读取项目 `.env`，把 `FNOS_ROOT_KEY` 泄漏到同一测试进程，导致后续通知密钥测试顺序敏感失败。

现已在该测试中隔离 `_load_dotenv`。以下顺序同进程运行 66 项全部通过：

```text
tests.test_config_cutover_v1
tests.test_notifications
```

### 2.3 Windows 本地路径规范化

`TransferPlan` 原先把 Windows 本地绝对路径：

```text
C:\data\job-1
```

错误规范化为：

```text
/C:/data/job-1
```

这会使 `TransferFinalizer` 在重启恢复和越界校验时触发 `Can't mix absolute and relative paths`。

现行为：

- 带盘符或 UNC 根的本地路径保留 Windows 原生绝对路径；
- `/data/relay/job-1` 等容器 POSIX 本地路径保持 POSIX 形式；
- OpenList/native locator 继续使用 POSIX 规范化；
- finalize 重启恢复、越界拒绝和 relay cleanup 测试均通过。

## 3. 全量测试

### 3.1 原始 Windows Store Python 现象

直接运行标准命令时，测试逻辑已无 failure，但当前 Codex Windows 沙箱会把 Python `tempfile` 以 `0o700` 创建的子目录变为当前沙箱令牌不可写，产生 40 个环境错误：

```text
PermissionError: WinError 5
sqlite3.OperationalError: unable to open database file
```

这些错误集中在 SQLite、本地文件、日志 fallback 和 tempfile 测试。简单的 `tempfile.mkdtemp()` 后写文件也可独立复现，确认不是业务代码导致。

### 3.2 排除 ACL 干扰后的完整结果

测试进程仅在运行时把 `tempfile` 的 Windows mkdir mode 改为继承工作区 ACL；没有把该环境补丁写入生产代码或测试代码。

结果：

```text
Ran 932 tests in 148.776s

OK
```

即当前保留的 unittest 测试为 **932/932 全部通过**。

另有 9 个 pytest 风格的顶层函数测试。当前 Python 环境未安装 pytest，因此直接导入并逐项执行，结果：

```text
9 pytest-style tests passed
```

覆盖 Docker/Compose 发布契约、环境变量文档和追更候选分页。

## 4. 静态验证

以下检查通过：

- `python -m compileall -q fnos_media_import`；
- 对 `tests/*.py` 逐文件执行 `python -m py_compile`；
- `python -m ruff check fnos_media_import tests`；
- 对 `static/*.js` 全部执行 `node --check`；
- `git -c safe.directory=C:/WorkSpace/fnos-search diff --check`。

## 5. 当前结论

此前报告的 `112 fail + 67 error` 已完成清理和修复。当前代码层验证结果为：

- unittest：932/932 通过；
- pytest 风格函数：9/9 通过；
- Python 编译、Ruff、JavaScript 语法、Git whitespace 检查通过。

## 6. 尚未覆盖的发布门槛

本轮证明的是当前代码和本地集成测试闭环，不等同于真实外部服务验收。上线前仍需在正常 Linux/容器环境执行标准全量命令，并使用非生产账号完成：

- 移动、夸克、天翼同源直转；
- 六盘磁链离线；
- OpenList/rclone 跨盘复制；
- 跨 Provider 到移动云/天翼的原生 API relay；
- Organizer 开启/关闭矩阵；
- Worker 重启、长任务续租和真实 Provider receipt reconcile。

当前 Windows 沙箱遗留的不可访问临时目录属于测试运行环境垃圾，不纳入 Git，也不影响上述测试结论。

## 7. 天翼云盘客户端完整登录与 PC API 修复（2026-08-09）

本轮以 OpenList `drivers/189pc`（`189CloudPC`，即“天翼云盘客户端”）为唯一协议基准，未混用 `drivers/189` 或 `drivers/189_tv`。此前同机 OpenList 正常而本项目出现 `InvalidSessionKey / check ip error`，根因是本项目把 PC Session 与 Web Open API 的请求地址、签名方式混用，并且账号探测会立即再次交换 Session，等价于一次界面登录触发两次 Provider 登录，增加了验证码触发概率。

已完成：

- 密码登录固定使用 `/api/portal/unifyLoginForPC.action`、`encryptConf.do`、`needcaptcha.do`、`picCaptcha.do` 与 `loginSubmit.do`；RSA 密文改为大写十六进制，不再擅自追加 `@189.cn`；
- `needcaptcha.do` 只要返回值不是 `0` 就进入验证码流程，支持首次验证码、错误后新图片、手动“换一张”和过期挑战重新生成；
- 登录 CookieJar 完整序列化到加密认证会话，续登时恢复 name/value/domain/path/secure/expires，避免只携带 `LT` 导致上下文丢失；
- 登录提交表单按 189CloudPC 字段收敛，不再混入 Web 页面的 `epd`、`mailSuffix`、`pageKey` 或 `smsValidateCode`；
- 密码登录成功后仅使用 TELEPC 6.2：redirect URL 通过 POST 交换 Session，Access Token 通过 GET 刷新 Session，删除 TELEMAC 兼容回退；
- 私有文件 API 改为 `api.cloud.189.cn/*.action`，请求头使用 `Date / SessionKey / X-Request-ID / Signature`，签名原文为 `SessionKey + Operate + RequestURI + Date`，以 `sessionSecret` 做 HMAC-SHA1 并输出大写十六进制；
- `sessionKey`、`sessionSecret`、Access Token、Refresh Token 统一规范化并加密持久化；支持 XML 错误响应、Session 失效后有限次数刷新以及 `keepUserSession.action` 保活；
- 新建 importer 读取到刚登录的 Session 后直接用于账号探测，不再立即进行第二次密码登录或 Session 交换；业务请求也不再后台盲目重提密码；
- 账号探测使用真实只读 `listFiles.action`；管理端验证码弹窗增加“换一张”，验证码错误继续保持 waiting 状态并显示新图片和错误信息，而不是终止认证会话返回 400。

验证结果：

- 天翼 importer、认证会话、管理端 UI、缓存版本与 API 契约：`83` 项通过；
- Provider Registry、存储设置、运行时装配、编码和配置兼容：`64` 项通过；
- 使用现有 Windows tempfile ACL 测试补丁执行全量 unittest：`969/969` 通过；
- `python -m compileall -q fnos_media_import`、`python -m ruff check fnos_media_import tests`、相关 `py_compile`、`node --check static/admin-storage-v1.js` 和 scoped `git diff --check` 通过。

尚需外部环境验证：

- 当前环境未使用真实天翼账号发起外部登录，也未绕过 Provider 验证码；部署到飞牛 Docker 后应使用非生产账号完成一次“无验证码登录”和一次“验证码登录”；
- 登录后至少验证根目录列举、Session 保活，以及一次真实转存任务；若 Provider 仍返回业务拒绝，应保留新的 `trace_id` 与脱敏后的错误码继续定位。

## 8. 配置兼容、天翼缓存与预检副作用审计（2026-08-09）

审计结论：

- `RCLONE_SRC/DST_*DIR` 在 `rclone_environment.py` 中是任务描述符内部映射键，不是存活的环境变量读取；被启动校验拒绝的 `CMCC_UPLOAD_*` 与上传器仍读取的进度、分片调优变量没有交集，因此不存在“同一变量既硬拒又被生产路径读取”的冲突。
- 数据库 `advanced_config` 已在启动阶段通过 CAS 自动过滤旧 `quark`、`routes`、旧 OpenList 身份字段和未知字段；旧数据迁移及持久化测试通过，无需手工删除 SQL。
- 天翼认证缓存增加本地 `account_id` 命名空间；相同用户名和凭证的两个本地账号不再共享进程级 Cloud189 会话。
- 完整预检改为状态只读：账号检测、OpenList 连接检测和 mount 读写检测仍执行真实网络探测，但不写账号健康状态、连接检测时间、mount 健康状态、mount revision 或修订历史。单独点击检测仍按原行为持久化结果。

验证结果：

- 天翼、账号服务、Provider Registry、OpenList、存储设置、配置迁移和运行时装配共 `168` 项测试通过；
- `python -m ruff check`、`python -m py_compile`、`tests.test_source_encoding` 和 scoped `git diff --check` 通过。

## 9. 访客天翼云预览与提交闭环（2026-08-09）

本轮只执行天翼与访客链路定向验证，没有再次运行 900+ 全量测试。

已完成：

- PanSou 搜索结果中的 `cloud.189.cn` 链接在 V1 无旧 `routes` 配置时识别为已实现来源；
- 访客搜索详情使用当前激活的天翼 Provider 账号检测分享、返回根目录，并支持逐层展开子目录；
- 搜索结果和手动粘贴链接共用“识别 → 预览 → 勾选 → 提交前检查 → V1 来源快照 → 天翼原生转存”链路；
- 访客可选择天翼文件/文件夹，未选择时默认保存整个分享；账号、目标目录、mount 等基础设施字段不会进入选择快照；
- 搜索缓存提交时保留服务端可信 URL/提取码，同时合并已清洗的 `tianyi_selection`，避免选择在创建 V1 任务前丢失；
- Tianyi importer 支持 V1 `provider_selection.tianyi_selection` 嵌套快照，并能提交从子目录预览中选择的条目；
- 前端补齐天翼选择模块导出、目录读取失败提示和静态资源缓存版本。

定向验证结果：

```text
Ran 75 tests in 0.822s

OK
```

覆盖天翼 importer、访客搜索/手动链接预览、目录展开、选择清洗、提交预检、缓存选择合并、前端静态契约、运行时装配，以及少量公开页面选择回归。

以下静态检查通过：

- `python -m compileall -q fnos_media_import`；
- 相关 Python 文件 `ruff check` 与新增测试 `py_compile`；
- `node --check`：`resource-search-workbench.js`、`public-search.js`、`public-submit.js`、`public.js`；
- 相关已跟踪文件 scoped `git diff --check`；
- `tests.test_source_encoding`。

未执行真实外部天翼账号或真实转存任务。部署后仍需使用非生产账号验证一次搜索结果预览、子目录展开、勾选提交，以及一次手动天翼链接提交。

## 10. 天翼保存目录 WebDAV PUT 405 原生链路降级（2026-08-09）

现场现象：当前 OpenList 实例的 `189CloudPC`（天翼云盘客户端）挂载能够认证、列举和创建目录，但 WebDAV `PUT` 文件上传返回 HTTP 405。官方源码确认 `189CloudPC` 实现了 `Put`，并支持 `stream`、`rapid`、`old` 三种上传方式；同时 OpenList WebDAV 层会把 `fs.PutDirectly` 返回的任意非 NotFound 错误统一映射为 HTTP 405。因此，405 不是“WebDAV 或天翼驱动不支持上传”的能力结论，而是当前实例底层上传失败后的笼统状态码，真实错误仍需查看 OpenList 容器同一时刻的 `PUT /dav/...` 日志。

本轮最终产品策略：天翼同源入库继续使用 Provider `native_save`；跨 Provider 入库则使用 `relay_upload_provider`，先把来源落到本地临时目录，再通过冻结账号修订对应的 189CloudPC 原生 API 上传到天翼。OpenList 在这条链路中负责目录可见性和 Organizer 的读取、MOVE、DELETE，不负责上传文件字节。因此，WebDAV `PUT` 405 不再阻断天翼作为跨盘目标，但也不代表当前 OpenList WebDAV 上传故障已经修复。

本轮完成：

- `TIANYI_MANIFEST` 与 `TianyiProviderAdapter` 已声明并装配 relay 能力，冻结账号修订后可构造天翼上传器和原生目录浏览器；
- 管理端保存位置支持直接浏览天翼原生目录并保存真实 `folder_id`，同时保留用于回显的目录名称；
- 新增 189CloudPC 原生递归上传：分片哈希、上传初始化、预签名分片 PUT、秒传提交、子目录创建、进度回调、durable manifest 幂等和重启 fence；
- 上传完成后按 durable manifest 中的文件 ID、相对路径和大小精确校验，不能仅凭目录总数或模糊名称判定成功；
- 保存位置创建时必须完成“天翼原生上传并列举确认”以及“OpenList 读取、MOVE、DELETE”两份证明，证明指纹绑定 Provider、账号修订、原生目录、OpenList 连接修订和 WebDAV 根目录；缺少任一证明均失败关闭；
- mount 探测仍真实执行 WebDAV `PUT`。PUT 405 时天翼 mount 持久化为 `readable=true / writable=false`，但已通过上述两份原生证明后仍可作为 Provider relay 目标；
- Planner 对天翼同源选择 `native_save`，对夸克/移动等跨源选择 `relay_upload_provider / provider_relay`，并冻结目标账号修订、mount 修订、原生 `folder_id` 与本地 relay 临时目录；
- Planner、PlanValidator 与 Storage Setup 均拒绝将“WebDAV 可写”冒充移动云或天翼原生上传证明；两个 Provider 都必须具备显式双证明；
- 管理端展示“原生上传已验证”“OpenList 整理已验证”“WebDAV 只读”等独立能力状态，路由说明明确跨盘数据由目标云盘原生 API 上传；
- 非 relay Provider 遇到相同 PUT 405 仍按 `OPENLIST_PROBE_FAILED` 失败关闭并补偿删除半成品 mount。

定向验证：

- 天翼上传协议与递归 relay：`tests.test_tianyi_upload_v1` 10 项通过；
- 天翼同源、夸克到天翼 relay、Planner/Validator 双证明：`tests.test_multilink_v1_integration` 6 项通过；
- Storage Setup、PUT 405、原生目录、双证明和预检：`tests.test_storage_setup_v1` 29 项通过；
- 传输分派、Provider 契约、V1 退役、管理端 UI/缓存与源码编码等定向组合：合计 80 项通过；
- `node --check static/admin-storage-v1.js`、`python -m compileall -q fnos_media_import`、相关文件 `ruff check`、`tests.test_source_encoding` 和全工作树 `git diff --check` 通过。

未访问真实天翼账号，也未执行真实外部上传。部署后仍需使用非生产账号完成一次小文件原生上传探测、一次跨 Provider relay、一次 Organizer 整理，并结合 OpenList 与本服务日志确认完整链路。

## 11. V1 旧代码全量硬切换最终门禁（2026-08-09）

### 11.1 固定架构结论

- 移动云与天翼都是冻结账号修订驱动的 Provider 原生 API 直传目标；rclone 只负责把来源下载到受控 `/data/relay/job-<id>`。
- Mobile 使用 `CmccMobileUploader`，Tianyi 使用 `TianyiCloudUploader`；两者都由 `ProviderRelayExecutor` 执行并在 ACK 后清理。
- Shell 中已删除的只是脚本内嵌 Provider 上传后端，不影响 Python Provider API relay。
- Mobile/Tianyi 保存位置都必须具备 `native_locator.upload_verified` 与 `native_locator.organizer_mutation_verified`；WebDAV writable 不得冒充任一证明。

### 11.2 本轮收尾修复

- rclone callback 强制要求冻结 `job-N` 目录，测试补充空值与非法目录拒绝；完成 ACK 必须明确 `delete_source_allowed=true`。
- rclone Worker 头部文案改为“script-embedded provider upload backends retired”，避免误读为 Provider relay 被退役。
- 删除无调用的 `evaluate_bt_content_risk` 兼容 façade 与 `TransferVerifier.__call__` 裸调用兼容。
- 更新设计、存储、部署和回滚文档，统一使用 `relay_upload_provider`，并明确 Mobile/Tianyi 双 Provider API relay。
- `TIANYI_MANIFEST` 与 `MOBILE_MANIFEST` 均显式声明 `supports_rclone_dest=false`；Planner 只能为其选择 `relay_upload_provider`，PlanValidator、canonical job reader 与 TransferOrchestrator 三层同时拒绝伪造的 `rclone_copy -> mobile/tianyi` 计划。
- 管理端删除 `relay_upload_mobile` 旧策略别名，统一显示“Provider 原生 API 直传（本地中转）”；Organizer/rclone 测试夹具同步改为合法的 SixPan/local rclone 目标。
- v29 硬切换迁移补齐 `organizer_ai_suggestions` 与 `organizer_tmdb_matches` 的显式统计和清理，dry-run、apply 与外键完整性测试均覆盖这两类执行子记录。
- 硬切换 CLI 只允许实际持有独占锁的进程关闭并删除锁文件，锁竞争回归测试已覆盖。
- 生产构造器已删除旧 `route/routes/current_routes` 输入；Provider、relay、Organizer 与取消入队均直接调用 V1 正式签名，不再运行时探测旧接口。

### 11.3 最终测试结果

使用测试进程内的 Windows tempfile ACL 继承方案执行：

```text
Ran 987 tests in 176.690s

OK
```

安装 pytest 后全量执行：

```text
996 passed, 194 subtests passed in 176.86s
```

定向复核：

- rclone callback、dispatch、Worker 隔离、冻结描述符与 Storage V1 退役：`57` 项通过；
- Mobile/Tianyi 双原生证明、Provider、Planner、Validator 与 dispatch：`71` 项通过；
- retired façade、Transfer 与 native protocol：`84` 项通过。

静态门禁：

- `python -m compileall -q fnos_media_import tests scripts`：通过；
- `python -m ruff check .`：通过；
- `tests.test_source_encoding`：通过；
- Git Bash `bash -n scripts/fnos_rclone_worker.sh`：通过；
- `static/` 下全部 23 个 JavaScript 文件的 `node --check`：通过；
- 工作树与文档暂存区 `git diff --check` / `git diff --cached --check`：通过。

### 11.4 未执行项

当前环境未访问真实 Provider 账号、未绕过验证码，也未执行真实外部上传。以下仍属于发布维护窗口门槛，不能记为已通过：

- Quark、Tianyi、Mobile、SixPan 非生产账号只读探测；
- 四 Provider 对应传输流程；
- 跨 Provider 到 Mobile 的移动云 API relay；
- 跨 Provider 到 Tianyi 的 189CloudPC API relay；
- OpenList、Organizer、取消、重试和 Worker 重启恢复。

执行步骤与回滚要求见 `docs/v1-hard-cutover-deploy.md`。

## 12. OpenList 原生上传探针跨域下载修复（2026-08-10）

现场复现确认：移动云原生上传成功，OpenList `PROPFIND` 也能看到探针文件及正确大小，但 WebDAV `GET` 经 302 跳转到对象存储签名地址后返回 `InvalidArgument`。根因是 Python `urllib` 默认把 OpenList 请求中的 Basic `Authorization` 继续带到跨域对象存储地址；对象存储同时收到签名查询参数和无关的 Basic 认证头，因此拒绝请求。

本轮修复：

- WebDAV 客户端只在同源跳转中保留认证头；跨协议、主机或端口跳转时移除 `Authorization` 与 `Proxy-Authorization`；
- 保持 OpenList 自身同源跳转的 Basic 认证行为不变；
- 探针读回重试耗尽时保留最后一次底层错误，避免只显示无诊断价值的 `OpenList probe file read failed`；
- 新增本地双 HTTP 服务回归测试，精确模拟“OpenList 302 到对象存储、携带 Basic 时返回 400”的现场行为。

验证结果：

- OpenList、连接韧性与 Storage Setup 相关回归：`104 passed, 6 subtests passed`；
- 新增跨域/同源重定向测试：`2` 项通过；
- `ruff check` 与相关文件 `compileall`：通过；
- 使用当前移动云账号的 `/webdav` 原生目录与 OpenList `/移动云` 挂载执行真实小文件探针，原生上传、OpenList 读回、MOVE、DELETE 全部通过；
- 探针与诊断文件无残留；服务已重启，`http://127.0.0.1:5251/admin` 返回 HTTP 200。

## 13. 入库、保存、rclone 与 Organizer 链路审计（2026-08-10）

本轮未创建真实媒体任务、未保存入库规则、未启用 Organizer。外部写入仅使用可自动清理的小文件探针。

### 13.1 新发现并修复的运行回归

- rclone 运行记录、rclone 文件记录、Organizer 任务列表和 Organizer 执行记录接口此前均把完整 `RepositorySet` 误传给计数依赖，运行时分别调用不存在的 `count_rclone_*` / `count_organizer_*` 方法并返回 HTTP 500；现已改为注入 `db.rclone` 与 `db.organizer`。
- 任务取消服务仍调用已退役的 `get_job` / `update_job_if_status` 聚合 façade；现已拆分注入 `job_queries` 与 `job_commands`，并使用正式仓储方法 `get`、`update_if_status`、`add_event`。
- 公共搜索、手动链接预览、六盘磁链预览和 BTBTLA 解析仍使用已退役的搜索缓存 façade；现已统一改为显式注入 `search_cache_queries` 与 `search_cache_commands`。
- 当全部外部搜索源失败时，访客搜索原先进入全局异常处理并返回 HTTP 500；现改为 HTTP 503、`SEARCH_PROVIDERS_UNAVAILABLE`、`retryable=true`，不再把外部搜索源超时伪装成服务内部错误。

修复后实时路由审计确认以下接口均返回 HTTP 200：

- `/api/admin/rclone/status`
- `/api/admin/rclone/logs`
- `/api/admin/rclone/runs`
- `/api/admin/rclone/events`
- `/api/admin/rclone/files`
- `/api/admin/organizer/tasks`
- `/api/admin/organizer/runs`
- `/api/admin/jobs`
- `/api/admin/system/events`
- `/api/admin/system/task-logs`
- `/api/public/config`

当前 PanSou 地址在本机真实请求中超时；访客搜索已验证返回 HTTP 503，而不是 HTTP 500。该外部搜索源仍需单独恢复或替换。

### 13.2 真实账号与 OpenList 小文件探针

- 当前激活并通过实时账号检测：夸克、移动云、天翼、六盘，共 4 个来源类型；另有一个未激活的旧天翼账号，不参与当前运行链路。
- OpenList `openlist_main` 认证和服务版本探测通过。
- 天翼、夸克、六盘、移动云 4 个 mount 均通过根目录读取、小文件 PUT、内容读回、MOVE 与 DELETE，结果均为 `readable=true / writable=true`，且任务级探针目录已清理。
- 天翼真实执行原生 API 小文件上传，并由 OpenList 完成可见性、内容读回、MOVE 和 DELETE，全部通过。
- 移动云真实执行原生 API 小文件上传，并由 OpenList 完成可见性、内容读回、MOVE 和 DELETE，全部通过。
- 4 个保存目标的结构、账号绑定、分类路径、rclone/OpenList locator 均通过目标校验。

### 13.3 当前配置阻塞

- Storage Setup 当前为 `3/4`：账号、OpenList、保存位置已完成，4 个来源的入库规则尚未选择默认保存目标。因此预检只失败于“来源保存规则”，当前不能宣称完整入库可用。
- `organizer.enabled=false`，TMDB、AI、STRM 本地根目录均未配置；本轮只验证了 Organizer 所依赖的 OpenList 读取、MOVE、DELETE 数据面，没有执行影视识别、命名规划、正式整理、回滚或 STRM 刷新。
- 当前 Windows 运行环境找不到 `rclone` 与 PATH 中的 `sh`，`/api/admin/rclone/check` 明确失败，`/api/admin/rclone/webdav-config` 返回 HTTP 503。Git Bash 的 shell 文件存在，但没有 rclone 可执行文件；本机也没有 Docker 命令，因此无法在正式镜像环境复验 rclone Remote、跨盘复制、Worker 回调和重启恢复。
- 当前没有待处理、运行中或历史真实入库任务；为避免写入真实规则，本轮没有生成一条端到端媒体任务。因此“来源保存 → 跨盘 rclone/Provider relay → Organizer 正式落位”的最终发布门槛仍需在配置入库规则并具备正式 rclone Worker 后，以一个非生产小文件任务完成。

### 13.4 相关范围验证

按用户要求只执行本次涉及范围，不以全量测试作为验收结果：

- Storage Setup、OpenList、目标/计划、Provider、rclone、readiness、UI、公共预览与取消链路：`292 passed, 78 subtests passed`。
- 新增公共搜索异常映射、搜索缓存显式仓储与硬切换约束：`22 passed, 66 subtests passed`。
- 生产任务取消仓储与公共缓存仓储补充验证：`14 passed`。
- 源码编码：`1 passed`。
- `python -m compileall -q fnos_media_import`：通过。
- 相关 Python 文件 `ruff check`：通过。
- `bash -n scripts/fnos_rclone_worker.sh`：通过。
- `node --check static/admin-storage-v1.js`：通过。
- `git diff --check`：通过，仅有 Git 的 LF/CRLF 提示，无空白错误。

## 14. 缺少 rclone 时解耦 OpenList 保存（2026-08-26）

### 14.1 问题与根因

本机保存 OpenList 连接报 `rclone WebDAV 同步或连接检测失败：rclone 管理命令不可用`，连接无法落库。

调用链：`RcloneWebdavConfigService._run` 找不到二进制抛 `FileNotFoundError` → `RcloneWebdavConfigError(503)`
→ `StorageSetupService._sync_rclone_webdav` 的 `except Exception` 上抛 → `save_openlist` 的补偿事务把刚写入的
连接与 mount 快照整体回滚 → 蓝图包装成带 `trace_id` 的错误返回。

判断这是错误耦合：rclone 只负责跨盘搬运，而 OpenList 连接同时驱动目录整理与 STRM，同盘链路根本不碰 rclone。

### 14.2 改动

- `services/rclone_webdav_config_service.py`：新增 `RCLONE_UNAVAILABLE` 错误码，`RcloneWebdavConfigError`
  增加 `code` 字段。`FileNotFoundError` 与 `OSError` 带上该码；`TimeoutExpired` 有意不带，因为超时说明 rclone 在位。
- `storage/setup_service.py`：新增 `_rclone_plane_absent` / `_rclone_plane_absent_payload` / `_rclone_degraded_state`。
  只有 `RCLONE_UNAVAILABLE`、`worker_unavailable`、`worker_timeout` 视为“控制面不存在”而降级放行；未接线的
  rclone bridge 也直接返回降级态而不再抛错。`save_openlist` 返回 `rclone` 与 `rclone_available` 字段。
- `_probe_rclone_webdav` 与 `preflight()` 未改，跨盘路由仍会硬失败。
- `static/admin-storage-v1.js`：`submitOpenList` 读取响应，`rclone_available === false` 时显示告警而非成功。
- `static/product.css`：补 `.storage-setup-notice.is-warning` 样式。

### 14.3 验证

- 保留回滚语义：凭据错误、Remote 名被其他类型占用、配置写入失败、rclone 在位超时、Worker 明确 409 拒绝，
  共 5 类仍然整体回滚；仅二进制缺失、Worker 不可达、Worker 超时 3 类降级。以 8 种异常形态的分类矩阵逐一确认。
- 本机真实无 rclone 的 subprocess 调用确认抛出 `code='RCLONE_UNAVAILABLE'`，被判定为 absent。
- `tests/test_storage_setup_v1.py` 新增 5 个用例：缺二进制仍保存、Worker 不可达仍保存、未接线仍保存、
  真实拒绝仍回滚、无 rclone 时跨盘预检仍失败。该文件 `42 passed`。
- Storage Setup + rclone WebDAV 配置 + Worker 隔离 + OpenList 四套：`121 passed, 4 subtests passed`。
- 全量：`1045 passed, 198 subtests passed`（187s），相对改动前 1040 基线无回归。
- 改动的 3 个 Python 文件 `ruff check` 通过；`node --check static/admin-storage-v1.js` 通过。

### 14.4 遗留

- `worker_timeout` 归入降级是一个取舍：远端 Worker 在位但 120s 内没完成 WebDAV 写入时也会放行保存。
  实际部署里该超时基本等价于 Worker 不可达，且跨盘路由仍被预检拦住，因此接受。
- 本机仍无 rclone 与 Docker，跨盘搬运、Worker 回调、重启恢复的正式镜像复验仍是第 13.3 节列出的未覆盖项。

## 15. 访客首页 500 与 PanSou 搜索 503 定位（2026-08-26）

用户报告访客首页搜索报错，日志里实际是两个独立问题。

### 15.1 `GET /api/public/config` HTTP 500（代码缺陷，已修）

`public_web.py:331` 的 `if cloud139_importer:` 判断无效：`ActiveProviderImporter` 没有定义
`__bool__`，代理对象恒为真，于是 `cloud139_importer.describe()` 经 `__getattr__` → `resolve()`
抛出 `ActiveProviderUnavailable`，把「移动云账号未激活」这一配置状态变成访客首页的 HTTP 500。
紧邻的 `placeholder_specs` 循环早已用 `get_optional_provider_method` 做了 fail-closed 加固，
cloud139 分支在那轮加固里被漏掉。

修复：
- `public_web.py`：cloud139 分支改用 `is not None` + `get_optional_provider_method("describe")`，
  异常时按空 description 处理，与 cloud189/sixpan 循环一致。该适配器降级为
  `configured=false`，公共投影统一输出 `status="unavailable"`。
- `app.py`：全局 `handle_error` 增加 `ActiveProviderUnavailable` → HTTP 503 `provider_unavailable`
  分支，置于通用 500 兜底之前。未激活账号是配置状态，不应报服务器内部错误，也不该把
  「哪个 Provider 需要激活」的可执行信息藏在 trace_id 后面。

验证：
- 复现确认 `bool(proxy) is True`（旧判断为何失效）、直接 `.describe()` 仍抛异常，
  修复后 `_public_adapter_capabilities` 正常构建，4 个适配器均为 `unavailable`。
- 逐一确认其余代理访问点已 fail closed：`proxy.configured`、`_importer_configured`、
  `_detail_capability_for_source`（mobile/tianyi/sixpan）全部返回 False 而不抛异常；
  `check_configuration` 仍按设计抛出，供后台探测显式报错。
- 新增 3 个用例：移动云未激活时公共配置降级、已激活时不被误判为未配置、
  错误处理器把 `ActiveProviderUnavailable` 映射为 503 且带 `provider_unavailable`。
- 反向验证：临时回滚两处修复后新用例 2 失败，恢复后 10 passed，确认非空测试。

### 15.2 `POST /api/public/search` HTTP 503（本节结论已被第 16 节推翻）

> 修正：本节当时判定为「外部依赖故障、无需改代码」，是错的。504 的直接触发因素是我们
> 自己请求里的 `conc=10` 参数。完整定位与修复见第 16 节。以下保留原始观测记录。

实测上游 PanSou `81.70.158.50:8055`：
- TCP 连接 0.0s 正常，`GET /` 200，`GET /api/health` 200 且 `auth_enabled=true`；
- 用 `advanced_config` 里的真实账号登录取 token 成功；
- 带 token 的 `POST /api/search` 在 timeout 放宽到 150s 后于 **60.1s 返回 HTTP 504**。

当时只观测到「上游在线但搜索超时」，未变更我们自己的请求参数就下了外部故障的结论，
这是本轮排查的方法缺口。另注意配置文件 `pansou.base_url` 是 `127.0.0.1:8055`，
运行时被 `app_settings.advanced_config` 覆盖为远端地址，排查时勿只看配置文件。

## 16. PanSou `conc` 参数导致搜索 504（2026-08-26）

用户提示 0810 分支当时是「传的某个参数有问题」，据此复查请求体，确认根因是 `conc`。

### 16.1 根因

`conc` 是 PanSou 的搜索并发度。固定成 10 会让插件较多的实例把这次搜索分批扇出，
冷搜索（未命中 PanSou 自身结果缓存）耗时超过上游网关超时，返回 HTTP 504；
不传 `conc` 时由 PanSou 按自身配置决定并发，一次扇出完成。

`providers/pansou.py` 的 `_search_payload` 本来就有 `if self.conc > 0` 的省略逻辑，
问题只在默认值：`DEFAULT_CONCURRENCY = 10`，且 `config.py` 每次都显式下发 `conc`，
所以必须同时改掉配置侧，否则改 Provider 默认值不生效。

### 16.2 实测证据

对同一上游、同一关键词做对照实验，请求带 `refresh=true` 以绕过 PanSou 结果缓存、
强制真实插件扇出（第一次未加 `refresh` 时两组都是 0.1s / 17 条，是缓存假象）：

| 请求 | 结果 |
| --- | --- |
| `conc=10` | HTTP 504，60s，0 条 |
| 不传 `conc` | HTTP 200，约 4s，191 条 |

3 个关键词、正反两种执行顺序，4/4 复现，排除缓存与顺序干扰。

### 16.3 改动的四处 + 文档

- `providers/pansou.py`：`DEFAULT_CONCURRENCY` 10 → 0（0 表示交给 PanSou 自己决定，
  `_search_payload` 随之整个省略 `conc` 字段）。
- `config.py:273`：`_env_int("PANSOU_CONC", 10)` → `0`。**关键**，此处一直显式下发 `conc`。
- `static/admin.js:1419`：回填改为 `pansou.conc ?? 0`。
- `static/admin.js:1752`：提交改为 `numberValue("advPansouConc", 0)`。**三处 UI 里最关键**，
  它会在任意一次高级设置保存时把值写回 DB `advanced_config`，留 10 会静默复现该缺陷。
- `templates/admin.html:762` 占位符、`config/config.example.yaml` 注释与示例同步为 0 并写明 504 风险。

### 16.4 验证

- 新增 3 个用例（`tests/test_pansou_search_payload.py`）：默认并发交给 PanSou、
  显式并发仍被保留、随包配置默认值不得固定并发（后者直接加载真实配置，守住 `config.py` 那处）。
  5 passed；反向验证回滚修复后 2 个失败，确认非空测试。
- 确认 DB `advanced_config.pansou` 里没有 `conc` 键，新默认值在本机确实生效。
- 走真实客户端端到端：`conc = 0`、payload 中无 `conc` 字段、`search OK in 5.8s items=16`。
- `node --check static/admin.js` 通过；改动文件均为 UTF-8 无 BOM。
- ruff 干净；本轮未重跑全量套件（第 15 节后曾全绿 1055 passed）。


## 17. 全局清理源码里的 CJK \uXXXX 转义（2026-08-26）

### 17.1 背景与判定标准

用户要求：源码里不应出现 `\u4e91\u76d8\u4fdd\u5b58\u6839\u76ee\u5f55` 这类转义，中文一律写成
真正的 UTF-8 字符。先把命中分成三类再动手，避免机械替换改坏语义：

- TEXT：普通字符串/注释里的中文转义 —— 本轮要清理的目标。
- RANGE：正则里的码位区间（如 `[\u4e00-\u9fff]`）—— **保留转义**。写成 `[一-龿]` 语义等价，
  但会丢掉「这是一段码位区间」的意图，可读性反而更差。
- VENDOR：`codex_tmp/` 第三方代码与 `.codex/` 工具自身 —— 不属于本项目源码，跳过。

### 17.2 等价性证明（每类改动都有各自的证明手段）

转义与字符在 Python 里是**同一个常量**，因此不同 token 用不同证明：

| 改动类型 | 证明方式 | 理由 |
| --- | --- | --- |
| 普通字符串、注释 | 全文件 `ast.dump` 必须完全一致 | 常量相同，AST 有任何差异即语义被改 |
| 原始字符串正则 | 编译后 `re._parser.parse` 树递归比对 | 常量确实变了，但 `re` 自己会解码 `\uXXXX`，匹配行为不变 |
| f-string | 同上，占位符两侧同样掩码后再比 | Python 3.12 起 f-string 拆成多个 token |

任何一项证明不通过就原样保留，绝不硬改。

### 17.3 两个必须留转义的陷阱（都被证明手段挡住了）

- **原始字符串里的 `\uu` 是数据不是转义**：`r"(\u7535\u89c6\u5267|...)"` 的反斜杠由 `re` 引擎消费。
  文件级 AST 检查会整份拒绝这类文件，因此这 3 个文件改用逐 token + 正则解析树证明，
  才把其中的普通字符串与可安全转换的正则分开处理。
- **双反斜杠是断言字面量**：`tests/` 里 `'\u\uu6b63\u\uu5728'` 断言的是**另一个文件源码中出现的字面文本**。
  转换器用 `(?<!\u\u)` 负向后顾跳过这类写法。
- `tests/test_source_encoding.py:11` 的 `MOJIBAKE_MARKERS` **必须保持转义**：它是项目自身的乱码守卫，
  一旦写成真字符，这个测试就会把自己的源码判成乱码。

### 17.4 结果

- 30 个文件完成转换，TEXT 命中从 313 行降到 1 行（即上面那个故意保留的守卫常量）。
- RANGE 17 行、VENDOR 419 行按判定标准保留。
- 顺带把 `static/admin-storage-v1.js` 里 `data-loading-label` 的中文转义改为 UTF-8，
  并同步 `tests/test_admin_settings_ui.py` 中对应的两条断言（原断言锁的是旧转义字面量）。

### 17.5 验证

- 全量套件 **1068 passed, 198 subtests passed**（225s）—— 机械改动面广，这里跑全量而非挑模块。
- `node --check` 通过：`static/admin.js`、`static/admin-storage-v1.js`。
- ruff（E9,F63,F7,F82）干净。
- `git diff --stat` 三个手工处理文件增删行数完全相等（60/60），确认只改了转义、没有行尾污染。
- 保留 `.codex/scan_escapes.py` 作为后续复查工具，一次性转换脚本已删除。

## 18. rclone 互斥锁被跳过却记成成功，导致中继上传空目录失败（2026-09-18）

### 18.1 现象

任务 #9（夸克 → 移动云，relay 链路）刚创建就失败：`rclone` 日志先打印"已有 rclone 搬运任务
运行中，本次跳过"，随后立刻"rclone run 结束已立即唤醒传输执行"，上传报"中继目录为空或不存在：
/data/relay/job-9"，20 次重试全部是"移动云目标目录不存在：/webdav/_入库暂存/电视剧/job-9"。

### 18.2 根因链

1. `scripts/fnos_rclone_worker.sh` 的 `acquire_lock` 用 `mkdir` 做锁，拿不到锁 `exit 0`；
   锁目录靠 EXIT trap 删除，SIGKILL / 容器硬重启后永久残留。
2. `RcloneProcessRunner` 只看退出码，0 即成功；完成服务把 run 标 `success` 并唤醒传输。
3. `ProviderRelayExecutor` 看到下载完成立即上传，`/data/relay/job-9` 为空。
4. 重试清空回执后重新 `submit`，适配器按 operation_key 对账到已 `success` 的 run，返回
   `completed` 回执；编排器把 completed 回执当作"传输完成"直接进目标校验，上传永远不再执行。
5. 移动云 `verify_target` 只读不建目录，目录从未被真实上传创建，校验必然 missing。

### 18.3 修复

- 脚本锁改为 `flock -n` 持有 fd 9，进程退出内核自动释放；拿不到锁以 `EXIT_LOCK_BUSY=75` 退出，
  缺少 `flock` 命令时 `exit 1` 而不是静默跳过。
- `constants.RCLONE_EXIT_LOCK_BUSY = 75`；`ProcessResult.not_started` 识别该退出码。
- `RcloneRunCompletionService.finish`：75 时 `RcloneRunState.mark_deferred`（回到 idle），
  任务级 run 回滚到 `queued`、写 warn 事件、不唤醒传输；非任务级 run 如实记 failed。
- `RcloneService._schedule_deferred_run`：按 staging 退避（默认 30s 起、指数、上限 8 次）
  用 `threading.Timer` 重新 `start`；期间 run 不再是 queued 则跳过；超过上限或重提交失败则
  标 failed 并唤醒传输，交编排器按普通失败处理。`shutdown` 取消所有延后定时器。
- `RcloneService.invalidate_run` + `RcloneOperationAdapter.invalidate`：只允许把 `success`
  的 run 标 `failed`，运行中/排队中的 run 拒绝作废；operation_key 不匹配拒绝。
- `ProviderRelayExecutor.submit`：下载回执为 `completed` 时降级为 `running` 并保留
  `step=download`，poll 沿"下载确认 → 上传"正常路径走完。
- `ProviderRelayExecutor._upload`：上传结果 `SOURCE_LANDING_UNAVAILABLE` 时调用适配器作废下载
  run，重试时 `find_run_by_operation_key` 对 failed run 返回 None，真正重新下载。

### 18.4 验证

- 新增 `tests/test_rclone_lock_busy_and_relay_reconcile.py` 23 例：脚本契约、退出码映射、
  完成服务四分支、延后重排/退避/耗尽/关闭、作废四分支、执行器降级与空目录作废端到端、
  适配器 invalidate 三分支。
- 受影响既有模块 16 个文件 230 passed + 128 subtests。
- 全量套件 **1318 passed, 253 subtests passed**（210s），含新增 23 例。
- ruff（E9,F63,F7,F82）干净。
- 未做：真实容器内 `flock` 行为与 75 退出码的端到端联调（本机无 docker）。`flock` 属
  Debian `util-linux`，`python:3.11-slim` 基础镜像自带；若基础镜像更换需复核。

### 18.5 生产处置

现网任务 #9 的旧 run 8 仍是 `success`：重试任务时新代码会在上传前发现中继目录为空并作废
run 8，随后自动重新下载，无需手工改库。若 Worker 内确有残留 rclone 进程占锁，先
`docker exec fnos-rclone-worker ps aux | grep rclone` 排查；旧版残留的 `/tmp/*.lock.d`
目录对新版无影响，可顺手删除。

## 19. 接续项目审计与 OpenList / 中继修复收尾（2026-10-09）

### 19.1 范围与交付

- 接续“检查游戏啊当前项目问题”中因服务额度错误中断的工作；保留并审核已有未提交修复，没有重置工作区。
- 审计记录中的 10 项代码问题和新增的 3 条用户故障链路已完成修复与针对性验证。以下是本轮最终代码状态，不沿用第 18 节的历史全量测试数作为当前证明。
- 全程仅使用内存替身、临时 SQLite、临时目录和本地测试客户端；没有操作实际网盘、生产配置或业务数据库，没有提交或推送、没有发布镜像。

### 19.2 主要结果与边界

| 问题 | 修复后的行为 |
| --- | --- |
| OpenList 识别频繁进入人工审核 | 保留真正命中 TMDB 的查询分数，在决定是否调用 AI 前计入置信度；不再因脏分享标题或偏低文件名分数重复触发 AI。低置信、冲突、缺集号及 AI 明确要求复核仍拦截。 |
| 广告删除后再次审核、无法确认 | 保存只是改计划；确认时生成删除操作。广告映射保持可见，提供“继续完成确认”，保留已完成移动/删除和撤销信息，不通过重新扫描空暂存目录续跑。只有广告时明确完成清理但不标记入库成功。 |
| 确认后计划又被修改 | 映射修改撤销原批准；本地线程和 Worker 都校验批准状态，认领 run 在 SQLite 事务内比较计划 revision，拒绝检查与认领之间的并发修改。 |
| rclone 跳过却触发空目录上传 | `flock` 冲突退出 75；兼容识别旧脚本“本次跳过”的错误退出 0。未启动只退避重排、不唤醒上传；旧成功记录若中继为空，原任务重试可作废旧下载并重新下载。 |
| 退避异常、取消竞争 | 首次异步认领、结果落库或 timer 回调异常不丢重排；重试耗尽仅收敛终态、不再启动下载。状态写入带预期状态条件，取消先落库再停进程，事件写入失败不妨碍重排。 |
| 取消中继与天翼重试 | 取消使用只读观察，不调用会推进上传的 poll；天翼上传前的确定失败允许重试，已提交且结果未知仍保留防重复保护。 |
| 全量配置备份 | v2 包含账号选择、OpenList 连接和挂载修订；先解除登录会话外键；事务与运行时失败回滚保留原数据。拒绝缺修订或不一致引用。缺历史的旧 v1 文档明确拒绝，不自动编造修订。 |
| 管理员登录与会话 | UTF-8 字节比较支持中文；持久化认证修订撤销其他旧会话。个人资料更新原子复核身份，Cookie 只绑定本次写入，头像保存不覆盖并发改密。升级后旧 Cookie 需重新登录。 |
| 人工审核与后台安全 | 与访客入口共用清洗契约，保留天翼文件选择和媒体身份，无效选择拒绝扩大转存。审核理由等作为文本转义；后台资源版本统一更新。 |
| 发布检查 | 修复 Ruff 未定义类型及两处仍锁定旧前端缓存版本的测试断言。 |

### 19.3 本轮实际验证

| 范围 | 结果 | 执行时间 |
| --- | --- | --- |
| Organizer 识别、审核续跑、持久执行、版本竞争、路径边界与配套文件（10 文件） | 127 passed，19 subtests passed | 36.76s |
| 管理员认证、审核载荷、TMDB、HTML 与资源版本（8 文件） | 58 个唯一用例，8 subtests passed | 首组 4.01s；修正旧断言后补跑 0.82s |
| 全量备份导出、导入、失败恢复与引用校验 | 17 passed，89 subtests passed | 15.09s |
| rclone / relay / 天翼 / 取消 / 发布契约（11 文件） | 179 passed，4 skipped，9 subtests passed | 1.79s |
| 源码 UTF-8 无 BOM 与乱码守卫 | 1 passed | 0.75s |

合计 382 个唯一用例、125 个子用例通过，4 项跳过。认证组首轮唯一失败为旧缓存版本字符串；只修正遗漏断言并补跑失败项和设置 UI，没有用跳过测试掩盖失败。其余组本轮集中验证通过。本轮没有重跑耗时全量套件。

实际 Flask 应用隔离冒烟通过（197 条路由注册）：未登录 401、登录 200、改密 200、当前会话 200、旧会话 401、旧密码 401、新密码 200；外部请求数为 0，临时目录清理成功。

全仓 Ruff 与 23 个静态 JavaScript 文件的 `node --check` 通过，`git diff --check` 通过。251 个应用/脚本 Python 文件按 Python 3.11 语法解析通过；这是兼容语法检查，不等价于 Python 3.11 容器运行验证。交付文档确认 UTF-8 无 BOM。

### 19.4 部署与回滚注意事项

- 当前本机没有 Docker 和 Linux shell/flock 运行环境，4 项 Linux 专用用例明确跳过；未构建镜像、未进行浏览器或真实 OpenList/网盘端到端验收。已通过的 UI 验证是 Node 渲染函数/契约测试，不等价于真实浏览器测试。
- 已在 schema v29 的部署不新增数据库迁移；环境变量不变。请先备份完整数据目录和 `FNOS_ROOT_KEY`，在维护窗口同步源码，同时重建 Web 与 Worker，命令见 README“本轮整理 / 中继修复的更新方式”。只重启 Web 或拉取未包含修复的旧镜像不会生效。
- 更新后重新登录；中转故障优先重试原任务，不新建重复任务；已有整理执行记录优先“继续完成确认”，不要先重扫已搬空目录。
- 全量配置密文恢复要求相同 `FNOS_ROOT_KEY`，不支持跨根密钥迁移。旧 JSON 缺失的修订只能从原完整数据库恢复。
- 回滚保持 Web/Worker 同版本，保留数据和根密钥；不要用旧 v1 配置导入器恢复 v2 文档，也不要通过删除真实任务或手工清锁掩盖运行进程。
- 上线前仍需一次非生产小文件链路验收，包括实际 `flock`、跨盘中继、OpenList 延迟可见和人工续跑；本轮不把这些未覆盖项宣称为已验证。

## 20. 历史记录直接清理（2026-10-11）

- 系统维护的清理确认按钮改为“直接清理”，移除自动备份提示，前端明确发送 `backup: false`；服务层和仓储层的缺省值同步改为不备份。保留一次防误点确认，不改变清理范围，追更订阅、来源和系统设置仍保留；手动备份功能及 API 显式请求备份的兼容能力不变。
- 更新 `admin-system.js` 缓存版本为 `20261011-history-cleanup-v001`，避免更新后沿用旧的“备份并清理”脚本；没有新增环境变量或数据库迁移。
- 针对性验证：新增历史清理测试 6 项、8 个子用例通过（3.59s），覆盖默认不备份、未确认不执行、临时 SQLite 清理及保留项、重复清理、前端成功/取消/失败分支。既有设置 UI、资源缓存和源码编码测试 15 项通过（1.17s）。JavaScript 语法检查、改动 Python 文件的 Ruff 和 `git diff --check` 通过。
- 验证仅使用临时数据库及 Node 模拟上下文，没有清理实际历史数据、操作真实网盘或部署服务；未执行耗时全量测试或真实浏览器验收。

## 21. 剩余 10 项 Bug 修复与收尾（2026-10-11）

### 21.1 修复范围

对应同日项目审计的 10 项问题，保留本轮开始时已有的 47 项未提交修改/新增文件，在其基础上完成修复；未提交、推送或部署，没有操作真实账号、业务数据库或网盘。没有新增依赖、环境变量或数据库迁移。

| 审计项 | 本轮完成的修复 |
| --- | --- |
| 1. 活动任务清理及迟到回调串写 | SQLite `BEGIN IMMEDIATE` 内完成活动检查和删除；排队、执行、自动重试、未完成取消/回滚返回 `409 / HISTORY_CLEANUP_BUSY`，不删除任何记录。保留任务编号，rclone 完成写入同时核对原操作和执行批次，忽略已删除运行的迟到结果/日志。 |
| 2. 扫描与旧 apply 竞争导致未确认删除 | 持久认领同时检查任务状态、扫描租约、批准 revision；扫描认领原子撤销旧批准，扫描失败也不能重新使用旧批准。 |
| 3. 已移动文件改标题后找不到旧路径 | 明确阻止直接改目标或重扫，返回具体 `run #N` 的现有“执行记录 → 回滚”入口；安全恢复到冻结暂存路径后可以修改并重新确认。对已完成/已对账但缺 run 级 undo 的记录，根据本任务持久操作补齐可用入口，不猜测未确认移动的归属。 |
| 4. rclone 完成结果落库失败不恢复 | 捕获 `run_id + operation_key + started_at`，独立退避补写终态；补写不调用 start，不改下一任务内存状态，取消和新批次优先。 |
| 5. 天翼提交前读取失败锁死重试 | 实际 importer 用专用异常区分首次上传 API 前的本地检查/哈希失败；仅确定未提交的失败恢复 prepared，已提交或结果未知仍保留防重复保护。 |
| 6. Organizer 回滚状态不完整 | 专用持久运行、租约/路径锁、逐步进度、丢回复对账和幂等续跑；回滚开始即撤销 Job 完成状态，成功后重新待审核、恢复操作重新待执行。再次入库完成显式删除旧 rollback 标记。UI 显示“已回滚”、隐藏无意义重复入口并反馈冲突。已删除广告不承诺恢复。 |
| 7. 追更原 Job 重试不再同步 | 失败候选重新检查关联 Job，恢复执行进入在途、完成后只登记一次；候选超过 500 条也完整读取。回滚联动只撤销本 Job 与精确回滚路径的 seen、失效订阅快照，保留其它副本。撤销事务复核回滚批次和当前 Job 状态，避免旧同步删除重新入库的新证据。 |
| 8. 清理后通知误判已发送 | 清理不重置 Worker 序列；新任务分配和清理共用通知审计高水位保护。即使旧版已经重置过编号，也无需再清理一次就能避开旧通知 ID；不修改既有任务身份、不自动重发完成通知。 |
| 9. 启动恢复只取 100 条 | 固定启动扫描上界，按 ID 游标分页恢复全部记录，不重复读取仍在执行的首批，保留取消竞争校验。 |
| 10. 搜索复用缓存不续期 | 同一批写入原子更新/续期复用 ID 的内容；即使旧行在查询后被过期清理，也能恢复可用 ID。补齐缓存 raw_data 的 JSON 解码，详情与提交可以使用更新后的元数据。 |

### 21.2 本轮实际验证

以下分组文件不重复计数，合计 **360 个用例通过、102 个子用例通过、1 项跳过**；没有重跑全量套件。最终仅收集清单核对为 35 个测试文件内 361 个唯一用例，没有借此重新执行这些测试。

| 分组 | 最终覆盖结果 | 有效测试时间 |
| --- | --- | --- |
| 历史清理、跨连接竞争、ID 高水位及通知升级（1 文件） | 20 passed，67 subtests passed | 11.04s |
| rclone 完成补写、启动恢复、relay/天翼及相关仓储（10 文件） | 141 passed，1 skipped，7 subtests passed | 2.27s |
| Organizer 批准/扫描、回滚/续跑、路径边界与既有恢复（9 文件） | 113 个唯一用例、15 subtests passed | 首组 48.39s；针对失败及新增边界补测 19.58s、8.39s |
| 搜索刷新/缓存、追更重试/回滚同步与既有边界（7 文件） | 58 passed，10 subtests passed | 5.64s |
| 实际 Organizer + 临时 SQLite + 追更联动（1 文件） | 1 passed | 3.18s |
| 回滚 UI、后台资源版本/设置/文本安全、源码编码（5 文件） | 18 passed，3 subtests passed | 1.88s |
| 新 ID 分配的既有 Worker/通知事务、幂等及重试契约（2 文件选取） | 9 passed | 9.56s |

新增回归文件：`test_rclone_completion_recovery.py`、`test_organizer_rollback_safety.py`、`test_search_cache_refresh.py`、`test_update_retry_and_rollback_sync.py`、`test_organizer_update_rollback_integration.py`、`test_organizer_rollback_ui.py`；同时扩展历史清理及天翼上传既有测试。

真实仓储联动用例验证了：正常整理完成 → 追更记录完成 → 回滚 → Job/候选重新待审核、原 seen 被撤销 → 修改标题并重新确认 → 正常入库、追更恢复完成、旧 rollback 标记清除。外部 OpenList 操作使用内存替身，不能等同真实网盘验收。

首轮发现的问题均已处理，没有用跳过掩盖失败：缓存 raw_data 未解码、递归合并残留 rollback 标记已修复；新测试的两个 SQLite 句柄释放问题已修正。一次不支持 bulk API 的测试替身误入对账等待已主动中止，调整仅限该测试的替身设置，不修改真实 bulk 行为，也不计为通过结果。独立集成自审又补上回滚 seen 撤销的批次竞争和旧通知编号的升级入口。

全仓 Ruff 正确性检查、23 个 JavaScript 文件语法检查、`git diff --check` 通过。相对本轮基线的 33 个源码/测试/模板/主要文档文件验证 UTF-8 无 BOM；其中 28 个 Python 文件按 Python 3.11 语法解析通过，不等同 Python 3.11 运行验证。源码编码守卫通过；新增中文均经 UTF-8 文件编辑写入。后台 `admin.js`、`admin-organizer.js` 缓存版本更新为 `20261011-bugfix-v002`，其它未改资源保持原版本。

### 21.3 边界与发布说明

- 当前环境没有 Docker；未构建/部署镜像、未做真实浏览器和 OpenList/网盘上传、删除、回滚验收。1 项跳过为 Linux `flock` 专用测试。当前 Windows Python 3.12 测试不替代镜像内 Python 3.11 运行验证。
- 完成补写使用进程内定时器；补写完成前进程退出/强杀，未落库结果不能保证跨进程保留，启动仍走既有冻结路径的幂等恢复。补写定时器自身不重新执行搬运。
- 回滚遇到文件冲突或尚未确认的移动时保留证据并停止，需按提示先对账或续回滚；已删除广告不可恢复。不能用清理记录解除执行保护，也不能在未完成回滚时降级旧代码。
- 历史清理仍默认不自动备份，显式备份接口保持兼容；只改变活动任务安全边界和 ID 保留行为。旧版已经丢失的历史或发生的漏发不能自动还原，不会盲目重发可能已送达通知。
- 发布前在维护窗口停止接收新任务、等待任务安全结束并备份完整数据目录及根密钥，然后同步源码、同时重建 Web 和 Worker。README 已补充操作、发布和回滚说明。本轮没有执行上述发布操作。

## 22. OpenList 审核、广告选择与中继空目录复核（2026-10-11）

### 22.1 结论及证据边界

本轮对比已提交的 HEAD `06f0e4c` 与接手时的未提交工作区，保留既有及其它并行工作，没有重置、提交或推送。下面区分历史缺陷、已有修复和本轮新发现，不把之前的修改再次记作本轮交付。

| 用户反馈 | 复核结论 |
| --- | --- |
| OpenList 整理频繁人工审核 | 旧实现丢失备用文件名的 TMDB 实际命中分数，可能额外调用 AI 并进入审核。当前工作区已有分数保留和调用顺序修复，专项测试通过。未发现必然“100% 审核”的代码逻辑，也没有线上任务样本支持该比例；低置信、缺集号、目标冲突及 AI 明确要求审核仍会正常拦截。 |
| rclone 跳过后上传空目录 | 旧脚本抢锁失败却退出 0，造成“未下载 → 误记成功 → 上传空目录”。当前已有 `flock`/退出 75、旧日志兼容、退避重排及旧假成功作废重下载修复。日志不能区分真实并发与残留旧锁；手工创建空目录不能解决未下载问题。 |
| 广告删除后再次审核且不能确认 | 已保存计划的批准、广告行可见、继续收尾及纯广告清理已在接手前修复。但前端仍允许未保存选择时提交旧计划，保存一行或保存失败又会丢失编辑；本轮复现并修复这部分残留。 |

### 22.2 本轮新增修改

- `static/admin-organizer.js`：同一任务存在未保存映射时，拦截“整理/重试”并提示先“保存本条”，不发送请求、不关闭抽屉、不自动保存或授权删除。
- 保存失败保留当前选择；保存成功只刷新已保存行的服务端结果，其它行未保存的字段继续保留。就绪列表除前 20 条外额外保留有草稿的行，避免重新排序把草稿挤出预览；自定义下拉显示同步更新。
- `templates/admin.html` 仅更新 Organizer 资源版本为 `20261011-review-guard-v003`，同步缓存契约测试；新增 `tests/test_organizer_mapping_confirmation_ui.py` 7 个用例。本轮不修改 Organizer/rclone 后端、依赖、环境变量或数据库结构。

### 22.3 本轮实际验证

| 验证范围 | 结果 |
| --- | --- |
| `test_organizer_review_recovery.py`、`test_organizer_ai_tmdb_confidence.py`、`test_organizer_apply_recovery.py`、`test_organizer_durable_dispatch.py` | 43 passed，4 subtests passed |
| `test_rclone_lock_busy_and_relay_reconcile.py`、`test_rclone_completion_recovery.py`、`test_transfer_relay_safety_regressions.py` | 69 passed，1 skipped，7 subtests passed |
| 新增映射确认 UI 用例及 `test_admin_asset_cache_versions.py` | 8 个唯一用例通过 |
| `test_source_encoding.py`、`test_organizer_rollback_ui.py` | 2 passed |

合计 **122 个唯一用例、11 个子用例通过，1 项跳过**，没有重跑全量套件。未保存广告选择及预览截断两条新回归均先在未修复实现中复现失败，再验证修复通过；最终 UI 文件 7 项通过（0.80s）。JavaScript 语法、相关测试 Ruff、`git diff --check` 通过，新增中文使用 UTF-8 无 BOM 写入。

验证仅使用本地隔离测试、临时 SQLite、OpenList 替身和 Node UI 模拟；未连接生产环境或真实网盘。跳过项为 Linux `flock` 实机测试；当前 Windows 环境没有 Docker，不能据此声称用户运行中的 Web/Worker 已升级。

### 22.4 使用与发布提醒

广告选择须先“保存本条”，再“确认并整理”；已经执行、等待收尾的任务使用“继续完成确认”，不要先重扫已搬空目录。中继故障在包含修复的版本上优先重试原任务，不靠创建空目录或重复新建任务处理。

本轮未部署，也未更新根目录旧 `fnos-search.zip`。发布时应使用最新源码，同时更新 Web/Worker；沿用 README 中的备份、源码构建和回滚步骤。已有线上“100% 审核”是否持续，仍需实际运行版本及具体任务的审核原因验证，不能用本地回归通过替代线上结论。

## 23. 部署前全局复核与新问题收口（2026-10-11）

### 23.1 范围与结论

针对部署启动、配置与权限、账号/存储入口、传输最终核验、Organizer 恢复、追更及备份维护进行分工复核；保留既有和其它并行修改，没有操作生产服务、真实网盘或业务数据库，没有提交、推送或部署。以下 10 类为本轮新确认并修复的问题，不重复计入第 22 节及以前的修复。

| 新发现 | 触发与修复 |
| --- | --- |
| 1. 整理成功后最终核验错误统计公共目录 | 新一集放入已有 Season 目录，旧集导致总数超出；删除广告后保留总数又低于源总数。旧路径都会进入 `VERIFY_MISMATCH`。现按绑定 task/job/plan 的持久映射逐文件核验名称与已知大小，排除确认删除/跳过项，不让旧集或封面替代缺失文件；读后再次校验任务修订。 |
| 2. 跳过整理复用旧批准、覆盖恢复计划 | 生成直通计划后 revision 增加，但旧批准仍指向之前版本，API 排队后 Worker 拒绝执行。现原子批准新 revision，并阻止已移动、回滚未完成及丢回复后移动结果未知的计划被重建。 |
| 3. 手动追更候选提前完成，漏记 seen 无法恢复 | 复用 done Job 时先 completed 再写 seen，后者失败后同步永远跳过。现先保存 submitted 与 Job 链接，seen 成功后才 completed；失败交既有同步恢复。 |
| 4. PATCH 已删除保存位置会意外重建 | 指定 destination_id 却不存在时曾进入 create 分支。现返回 `404 TARGET_NOT_FOUND`，探测与写入都不发生。 |
| 5. 旧页面能覆盖新的默认来源账号 | 服务曾丢弃客户端版本并自行读取最新选择修订。现快照返回 `active_selections`，切换携带 `expected_selection_revision`；缺失 428、陈旧 409，显式 null 仅表示首次选择。 |
| 6. 拒绝账号保存前已经改了名称 | 禁止 credentials/config 的校验曾晚于改名。现先验证，账号切换与独立改名分离；前端切换不再携带旧名称，失败不写入。 |
| 7. 位置冲突刷新后仍持有旧版本 | 冲突后重载页面但保留旧 editor revision，再保存仍冲突。现保留输入、不伪称已经恢复；提示取消编辑、刷新配置并重新打开核对，不悄悄提升 CAS 版本。 |
| 8. 文档中的四个传输超时变量未进容器 | `.env.example` 和 dispatch 支持 `TRANSFER_*_SECONDS`，Compose 却没传入。现同时透传 Web/Worker，默认值不变；更新环境变量需重建容器而不是 restart。 |
| 9. OpenList 配置失效时后台可能无法启动修复 | Web 等待 Worker 的业务 `/readyz` 健康，缺挂载时 Worker 永远未就绪，后台也无法启动。现 Worker 容器以 `/livez` 作为初始化完成屏障，Web 仍等待该健康屏障；不使用 `service_started` 避免首次共享数据库初始化竞争。Web 健康检查及 `/readyz` 的严格业务判断保留。 |
| 10. 全量配置导入可以覆盖在途任务使用的账号/修订 | 已复现 submitted Job 与 pending Worker 保留、账号却被导入删除。现写事务内阻止排队、执行、恢复任务及 waiting/polling/persisting/persisting_active 登录授权；重载失败补偿前再次检查，若新任务/授权已开始，不强行回滚其配置。返回 `CONFIG_RESTORE_BUSY` 或明确回滚未完成。 |

### 23.2 修改范围

- 传输：`transfer/dispatch.py`、`orchestrator.py`、`verifier.py`；新增 `test_transfer_organizer_manifest.py`。旧 cleanup-only 测试替身仅补实际数据库已有的 mapping.status 字段。
- 整理/追更：`organizer/service.py`、`services/update_candidate_import_service.py`；新增 skip 恢复和候选完成写入顺序测试。
- 存储：`storage/setup_service.py`、`static/admin-storage-v1.js`、`openapi.py`；新增 HTTP/SQLite、Node 交互及 API 文档契约测试。Storage 静态版本升为 `20261011-storage-cas-v004`，保留上一轮 Organizer 版本。
- 部署/维护：`docker-compose.yml`、`services/backup_service.py`、部署契约与备份活动状态测试、README。无新增依赖或数据库迁移；四个超时环境变量是补齐已有设置的容器透传。

### 23.3 验证结果

| 分组 | 当前通过的唯一用例 |
| --- | --- |
| 部署编排、Worker 健康与启动装配（3 文件） | 36 passed |
| 完整配置备份及新增活动任务/授权保护（2 文件） | 21 passed，95 subtests passed |
| 最终任务文件清单及 Organizer/cleanup-only 兼容 | 21 passed，12 subtests passed |
| 跳过整理恢复、手动候选补写及 3 个相关既有叶子 | 9 passed |
| 存储 API/界面/快照契约、账号与位置兼容、资源缓存 | 18 passed，15 subtests passed |
| 全仓源码编码守卫 | 1 passed |

合计 **106 个唯一用例、122 个子用例通过**；没有执行全量测试，没有将复测次数重复累计。新增失败场景先经静态加隔离复现或红测确认。核心回归耗时约几十秒，不运行真实网盘慢链路。

实际 Flask 装配冒烟注册 197 条路由：未登录读取存储配置 401，登录、配置快照与 OpenAPI 200；正常 `/readyz` 200，启用但缺 OpenList 时 `/readyz` 503，而 `/livez` 及修配置入口仍 200。外部请求数 0，使用临时数据库和假 Worker 状态；不等同实际容器或网盘验证。

全仓 Ruff、23 个 JavaScript 的 `node --check`、253 个应用/脚本 Python 文件的 Python 3.11 语法解析及 `git diff --check` 通过。中文由 UTF-8 无 BOM 文件编辑写入；Python 3.11 语法通过不等于在该版本运行过。

验证期间的调整：备份新测试首次误用不存在的仓储 get 方法、skip 内存替身漏建分类根，均修正测试后只补测失败项；没有放宽生产校验。隔离启动冒烟的首轮退出清理先于关闭 Windows 日志句柄，调整顺序后清理通过。交叉复核额外补齐了启动初始化屏障和活跃授权阻塞，未以已有绿测替代边界检查。

### 23.4 部署与剩余边界

- 必须同步最新源码及 `docker-compose.yml`，同时重建 Web/Worker；仅升级镜像但继续使用旧编排，超时透传和启动屏障修复不会生效。根目录旧压缩包本轮未重打包，不能把它当成此次交付版本。
- 更新后强制刷新后台，账号切换使用新的版本快照契约；API 调用方需按 OpenAPI 传选择修订，不混合激活和改名。
- 旧 Organizer 记录缺少可靠任务级完成证据时保持复核，不凭公共目录里已有文件判定成功。来源大小未知只核验准确目标文件存在，不声称完成内容哈希校验。
- 备份导入的事务内空闲检查不是覆盖整个运行时重载期间的维护锁；仍须停止接收新任务、完成或取消登录授权。若补偿被新工作阻止，应核对运行时和配置后人工恢复，不反复强行覆盖。
- 当前 Windows 环境无 Docker/Linux 实机验证，未做真实浏览器及多网盘端到端验收。建议部署后先用非生产小文件验证“新增一集进已有目录”“广告删除后收尾”“跳过整理”“账号切换”，再批量执行。
- 已处理本轮确认项，不宣称全项目不存在其它缺陷；用户自行部署的具体结果尚未验证。回滚保持 Web/Worker 与编排同版本，先备份数据及根密钥，不删除运行记录来规避保护。

## 24. 多网盘六项缺陷修复及交付（2026-10-11）

### 24.1 范围与用户确认

本节对应 `.codex/multicloud-audit-2026-10-11.md` 的六项问题，保留此前修复及第 22、23 节的并行修改，不覆盖其它任务的 Organizer、备份或部署变更。本轮源码基线保存在项目本地 `.codex/multicloud-fix-20261011-baseline`。

用户明确选择：**已有账号“重新登录”若属于另一真实网盘账号，拒绝覆盖，要求新增独立账号**。实现不使用昵称或可编辑用户名猜测身份；身份不能确认时也不会默许覆盖。

| 审计项 | 最终修复行为 |
| --- | --- |
| 1. 换真实账号复用旧映射/证明 | Provider 探测返回可信稳定身份，作为内部元数据保存在不可变账号修订；不同身份拒绝发布。凭据变更但无法证明同一身份时，交互重登录拒绝，原凭据、选择和映射不变。旧账号可在事务外只读验证旧凭据后比对；不能验证则要求新增账号。手动修订仍保留旧映射失效防御，可信自动刷新必须匹配 expected_revision。 |
| 2. 部分分类被扩大成全分类 | 只要有分类未分配，展示默认目标保持空，已配置分类用显式 overrides 表示；电影单独配置经过读取、界面回填、再次保存不会扩大范围。 |
| 3. 旧页面删除新网盘路由 | routing_token 覆盖当前规则和历史版本高水位，SQLite 写事务中统一校验新增、更新、删除及 ABA；null 明确表示读取时不存在。两个真实 HTTP PUT 入口均强制快照，缺失 428、陈旧/无效 409。前端保留未保存选择，不自动刷新重提。 |
| 4. 登录恢复后旧认证熔断继续阻止 | 停止状态绑定 Provider、账号、凭据修订，新修订可尝试；同一旧修订保持停止，不污染其它账号或新修订。旧裸账号 ID 不会错误绑定到首次遇见的新修订。已有完成/未知上传回执优先处理，不盲目重传或自动换账号。 |
| 5. 禁用期间异步登录抢回默认 | prepare 和最终提交都检查身份/版本，最终写事务再次检查 enabled；即时登录 helper 也走同一保护。已有账号重新登录不会覆盖管理员已选的其它默认账号。 |
| 6. OpenList DB/Remote 并发错配 | DB 目录旁两层内核锁分别协调完整配置操作和实际 Remote 写入，网络期间不持 SQLite 写事务；RPC 绑定 connection_id/revision/owner token。确认 HMAC、标记与数据面同时绑定 Remote 名称及规范化配置路径，status/test 还比较实际 URL/user。失权或未知结果不做猜测补偿、不显示成功；确定拒绝安全恢复并重新确认旧配置。 |

同步完善了 OpenAPI 的规则 GET/PUT 快照契约，以及 Web/Worker 对 rclone 错误 code 的透传。现有并行版本中的 `admin-storage-v1.js` 缓存标记保留为 `20261011-storage-cas-v004`，没有将其它任务更新过的静态版本降回本轮中间版本。

### 24.2 实际验证

中断前各组均已完成执行；恢复后只核对源码、结果清单和文档，不重跑全量套件。按测试 node_id 去重后，本轮执行范围为 **417 passed、1 skipped**。下表分组有交集，不能直接相加；也没有把其它聊天第 23 节的 106 项结果算入本节。

| 分组 | 本轮执行结果 |
| --- | --- |
| 账号身份、禁用竞争、自动刷新、Provider 探测与旧绑定 | 94 个唯一用例通过；包括新身份保护文件 24 项。最终严格政策补测 14 passed / 15.12s，绑定 revision 刷新补测 4 passed / 4.02s。 |
| 中继认证修订隔离及执行契约（5 文件） | 143 passed、1 skipped、13 subtests passed / 1.65s。 |
| 路由往返、完整快照、HTTP 与 Node 表单 | 23 个唯一用例、5 subtests passed；首组 16.32s，夹具修正后仅补测两项 1.57s。 |
| OpenList 保存、双锁、超时、迟到 RPC、Remote scope、已有 rclone 契约 | 合并去重 81 个用例通过。初组 54 passed / 8.89s；新并发文件最终 22 项均通过，scope 主组 40 passed / 6.00s，最后检测竞争项 1 passed / 0.77s。 |
| OpenAPI、错误 code、静态版本、后台设置 | 25 passed、8 subtests passed / 0.65s。 |
| 多链路、计划修订、路径、原生协议集成（4 文件） | 39 passed、4 subtests passed / 13.61s。临时数据库清理补齐新增锁文件后，仅复测两项 2.09s。 |
| 与并行账号选择/保存位置修改的合并边界 | 路由、API、资源版本和 mutation-safety 合并验证 29 passed、26 subtests passed / 13.87s，其中 12 项是上述分组之外的补充联动验证。 |
| 源码编码 | 1 passed / 0.81s。 |

验证使用临时 SQLite、内存网盘/FakeRunner、Flask 测试上下文与 Node 模拟页面，不调用真实网盘或 rclone。独立复核发现并补齐了“切换 MP 为 OTHER 后旧确认仍可复用”的范围遗漏；原隔离场景最终返回 confirmation_required、数据面校验 false、test 拒绝为 RCLONE_SYNC_RESULT_UNKNOWN。

新测试中曾有夹具取错返回字段、方法参数形状及 DOM 替身缺失，均只修测试后定向补测；没有降低生产身份、路径或并发检查。用户确认安全策略后进一步收紧了交互未知身份和无 revision 的凭据刷新写入。前一次中断发生在最终联动测试已经输出通过之后，没有遗留运行中的 pytest 进程。

全仓 Ruff 正确性检查、23 个 JavaScript 文件语法检查和 `git diff --check` 通过。恢复时仅用 collect-only 核对 29 个测试文件内 418 个唯一节点（417 通过、1 跳过），并按 Python 3.11 语法解析 253 个应用/脚本文件；没有重新执行这些测试。新中文和交付文档均验证为 UTF-8 无 BOM。语法解析不等同 Python 3.11 运行验证；本节不沿用更早轮次的全量测试数量。

### 24.3 兼容、发布与未覆盖边界

- 用户确认的策略已生效：跨真实账号覆盖被拒绝。旧凭据无效且无法取得可信稳定身份时，需要新增独立账号；昵称相同不作为放行依据。旧版已产生的物理账号错配或数据损失无法凭空自动修复，应重新核对映射。
- 规则 API 先 GET 获取 routing_token，再原样放入两个 PUT 入口；旧客户端缺 token 会收到 428，冲突 409。保存后重新 GET，新旧客户端不能混用无版本替换。
- **同时更新 Web 和 Worker 后，在“网盘入库 → OpenList”重新保存一次连接，建立 Remote 确认。** 单纯“检测”不能替代此步骤。Remote 名称/配置路径改变、恢复数据库导致指纹不匹配时也需要重存确认；未确认前跨盘被阻止，直接原生/OpenList 凭据解析不受此 Remote 门禁影响。
- 本节没有新增 Schema、环境变量或依赖。共享 DB 目录新增 `.openlist-` 锁、owner 和确认文件；Web/Worker 需要共享同一目录及可靠文件锁权限。运行中不要删除这些文件或手工修改托管 Remote 来绕过确认。
- 网络同步结果未知时保留当前 DB 修订并停止跨盘，不误称已恢复；等待原操作结束、核对后重新保存。新账号修订可恢复新任务，旧任务冻结修订和未知上传回执不会被自动改写。
- 未进行真实 Docker/Linux 容器、多网盘端到端或断电故障验收；1 项跳过为 Linux 专用用例。保持此前 README 的备份、维护窗口和同时重建 Web/Worker 步骤，并包含新增源码文件；没有推送、部署或操作真实账号/业务数据。

## 人工确认与 rclone 入口闭环（2026-10-11）

### 修复与边界

- 已关联任务的访客请求 `review` 不再误标“低风险 / 可自动通过”，列表和详情提供“处理确认”，进入原任务；不通过再次批准制造新任务。
- 任务详情显示持久执行阶段、真实复核原因和冻结计划中的目录。有回执的任务可受保护地继续；无回执且已有提交栅栏时，必须人工核对无本次文件及未完成网盘操作，填写说明并再次确认，才能重新提交。
- 新增管理员 POST `/api/admin/jobs/{job_id}/review`。确认要求正整数 `expected_row_version`、显式布尔确认和 1–1000 字说明；事务内复核任务状态、回执、已验证结果和活跃租约，保留原 fence、管理员及说明的审计历史。不修改 Schema、不删除任务、不直接标成功、不将失联等同于未执行。
- 普通重试不能通过把恢复阶段改为 planning 等方式绕过未知 fence。人工确认后仍复用原 job 与冻结计划；审计在后续 Worker 保存时保留。
- 入队必须获得明确确认并核对真实队列任务绑定和状态；被取消/已结束的同 key 队列记录不再伪报已入队。确认已落库但入队未知返回 503 + `recovered:true`；页面刷新且失效旧确认，不误报成功、不重复清除栅栏。
- rclone 页面旧全局启动按钮改为“查看入库任务”；后台旧入口返回 `409 / RCLONE_JOB_REQUIRED`，不调用全局扫描。数据面仍要求有效的 V1 任务描述，提示改为 UTF-8 中文。
- 回执保存校验失败不再统一误报 Worker 并发；首次提交错误及转人工复核保留实际脱敏原因，写入任务事件与 WARNING。用户提供的 179 行容器日志只有启动、健康探针及接口访问记录，不能据此确认所有缺回执任务的底层原因；没有宣称该底层故障已经复现或修复。

### 验证

针对本次风险选择 12 个测试文件，最终 **133 个唯一用例通过，145 个 subtests 通过**。首轮 132 通过、1 个新增测试夹具漏设 `enabled`；只修夹具并补测该文件 11 项全部通过，两次 pytest 总执行约 7 秒。没有重复运行全项目测试，子代理分组结果不重复计数。

交付前保留并行修改提升的 `admin-jobs.js` 版本 `20261011-request-retry-v006`，没有降回本轮中间版本；仅补验相关 UI 和资源缓存 18 项，约 3 秒全部通过，这些已包含在上述唯一用例内。

覆盖请求/任务按钮分流、说明与取消确认、双击、业务失败/503、冻结目录与转义、管理员 POST 路由、版本过期、回执/取消/租约保护、审计续存、真实内存 Worker 队列取消竞态，以及原有取消/调度/接口文档/缓存版本和 UTF-8 编码检查。定向 Ruff、两份 JS 语法检查、Python 编译及 `git diff --check` 通过。

### 发布与限制

修改发生在本地工作区，未连接或部署到 NAS，未使用真实 Provider 账号、删除数据或执行真实转存。须将完整更新源码（包含新增服务）同步到 NAS，保留其 `.env`、原 `FNOS_ROOT_KEY`、数据及挂载目录，按维护窗口同时重建 Web/Worker。无新增环境变量、依赖或数据库迁移。

“未执行”是管理员的明确核对声明，不是程序对远端的证明；核对错误仍可能重复提交。已存在文件/任务或结果仍未知时不得选该项，保留证据继续对账。回滚前停止新的执行并保存数据库和复核审计；不要让旧版本覆盖新增审计，也不要直接恢复旧库后自动重放已发生的外部操作。

## 升级后 OpenList 自动确认：后端根因修复（2026-10-11）

### 根因与修复

用户进一步提供“来源已转存到夸克，后续搬运因 Remote 身份确认失败”的证据，并明确要求修复后端而非修改 UI。本轮没有新增 UI 改动。

实际问题不是升级主动删除标记，而是旧部署可能没有新版要求的确认 sidecar；自动补写位于 run 启动阶段，晚于冻结路径校验，并且发现 Remote 已配置就直接跳过。因此“已有 Remote、缺少确认”始终到不了自动恢复逻辑。

- 新增 Worker-local `ensure_confirmation()`，在 Worker 启动、消费传输队列之前执行，并在新搬运提交的前置校验中再次调用。身份不变时自动恢复确认，不要求每次升级后人工重新保存连接。
- 缺标记、旧确认指纹、已释放锁的中断同步，以及缺失 Remote，均沿用已保存的唯一启用连接，通过真实配置同步、读回与连接探测恢复；不修改连接修订或冻结计划。忙锁或临时 5xx 在无提交 fence 的前置阶段有界退避，不重新转存来源。
- 已确认状态绑定实际 Remote 类型、URL、用户名、密码配置及 scope 的摘要；没有变化时不重复探测或写 Remote。密码配置漂移需要真实重新核验，不能仅凭“pass 非空”放行。URL、用户名、类型或已知 scope 漂移继续拒绝自动覆盖。
- 保存确认保持 change→remote 锁顺序及 owner/revision 防护；Web 回写保留 Worker 已验证的实际配置摘要，pending/unknown 不继承旧摘要。
- OpenList/rclone 只读前置校验移到新提交 fence 之前。明确未启动的校验失败不再变成“已提交但缺回执”；历史未知 fence 仍先拦截，不依据错误文本或空 run 查询自动解除。
- 来源可见交接单独保留来源 receipt/fence，后续只恢复搬运。明确失败/取消的回执保存阶段与操作身份后才能释放；异步 poll 的确定失败也写回回执，避免只轮询失败任务而不真正重试。测试验证来源转存 1 次、搬运重试 2 次。
- 进程启动前保留最后一次 Remote 检查；真实确认失败不会被吞掉后继续 spawn，已认领 run 会安全结束或沿用完成补写机制，不留假运行状态。

### 验证

| 范围 | 实际结果 |
| --- | --- |
| 自动确认、跨 Worker 并发、原配置服务、启动接线、搬运前置/恢复、原执行器及编码（12 文件） | 219 passed，53 subtests passed，7.13 秒 |
| OpenList 保存接线及既有 mutation-safety（2 文件） | 63 passed，13 subtests passed，43.57 秒 |
| 最后进程启动门禁、启动接线及旧补写兼容 | 24 passed，6 subtests passed，0.77 秒；其中 7 项为新增，其余与首组重合 |

上述最终验证覆盖 289 个唯一用例，未运行全仓测试。使用内存数据库、假 rclone、Mock 及源码接线检查；未连接 NAS、真实 OpenList 或网盘。定向 Ruff、应用模块导入和差异检查通过。保留工作区原有并行改动，未修改真实配置、删除记录或推送远端。

### 发布边界

同步完整更新源码并同时重建 Web/Worker，保留原 `.env`、根密钥、数据库和 rclone 配置卷。无新依赖、环境变量或数据库迁移；旧确认缺摘要由后台自动验证升级。正常身份未变化的升级不再需要手动保存 OpenList。

已经被旧代码标记为未知提交的任务不会被批量清 fence 或重新转存；新代码不会凭日志文本伪造历史未提交证明。来源已存在时必须保留原文件。真实连接身份发生变化或确认文件损坏仍需处理具体错误，不能当作普通升级自动放行。回滚应先停止任务并保留数据库、配置和确认 sidecar；旧版本可能再次要求确认，不能以删库或重放外部操作代替回滚。

## 下载、上传实时日志与进度修复（2026-10-11）

### 证据与实际修复

用户日志中 405 条下载统计均显示为 `22:32:34`，但随后有 `22:32:37` 下载结束、唤醒原传输任务，以及
`22:33:04` 移动云上传完成的记录。能够确认下载后已进入上传，不能从这份日志直接认定后续整理、ACK、清理也已完成。

- 脚本改用 POSIX `read` 建立逐行输入边界，再交给原 awk 格式化；不再仅依靠 `fflush()` 解决输出缓冲。
  一些 awk 对非终端输入的整块等待不受 `fflush()` 影响。新增活进程测试使用真实假 rclone 和真实 awk，
  并用等待 EOF 的输入缓冲模拟做对照：旧长管道不能及时输出，新管道在生产者仍等待父进程确认时已交付两条进度。
  未直接查询 NAS 的 awk 版本，不将特定实现猜测写成实测事实。
- 脚本版本升级为 `v1.2-live-progress`；统计仍默认每 5 秒输出。看门狗覆盖整个 copy 调用，持续报告等待或
  进度值未变化，不在第一条 0% 后停止、不在 300 秒后失声。保留 stderr、退出码、flock 和临时文件清理。
  部分下载失败不会再宣称已移交上传。
- 原生上传开始立即输出日志；普通进度节流，阶段/文件切换及首次 100% 不被吞掉。日志桥使用有界异步串行通道，
  不阻塞原生上传；无新有效进度默认每 30 秒给出准确等待提示，hash 与上传字节分开。调用结束后停止心跳并拒绝迟到回调。
- 新增任务级内存 `status.transfers`，下载、上传及 hash 分离，提供已有的百分比、字节、速度、ETA、更新时间。
  不知道的数据为 null，不虚构总任务进度。心跳不刷新字节进展时间，不继续展示过期速度；最多保留 50 个任务。
- 前端每 3 秒独立刷新状态、搬运日志、系统日志，到达即可渲染，慢接口不再拖住已经返回的日志。
  显示真实分阶段进度条，hash 不当已上传；下载进程空闲不再掩盖随后仍在进行的原生上传。失败保留已显示日志。
- 只有持久执行保存成功且业务任务确为 done 后，才通过可信应用通道输出“入库阶段：completed / 入库完成”。
  上传完成、100%、原始 stdout 仿造的完成标记均不能把进度状态标为入库完成；后置阶段不被迟到的上传日志倒退。
- 事件批量落库保留接收时间，兼容原三元组调用；外部上传任务不会串入另一任务当前下载 run 的历史日志。

### 验证

最终定向集合：**177 passed、3 skipped、36 subtests passed，15.12 秒**。覆盖真实生产者未结束时的两次下载进度、
Python 逐行读取、持续静默/停滞反馈、阻塞上传期间的进度与心跳、阶段切换/100%节流、日志桥阻塞不阻塞上传、
慢状态接口不阻塞日志、数据库堵塞不堵内存日志、任务隔离、可信完成态及既有传输/回调恢复。

3 个跳过项为旧 awk 直接调用测试的 PATH 环境限制；本轮新增的 Git Bash/真实 awk 活进程测试均实际执行通过。
同步更新了旧“一次性看门狗”静态断言和两个假定同步上传日志的断言，使用事件等待而非长时间睡眠。
Node 语法、Shell 语法、定向 Ruff、应用导入通过。未用真实网盘或 8 GB 文件进行耗时测试，未更改转存、上传或完成判定以制造成功。

### 交付边界

当前修改仅在本地工作区，未重启 NAS 或中断现有任务。完整同步源码后，在活动下载/上传安全结束时同时重建 Web/Worker；
启动日志应显示 `v1.2-live-progress`，前端资源版本为 `20261011-live-progress-v007`。无新依赖、端口或数据库迁移；
默认设置即可使用。实时指标是内存快照，重启后等待新反馈；实际任务状态仍以数据库为准，不以进度条代替校验和入库确认。

## 夸克子目录文件选择与转存修复（2026-10-12）

### 已复现的问题

- `list_files()` 返回经 HTTP/业务码验证的真实 Quark `{code: 0, data: {list: ...}}`，不保证带 `success`；
  深层 `_walk_to_fid()` 却强制 `success is True`，把成功列表当成失败，最终误报没有可转存文件。
- `file_type=0` 的目录标志被 `or` 表达式丢掉；没有额外 dir 标志的目录可能无法匹配 root_dirs 选择。
- 公众页将所有“已加载子项”被选中自动转换为 root_dirs，并清掉原子项。外层框自动勾选不是单纯视觉联动，
  而是实际扩大提交范围；截断预览尤其不能代表用户选了整个父目录。
- 工作台多次下钻覆盖 root_items，返回根目录实际回到上一级，却清除了子目录上下文，可能提交错误层级的根目录 ID。

### 修复

深层读取沿用已验证的 code/status 契约，子目录显式请求分享上下文并使用本次读取的新 token；数值 0 目录正常识别。
逐个核对选择 FID、路径末项和 token，任一条目缺失整批拒绝，不提交子集或整个分享；选择有效后才解析/创建目标目录。
单次 save 保留声明的 base_dir 上下文，根选择使用 0，不擅自拆成多个无聚合回执的保存任务，也不新增跨子目录选择限制。

公众页子项选择和最终 payload 均保持 subdir_items，不自动升级整目录；显式父目录操作不变。工作台只保留首次真根列表。
清洗器正确识别目录布尔/数值标志；无效、超限或部分无效的显式选择返回拒绝，不静默丢选。提交与审核入口拒绝无效非空选择；
公开提交拒绝内部选择载体，避免它们覆盖规范 quark_selection。确定选择错误以 SHARE_INVALID 传到操作回执，停止无意义自动重试。

### 验证和边界

**166 passed、45 subtests passed，3.59 秒**。覆盖 Quark 原始 code=0 响应、两级目录、fresh token、只选子文件、
跨子目录精确选择、末项 FID 不匹配、缺 token、数值目录类型、截断预览不扩大范围、多次下钻返回真根、
真实清洗→SourceResolver→加密来源快照→Adapter→Importer/FakeHTTP，以及新建/审核一致性和错误码重试策略。
同时回归现有 Quark、其它 Provider 选择、公开页提交提示、静态缓存与 UTF-8 编码；不跑真实网盘或大文件传输。

前端 `public-submit.js` 和 `admin-trending.js` 版本提升为 `20261012-quark-selection-v008`；不影响其余已有资源版本。
没有改写旧任务、旧来源快照或真实账号，没有自动重试 job 17。若旧请求已丢失子项而只保存父目录，更新不能恢复原始勾选意图；
应在新版重新选择需要的文件。正常保留 subdir_items 的旧快照可使用修复后的解析逻辑；真实条目缺失仍会明确拒绝。
同步完整源码并同时更新 Web/Worker，保留数据库与原根密钥，无新增依赖、环境变量或 Schema 迁移。

## 25. 正常功能链路复查（2026-10-11）

### 新确认并修复的功能问题

| 问题 | 修复与验证要点 |
| --- | --- |
| 整分享统计未知时，成功下载后仍进入人工审核 | 冻结计划的文件数/大小为 0/0，即使成功 run 和回调清单已经落库，旧暂存闸门仍返回“计划缺少冻结的文件数或总大小”。现从成功 run 的受信清单逐文件核验，关联 job、operation、复制描述符和原计划；有无 Organizer 都适用。关闭 Organizer 时归档后再次核验最终文件。原有已知数量/大小、relay/native 及 Organizer 最终校验不变，缺证据仍不放行。 |
| 任务操作失败像按钮无效 | 批准、拒绝、取消提交、取消任务、删除记录缺少 API 异常反馈。现显示具体失败原因，失败不关闭抽屉、不自动重试。共享工作区并行修改已补齐 retryJob，本轮保留并验证，未覆盖其人工复核入口。 |
| 失败且尚无正式任务的访客提交无法真正重试 | 原 Worker 已 failed，再批准仍返回 queued，但 Worker 不会重新执行。现提供“重试提交”，显式 `retry: true` 才能恢复 failed/no-job 请求；请求状态与入队在同一 SQLite 写事务提交，Worker SQL 仅重新激活 failed，拒绝已完成、取消、排队或执行中的记录。 |
| 真实读取丢失审批的文件选择及 Worker 元数据 | GuestRequest 查询返回 raw_data JSON 字符串，服务仅接受 dict，导致忽略保存的选集/排除项。现各请求查询入口统一解码有效 JSON，沿用既有空/损坏数据兼容行为；普通批准和失败重试均用真实仓储验证原选择保留。 |
| 废弃的过期登录会话一直阻止备份导入 | waiting 会话以前只有再访问 get/poll 才过期；被关闭或切换的登录窗口可能永久留下阻塞。现备份事务复用正常登录过期规则收敛已经过期的 waiting；CAS 保护保留，polling/persisting/persisting_active 不会只因过期时间被清除。 |

主要修改为 `transfer/dispatch.py`、`rclone_executor.py`、`orchestrator.py`，请求审批/队列协调服务及 Worker 仓储、GuestRequest 查询仓储，`accounts/auth_sessions.py`、`services/backup_service.py` 和 `static/admin-jobs.js`。同步 approve API 文档、README 与缓存版本；不改数据库结构或依赖。

工作区其它并行任务在本轮修改了人工复核 UI、相关服务及文档，本轮按最新文件进行局部合并，保留 `REVIEW_COPY`、任务复核按钮和已有 retryJob 防护，没有回退或计入本轮新增修复。仅将本轮修改的 `admin-jobs.js` 缓存更新为 `20261011-request-retry-v006`，其他资源版本保持各自最新值。

### 本轮实际验证

| 范围 | 结果 |
| --- | --- |
| 新 rclone 清单暂存闸门、Organizer 最终清单及原 Organizer 编排 | 23 passed，19 subtests passed（6.18s） |
| 备份等待授权过期/在途保护及既有登录过期 | 7 passed，9 subtests passed（6.33s） |
| 配置保存、前端补丁合并与重载失败回滚 | 6 passed（1.86s） |
| 请求读取 JSON、选择项、分页去重与事件兼容 | 3 passed，7 subtests passed（2.74s） |
| 失败请求恢复、原审批选择、Worker 兼容、按钮成功/失败/取消与 API/缓存 | 24 passed，46 subtests passed（7.93s + 0.79s + 0.81s） |
| 源码 UTF-8/无 BOM/乱码守卫 | 1 passed |

合计 **64 个唯一用例、81 个子用例通过**，不含之前轮次结果，不把失败项复测次数重复累计，未执行全量测试。新问题均有代码路径和隔离复现依据；完整非 Organizer 流程的首次失败来自挂载替身遗漏 writable/identity_status，补齐真实字段后通过，没有放宽实际路径校验。

额外正常操作回放：使用当前 Storage JavaScript 的真实事件生成请求，再交临时 SQLite 的 Flask API 执行，账号改名、切换、创建本地保存位置、编辑及删除共 **5 个操作全部成功**（2.51s，外部请求 0）。该区域本轮未发现新的正常操作缺陷，没有为此修改存储代码。

全仓 Ruff、23 个 JavaScript 语法检查、254 个应用/脚本 Python 文件的 Python 3.11 语法解析、`git diff --check` 通过。查询、重试和清单验证均用真实本地仓储配合替身，不等同真实浏览器或网盘验收。

### 使用及验证边界

- 失败但无正式任务：在请求列表或详情使用“重试提交”；有正式任务时进入任务页处理，不复活已取消任务。
- 统计未知的 rclone 清单当前证明准确文件存在及成功 run 关联，不宣称有清单未记录的大小/内容哈希证据；有冻结数量/大小时仍执行原比较。
- 有效的 pending/polling 或冻结登录结果仍阻止配置覆盖；只自动结束按原规则已过期的 waiting。
- 更新需同步完整最新源码及新增文件，重建 Web/Worker 并刷新后台缓存；本轮无新增环境变量或迁移，未部署或重打包旧压缩包。
- 未操作生产服务、真实账号、网盘或业务数据库，没有提交/推送。没有 Docker/Linux 容器及真实网盘端到端证明，不能把本地回归通过等同线上不存在其它问题。

## 26. 小中继盘逐文件闭环（2026-10-12）

### 实现与安全边界

- 移动云、天翼中继改为：单文件下载 → 原生上传 → 远端身份和大小核验 → fsync 成功回执 → 删除该文件缓存 → 下一文件。
- `SequentialRelay` 保持外层任务幂等键不变，内部使用逐序号的下载 run 和上传键；完整源 manifest 仍保留供最终校验及 Organizer 使用。
- `/data/relay/job-N/.fnos-relay-upload-sequential.json` 持久化 pending/downloaded/verified/cleaned；删除媒体后仍保留小型原生上传回执，合并为原任务 manifest，复用最终校验与 ACK。
- 下载前检查当前文件容量加 64 MiB 安全余量；大小已核验的任务级完整缓存直接复用。运行中的磁盘满/配额不足不再触发同文件的盲目下载重试。
- 未验证、写回执失败、未知上传、取消存在外部上传结果时保留数据；源落地区仅在整个任务最终确认后按原计划清理。
- 旧批量上传已有 completed 回执时复用原幂等结果，先复核远端；旧上传状态不确定时人工复核，禁止换键重传。

### 针对性验证

- `tests.test_sequential_relay`、`tests.test_sequential_relay_recovery_guards`、`tests.test_relay_single_file_worker`、`tests.test_transfer_relay_safety_regressions`：50 项，49 通过、1 项 Linux flock 实机测试因 Windows 环境跳过。
- `tests.test_rclone_managed_descriptor_v1`、`tests.test_rclone_staging_manifest`、`tests.test_rclone_live_streaming`、`tests.test_transfer_v1.ProviderRelayTests`、`tests.test_transfer_v1.RelayProviderTests`、`tests.test_relay_auth_revision_isolation`、`tests.test_relay_live_upload_progress`：62 项通过。
- `tests.test_relay_uploader_file_selection`：10 项通过（移动云/天翼精确选择、删除后回执对账与验证、合并、非法路径和持久化失败）。
- 共 121 项通过、1 项平台跳过。实际 shell + Python CLI 假数据验证了每 run 只下载 1 个、完整源清单未裁剪、源文件不被删除、不能越过未清理文件、空间不足不开始下载、磁盘满不重复下载。
- 新增/修改 Python 定向 Ruff、shell `bash -n`、`git diff --check` 和新增源文件 UTF-8 无 BOM 检查通过。未运行全量测试或真实网盘传输。

### 发布与限制

- 本轮没有数据库迁移、新用户环境变量、远端提交或线上部署；保留工作区既有修改，未重新打包旧 zip。
- Web/Worker 必须同步发布新源码及新增模块；停止旧 Worker 后再启动新版，保留数据库、缓存及隐藏回执。不能让旧版整批上传器接手已进入逐文件流程的任务。
- 小中继盘仍需放得下一个完整文件及安全余量；历史残留和其它任务/程序占用不会被擅自清理。单文件流程不等于无本地存储的流式转发。

## 27. 整个入库链路复查与修复（2026-10-12）

### 检查范围与确认缺陷

检查公开提交/审批、冻结计划、来源物化和可见性、rclone/逐文件中继、目标校验、Organizer、ACK/清理，以及跨阶段取消、重试、进程恢复。

| 确认问题 | 修复及边界 |
| --- | --- |
| 共享资源幂等键被误当成请求所有权，另一请求取消时能撤销原任务；原创建者也可能在别人绑定后补偿取消共享任务 | 创建时持久化可信请求 owner；补偿取消与全局未绑定检查同一数据库事务；绑定事务拒绝已取消任务，覆盖双方先后顺序。不猜历史 owner，不因用户 JSON 开启补偿权限。 |
| 任务创建返回结构化失败或规划异常时，请求可一直停在 submitted | 终态业务错误同步请求 failed；暂时性创建/入队错误明确重试并在耗尽时落失败。 |
| 移动/天翼的显式选择清洗为空后退回整个分享 | 提交和审批均拒绝无效显式选择；未提供选择或默认空对象仍允许合法整分享，合法目录选择不受影响。 |
| Organizer 人工确认成功直接把 job 标成 done，而 Transfer 仍在 manual_review，未做最终验证、ACK 或清理 | 保留与非完成 Transfer 的关联；仅对 organizing 来源、匹配 task ID/计划 hash/完成证据的任务，事务内恢复并排队；处理完成回调与进入复核并发，仍经最终逐文件验证。 |
| 取消路径遇到已有提交 fence、回执缺失时，可能绕过未知提交保护进行清理 | 管理员入口与 Worker 收敛路径均拒绝破坏性清理；结果未知必须先核对外部操作。 |
| 多任务争用小中继盘时，后一个任务的正常缓存等待会变成磁盘不足失败 | 在已有 rclone 互斥锁内、实际下载前，等待其它任务 downloaded/verified 缓存释放；可取消且有等待时限，不消耗普通下载重试。 |

另外保留 unknown 传输的实际错误消息与错误码，人工复核页面不再只剩泛化的“传输结果未知”。

### 验证与复现

- 使用真实临时 SQLite、MemoryOpenList、原生上传替身和真实 shell/Python CLI，不使用生产数据库或真实网盘。
- Organizer 缺陷修复前复现到 `job=done` 但 `transfer=manual_review/final_verified=False/app_acked=False`；修复后人工确认继续最终校验，目标文件缺失时不 ACK、不清理；队列失败事务回滚可恢复。
- 共享任务验证两个线性化顺序：先绑定则补偿不取消；先取消则后续绑定拒绝。普通管理员显式取消不受补偿约束影响。
- 逐文件链路增加每个编排阶段都重建执行器/恢复快照的完整主流程；确认两文件交替下载上传、媒体逐个释放、最终验证后才 ACK 和整任务清理。
- 合并后的精简链路回归 **130 项全部通过，40.128 秒**：`test_public_ingress_guards`、`test_organizer_transfer_completion_resume`、`test_transfer_cancellation_submission_fence`、`test_transfer_preflight_resume`、`test_sequential_relay`、`test_sequential_relay_recovery_guards`、`test_relay_single_file_worker`、`test_transfer_execution_repository_guards`、`test_job_cancellation_v1`、`test_transfer_organizer_manifest`、`test_multilink_v1_integration`。
- 定向 Ruff、shell 语法、Python 3.11 语法解析、差异检查及 UTF-8 无 BOM 检查通过。未为本轮审计运行全仓长测试。

### 发布与未覆盖项

- 无新增 Schema 迁移、依赖或用户环境变量；Web/Worker 必须同步更新，保留现有数据与回执。历史缺所有权/外部回执的任务不自动推断或删除。
- 本轮未提交、推送、部署、重打包压缩包，也没有取消或清理任何真实任务。
- 真实网盘限流/认证、OpenList 可用性、NAS 磁盘与 SQLite I/O 仍需在现场确认；本地通过不能证明线上这些环境问题已经消失。

## 28. 传输中心、失效资源提示与移动云上传回执恢复（2026-10-14）

### 本轮交付与关键边界

- 新增独立“传输中心”，日志中心不再堆叠下载/上传卡片。默认隐藏已完成和取消任务，失败/人工复核仍保留；提供当前、需处理、最近结束及本地搜索。
- 只展示当前传输阶段的实时进度；整理/校验阶段收起旧的满进度条。100% 不代表入库完成，只有后端 `completed === true` 才归入成功历史。最近结束是 Worker 的有限近期观测，不代替正式任务历史。
- 传输页每 3 秒只读取状态，不拉取日志；隐藏页面/切到其他业务页停止轮询。刷新失败保留已有数据、显示重试，搜索、筛选和展开状态不因刷新丢失。环境检查使用独立区域，不覆盖传输列表。
- 新增 `provider_errors.py`：已知分享读取端点的 404/410、明确失效业务码统一为“分享资源已失效或被删除，请更换有效链接后重试。”，并保留非重试型 `SHARE_INVALID`。普通 API/账号 404、登录、提取码、权限、限流和网络故障不误归类。六盘仅明确资源失效才停止提交，不能凭离线 API 的裸 404 推断资源失效。
- HTTP 状态、原业务码、请求路径和脱敏诊断保留供排障；不把 Authorization、Cookie、token 或密码新增暴露到错误详情。
- 收尾检查补齐重试兼容性：夸克/移动云的临时 HTTP、连接与超时异常保持 `NETWORK_TRANSIENT`；未知业务码仅保留在诊断字段，维持原有 fallback，不能因新增原始 HTTP/业务码而被误判不可重试。
- 隔离复现了移动云已写入逐文件成功记录、但最后 `completed` 未落盘后永久停在未知状态的缺口。现在只在原操作键、目标指纹、完整原始文件范围与成功回执匹配，并只读核实云端文件 ID、名称、大小、目录归属后，原子补齐上传完成回执；后续仍走正常目标校验、整理、最终校验及 ACK，不重传、不直接标记任务成功。
- 移动云损坏/不匹配/不完整回执及观测/保存失败仍停在未知状态，保留文件和原回执。复核提示改为说明“成功结果尚未核实、暂停以避免重复上传”，不再直接展示 fence 术语。

### 针对性验证

- 前端回归 32 项通过：`test_rclone_live_progress_ui`、`test_transfer_center_ui`、`test_admin_asset_cache_versions`、`test_rclone_task_entry_ui`。
- 资源错误回归 98 项通过：`test_share_resource_errors`、`test_quark_importer_v1`、`test_quark_selection_error_policy`、`test_sixpan_api_contract`、`test_cloud139_importer_probe`、`test_tianyi_importer`。覆盖 JSON/HTML 失效响应、错误码透传、不误判、阻止失效资源继续提交、临时故障仍可重试和诊断脱敏。
- 移动云恢复回归 32 项通过：`test_mobile_upload_fence_recovery`、`test_relay_uploader_file_selection`、`test_sequential_relay_recovery_guards`。上传次数始终为 1；缺文件、大小/ID/目录不符、选择变化、认证故障、损坏回执、并发新回执、恢复落盘失败均不会重传或冒充成功。
- 合计 162 项针对性测试通过，没有运行全仓长测试。新测试仅使用内存响应/临时目录，不连接真实网盘或业务数据库。
- 本地隔离 Flask 模板预览验证菜单接线、当前/最近结束、搜索与桌面 1440×900、手机 390×844 布局；手机页面及面板均无横向溢出。预览使用模拟数据，不代表真实任务状态。截图：`.codex/transfer-center-preview-20261014.png`。
- 定向 Ruff、三份管理端 JS 的 `node --check`、差异检查和相关文件 UTF-8 无 BOM 检查通过。

### 发布与现网限制

- 未提交、推送、部署或重打包原有 zip；未改变真实 job-21、网盘文件、数据库、上传回执或暂存目录。保留所有工作区原有修改。
- 发布需同步更新 Web 与 Worker 的本轮源码/镜像，包含新增 `provider_errors.py` 与 `admin-transfers.css`；管理端资源版本已更新。无新增依赖、环境变量或数据库迁移。
- 本次修复不能证明真实 job-21 已恢复。仅底层上传 sidecar 完成而缺少原任务成功记录时仍需人工核实；旧非逐文件恢复路径未扩大修改。已有云端文件时不要选择“确认未执行”，不要删除回执、重建计划或重复提交任务。
- 回滚代码时保留现有数据库、回执与缓存；旧代码可能重新显示人工复核，但不能以清理现场来绕过核验。

## 29. 传输中心以当前影视为主、弱化结束记录（2026-10-14）

- 删除顶部四张统计卡和“最近结束”筛选页签，影视文件名称、当前阶段和实际进度直接位于主区域；排队仅保留小型文字提示，零队列不显示。
- 已结束任务只保留底部默认关闭的小入口。关闭时历史容器为空、不生成任何历史任务行；主动展开最多展示 5 行低对比记录，区域上限 280px。展开历史不会替换或移走当前任务，刷新不会擅自打开历史。
- 完成阶段的步骤条移入默认折叠的阶段记录，下载服务信息移到页尾折叠区；没有当前任务时使用紧凑中性空态，不展示大型完成/庆祝卡。
- 38 项前端回归及 4 项夸克失效/重试回归全部通过，共 42 项，测试运行约 3.37 秒。确认 JSON/HTML 分享 404/410 仍为失效文案，而临时 HTTP 与网络故障保留重试。
- 隔离浏览器用 2 个当前影视任务与 50 条结束记录验证：默认仅 2 张主卡、历史 0 行；主动展开 5 行且高度不超过 280px；关闭后历史 DOM 清空。1440×900 与 390×844 验证，手机页面与面板无横向溢出。
- 预览为模拟数据，不代表真实 job-21。截图：`.codex/transfer-center-media-focus-20261014.png`。JS 语法、定向 Ruff、差异及 UTF-8 无 BOM 检查通过。
- 本轮仅修改 Web 模板/传输脚本/样式及相应回归与说明，保留上一轮网盘错误和上传回执修复；`admin-rclone.js` / `admin-transfers.css` 缓存版本更新为 `20261014-media-focus-v010`。
- 未部署或操作真实任务；无依赖、环境变量或数据库迁移。本轮 UI 发布需同时更新模板与两份 v010 静态资源；回滚也应一并恢复，不删除历史数据。

### 补充：已结束记录收窄（2026-10-14）

- 历史展开区不再 flex 拉伸：最大宽度 440px、最大高度 200px，行间距缩小；当前任务保留全宽并增加清晰的蓝色边线，待处理任务保留琥珀色。
- 7 项模板/样式与缓存回归通过；浏览器实测 1440px 视口下当前卡 1161px、历史 440px，720px 下历史仍 440px，390px 下缩至 333px；历史和服务同时展开也无横向溢出。
- CSS 缓存版本为 `20261014-compact-history-v011`，JS 仍为 v010；没有修改传输状态判定或后端逻辑，未部署到 NAS。展开预览：`.codex/transfer-history-narrow-20261014.png`。

## 30. 长剧集跨盘传输不再因总时长误停（2026-10-15）

- 根因：`TransferOrchestrator.step()` 在查询原传输回执前执行固定 10800 秒上限，正常运行甚至已经完成的任务都可能直接进入人工复核。
- 有有效操作 ID 回执的跨盘任务现在先按原回执继续查询，不再以总运行时长判失败；目标校验、ACK、取消、未知结果及真实失败的保护不变。无回执等待、来源可见性、原生任务、取消确认和 Worker 中继缓存排队仍保留原有上限，未简单放大超时值。
- 后台恢复扫描严格识别旧版纯时长超时：检查原提示、阶段、错误码、冻结计划哈希/任务/策略、回执及提交栅栏一致性，拒绝取消、终态、有效租约和真实未知/失败回执。只恢复原任务到保留回执的等待阶段，不新建入库任务、不重新提交、不直接标成功；任务与执行状态在同一事务更新，入队失败可由下次扫描补偿。
- 旧超时证据保存在 `transfer_timeout_recovery`；后续 Worker 保存不会丢失该记录，其他人工审核不会自动放行。README 与 `.env.example` 已说明配置含义。
- 定向验证：188 项测试、187 个子用例通过（约 4.16 秒），覆盖长时轮询/重启、两个跨盘策略、完成校验门禁、缺失/冲突回执、取消、异常、旧任务幂等恢复、事务回滚和队列故障；定向 Ruff 通过。未跑全仓长测试，未连接真实网盘或生产数据库。
- 发布需同步更新 Web/Worker 源码或镜像；无新增依赖、环境变量、Schema 迁移。发布后由已有恢复扫描自动接续符合条件的旧任务，不要删除回执或重新提交。回滚保留数据库及回执，旧版代码可能再次触发总时长限制。
- 未提交、推送、部署、重打包，也未修改真实 job-26 的状态或文件；本地通过不等同于现网任务已经恢复。保留全部原有工作区修改。

## 31. 传输中心紧凑卡片与进度视觉优化（2026-10-15）

- 内容区限制为 1160px，当前任务改为响应式双列卡片，单卡不超过 620px；空态限制为 640px，不再横跨整屏。窄屏自动单列。
- 当前影视标题与正在处理的文件名分开显示，进度区域突出百分比，并用三列展示容量、速度和预计剩余。下载/上传/校验采用蓝色、青绿色、紫色渐变进度条；未知指标不填充假进度，校验容量明确标为“已校验”，不会冒充已上传。
- 已结束记录改为 640px 的精简列表面板，保留默认折叠和最多 5 条限制，增加结果图标与清晰的标题/任务编号/时间层级，移除旧 200px 内滚动。取消、真正完成与当前状态判定不变。
- 41 项定向 UI/缓存/实时进度测试通过（原组合 40 项约 4.18 秒，新增校验语义用例 1 项）；Node 语法、定向 Ruff、差异检查通过。未运行全仓长测试，未改后端传输逻辑。
- 隔离 Flask 预览仅使用模拟数据、不导入生产应用配置或数据库。浏览器连接工具返回认证令牌不可用，降级为独立无用户配置的本地 headless Chromium 渲染；禁止预览访问非本地服务。
- 视觉实测：1440px/1787px 桌面当前卡宽 562px、历史宽 640px、空态 640×90px；390px 手机当前卡与历史均为 333px，页面及卡片无横向溢出；四条完成记录无需内层滚动，浏览器无脚本错误。
- 模拟预览截图：`.codex/transfer-polish-desktop-20261015.png`、`.codex/transfer-polish-mobile-20261015.png`、`.codex/transfer-polish-mobile-history-20261015.png`、`.codex/transfer-polish-idle-20261015.png`。截图不代表真实任务状态。
- 发布同时更新 `templates/admin.html`、`static/admin-rclone.js`、`static/admin-transfers.css`，JS/CSS 缓存版本均为 `20261015-transfer-polish-v012`；无依赖、环境变量或数据迁移，回滚时同步恢复这三份文件即可。未部署、提交、推送或重打包，保留前轮全部改动。

## 32. 同任务有界预下载流水线（2026-10-15）

### 保存基线与实现

- 开发前按要求暂存并提交当前全部非忽略改动：`26d6acc`，175 个文件；未纳入 `.env`、数据库、网盘配置或运行缓存，未推送远程。
- 新增 `BoundedRelay`：保持一个上传和一个下载，上传 N 时预下载 N+1；额外窗口默认 1，可选 0/1/2。线程只在本次同步上传期间负责补位，退出前停止并等待线程收束，原任务、文件清单和上传幂等键不变。
- 新版 Worker 首文件下载前即启用 v2 账本；默认预算 20 GiB、实盘余量 1 GiB，未确认大小不开始下载。文件、预留、在途及已落盘部分均计入限制，跨任务未知缓存不被绕过或删除。缓存满即时交回队列，不再等 10800 秒，也不消耗失败重试额度。
- 本地 v2 下载用 `--inplace`，运行中每秒检查实盘余量；暂停保留原文件/预留，恢复按剩余字节计算。提高 `RELAY_CACHE_MAX_BYTES` 并重新创建容器可使超出原预算的原任务继续；不自动扩大已冻结的文件窗口或降低余量。
- rclone 队列按 run ID/操作键去重和取消，不再误吞同 job 的下一文件，也不会用取消一个文件的回执去停止另一个运行。
- 清单增加跨进程短锁、原子读改写及修订号，下载/上传分别修改自己的条目。Windows 读操作同样参与短锁，避免读取句柄与原子替换冲突；嵌套读取可重入。
- 取消在首文件提交前、日志桥接后、外部下载/上传边界重新检查；取消标记保持单调。确认本次尚未调用外部 API 时撤回本次准备标记，不制造永久“结果未知”；旧的不确定上传证据绝不清除。取消枚举全部预下载回执，不只处理当前上传文件。
- UI 和日志按文件/通道身份分离；同卡显示下载下一集与上传当前集、各自速度/反馈及缓存就绪数。迟到旧上传日志不能覆盖新文件；100%/下载批次结束仍不等于入库完成。

### 针对性验证

- 最终主链路/状态/真实脚本替身/配置组：51 项通过，15 个子用例（33.40 秒）；覆盖默认1、窗口2、关闭预取、首集预算、空间不足串行退化、上传期间确实下载下一集、重启回执、未知上传保留、取消全部预取和读写锁。
- 最终兼容/进度/调度组：206 项通过，83 个子用例（12.61 秒）；包含旧串行、原生转存、长时传输、并发取消、scope队列、双通道日志/投影/UI及静态资源版本。
- 另新增 1 项读写锁与嵌套读取回归通过。定向 Ruff、Node 语法、shell 语法及差异检查通过；没有运行全仓长测试。
- 中途发现并修正旧测试的“不得下载下一集”断言、配置列表/脚本版本契约，以及 Windows 并发读取时偶发的回执替换失败；最终上述检查全部通过。
- 独立模拟服务/浏览器渲染：1440px 桌面与390px手机，同一个任务显示两条独立进度、相邻不同文件名；卡片与页面无横向溢出、浏览器无脚本错误。截图 `.codex/relay-prefetch-1440-20261015.png`、`.codex/relay-prefetch-390-20261015.png`。预览不是实际任务状态，服务已在验证后停止。

### 发布与限制

- 同步更新 Web、Worker、脚本和编排，前端缓存版本 `20261015-relay-prefetch-v013`；三项可选环境参数见 README，不需要新增依赖或数据库 Schema 迁移。
- 源码部署命令：`docker compose -f docker-compose.yml -f compose.dev.yaml up -d --build fnos-media-import fnos-rclone-worker`。先停止新调度、等待当前上传结束再更新，避免中途终止未知上传和新旧数据面混跑。此命令本轮未执行。
- v2 缓存清单不应交给只支持 v1 的旧代码。保留数据库、媒体缓存和全部隐藏回执；降级优先在当前版本将新任务的预下载设为0，不通过删除回执或重建任务回滚。
- 未访问真实网盘、未运行线上任务或生产数据库、未部署、未推送、未测速；实际提速取决于上下行带宽、NAS I/O 和网盘限制。空间保护不能替代操作系统配额或防止外部程序瞬间占满磁盘。

## 33. v1.0.3 发布 CI 失败修复（2026-11-03）

- 已读取 GitHub run `41613885432` 的实际日志：`Run Python tests and lint` 步骤中 10 项测试失败、2028 项通过。真实错误是 `RelayFileState.load()` 对不存在的回执仍创建共享锁和 `/data/relay`；Linux Runner 无权创建 `/data`，使只读提交/恢复路径提前失败。不是 Node 弃用或 Ubuntu 迁移通知导致失败。
- 修正为：先校验回执路径；回执不存在时只返回 `None`，不创建目录或锁文件；已存在回执继续使用短锁读取，保留 Windows 并发替换保护。未增加 sudo/chmod、未跳过原失败测试。
- 新增“父目录不可写仍可只读检查不存在回执”和“不存在回执不创建锁”回归，用 PermissionError 注入复现 Linux 条件，不依赖本机已有 `/data`。
- 两个 job 固定为 `ubuntu-24.04`；checkout/setup-python、Docker QEMU/Buildx/login/metadata/build-push 全部改为已发布的 Node.js 24 版本，逐个核实官方 `action.yml` 的运行时及本仓库使用的输入参数。
- 定向验证：101 项通过、33 个子用例通过、1 项 Linux 原生 flock 测试在 Windows 跳过，耗时18.70秒；全仓 Ruff、差异检查通过。当前本机没有可用 WSL Linux，因此由发布 Action 验证真实 Linux 全量套件。
- 用户已确认修复后推送 master 并新建 `v1.0.4` 重新发布；保留原 `v1.0.3`，不移动或覆盖旧 tag。此次不修改 NAS、网盘、业务数据库或运行任务。

## 34. 通知事件跨秒幂等与发布前校验（2026-11-03）

- v1.0.4 的 Linux 全量 CI 已通过原来全部 10 项失败测试，Node/Ubuntu 警告也已消除；随后暴露通知去重时间边界：2040 项通过、1 项失败。
- 固定两次事件时间分别为相邻秒，稳定复现失败。原因是通知每次生成新的 `occurred_at`，而通用队列允许刷新 pending 命令，导致同一事件误报新提交并重置原退避。
- 通用队列增加显式 `replace_pending=False` 选项，通知发射器在普通连接和业务同事务路径均启用：保留第一次事件载荷、时间、任务 ID 和重试状态。其他命令仍保留原 pending 更新行为；跨任务类型的幂等键碰撞拒绝覆盖。
- 增补跨秒、事件内容变化、重试退避、同事务及跨类型冲突检查；83 项针对性测试、5 个子用例及全仓 Ruff 通过（40.65 秒），未靠重复重跑或跳过测试规避问题。
- 发布流程新增 master push 只校验的入口，publish job 仅 tag/manual 事件可执行；先确认真实 Linux 全量测试通过，再打新 tag。用户已授权后续发布 v1.0.5，v1.0.3/v1.0.4 保留不覆盖。
