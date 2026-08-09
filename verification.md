# V1.0 多链路改造验证记录

更新时间：2026-08-09  
工作区：`C:\WorkSpace\fnos-search`  
分支：`faat_0806_入库流程改造`  
HEAD：`c454fa3`

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
- 跨盘到移动 relay；
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
