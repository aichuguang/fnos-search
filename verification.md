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

### 11.3 最终测试结果

使用测试进程内的 Windows tempfile ACL 继承方案执行：

```text
Ran 984 tests in 172.429s

OK
```

安装 pytest 后全量执行：

```text
993 passed, 178 subtests passed in 182.36s
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
