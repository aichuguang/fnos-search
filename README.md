# 飞牛影视统一入库维护系统

这是一个面向家庭 NAS 的影视资源搜索、审核和自动入库工具。

访客可以搜索并提交想看的影视，管理员负责审核和查看任务进度。系统可以把资源转存到网盘，
再通过 OpenList 整理目录、刷新 STRM，最后交给飞牛影视等媒体库识别。

项目为自用项目，开源给同需求用户

项目主要是给网盘使用，适合移动云盘空间大的用户（我大概有上百T)，目前流程是，移动云盘资源直接进入openlist,6盘离线到6盘空间，夸克资源多，空间小，API容易风控，不能302等一系列问题，所以目前只是找资源然后高速下载转存到移动云盘，简单来说，目前项目是主要为移动云盘服务，但是，可以支持下载资源到夸克和本地（此功能还需再次优化）。

## 1. 项目介绍

### 能做什么

- 提供访客搜索和提交页面。
- 提供管理员审核、任务重试、日志和运行状态页面。
- 支持 PanSou 聚合搜索（可选内置，一行开关启用），也可以启用 BTBTLA 作为补充磁链来源。
- 支持夸克、139 移动云和 6盘等入库线路。
- 支持访客按文件或文件夹选择需要入库的内容。
- 支持 OpenList 目录整理，可按 TMDB 信息生成标准影视名称。
- 识别不准确时可以人工修改，也可以跳过改名，直接放入正确的分类目录。
- 支持电影、电视剧、动漫、综艺和其他分类。
- 支持邮件、Webhook 和每日摘要通知。
- 支持定时追更和每日热榜候选。

### 使用流程

```text
访客搜索影视
  → 选择资源并提交
  → 管理员审核
  → 网盘转存或离线下载
  → 移入任务暂存目录
  → OpenList 整理或人工跳过整理
  → 刷新 STRM
  → 媒体库更新
```

PanSou 聚合搜索可作为可选内置服务随编排启动（默认关闭，在 `.env` 加 `COMPOSE_PROFILES=pansou`
启用）；也可以在后台填写自建的外部 PanSou 地址。项目不会自动提供
OpenList、第三方网盘账号或 TMDB Token。它负责把这些已有服务连接起来，并统一管理整个入库过程。

### 页面入口

| 页面 | 地址 |
|---|---|
| 访客页面 | `http://服务器IP:5251/` |
| 管理后台 | `http://服务器IP:5251/admin/login` |
| 健康检查 | `http://服务器IP:5251/health` |
| API 文档 | `http://服务器IP:5251/swagger` |

## 2. 准备工作

### Docker 环境

建议使用：

- Docker Engine 20.10 或更高版本。
- Docker Compose v2。
- Linux 容器模式。
- `amd64` 或 `arm64` 设备。

### 外部服务

根据实际使用的功能准备，不需要全部安装：

| 服务 | 是否必需 | 用途 |
|---|---|---|
| PanSou | 可选 | 聚合搜索源。可在 `.env` 加 `COMPOSE_PROFILES=pansou` 启用内置容器（fish2018/pansou），也可以在后台改填自建外部 PanSou 地址 |
| 内置夸克 Provider | 使用夸克线路时需要，在后台使用夸克 APP 扫码登录（自动提取并加密保存 Cookie） | 通过夸克官方网页 API 检查分享并原生转存 |
| 内置天翼 Provider | 使用天翼线路时需要，在后台使用账号密码登录；风控时输入图片验证码 | 自动维护 Token，通过天翼网页 API 获取分享目录、原生转存并轮询校验 |
| OpenList | 使用目录整理或 STRM 时需要 | 统一访问网盘、整理真实目录、生成 STRM |
| 139 移动云认证 | 使用移动云线路时需要 | 官方直转或上传文件 |
| 6盘账号 | 使用磁链离线下载时需要 | 解析磁链并创建离线任务 |
| TMDB Token | 建议配置 | 帮助识别影视名称、年份和季集 |
| AI 接口 | 可选 | 仅用于低置信识别建议 |
| 飞牛媒体库接口 | 可选 | 整理完成后触发媒体库刷新 |

天翼账号默认通过后台的“账号密码登录”接入。系统按天翼登录协议对账号密码做 RSA 加密；若天翼触发风控，会在登录弹窗中显示图片验证码并等待输入。登录成功后 `accessToken`、`refreshToken` 和过期时间会加密保存，运行时优先使用 Access Token，失效后自动刷新并写入新的账号修订。Refresh Token 不可用时再回退到已保存的 SSON 或账号密码；后台无人值守重登若再次触发验证码，会要求管理员重新登录。SSON 仍可作为恢复凭证，支持只填 Cookie 值或填写 `SSON=...`。私密分享访问码随资源提交，不写入账号凭证。

夸克运行时只使用后台“网盘入库 → 云盘账号”中激活的 Provider 账号。系统不再读取全局
`quark` Cookie、高级配置 Token 或环境变量回退；账号无法解密、登录态过期或凭证为空时会明确
提示当前账号不可用，需要在账号工作区重新登录或切换账号。

### 磁盘空间

数据库、后台配置和自动生成的进程认证令牌默认保存在 `data`，日志默认保存在 `logs`。
浏览器会话签名密钥由 `FNOS_ROOT_KEY` 按 `app-session` 用途派生，不单独配置或持久化。
`rclone/temp` 可能临时保存完整视频文件，如果系统盘空间有限，应在 `.env` 中把
`RCLONE_TEMP_HOST_PATH` 改到容量较大的存储盘。

## 3. 如何部署

### 方法一：命令行部署

下载项目并启动：

```bash
git clone https://github.com/aichuguang/fnos-search.git
cd fnos-search
cp .env.example .env
openssl rand -hex 32
# 把上一行生成的 64 位 hex 填入 .env 的 FNOS_ROOT_KEY
docker compose up -d
```

第一次启动会自动完成以下工作：

- 拉取 `aichuguang/fnos-search:latest`。
- 启动 Web 容器和独立的 rclone Worker 容器；启用了 `COMPOSE_PROFILES=pansou` 时还会启动内置 PanSou 聚合搜索容器。
- 创建数据库和全部表结构。
- 创建默认持久化目录。
- 校验你提供的 `FNOS_ROOT_KEY`，派生应用会话签名密钥，并生成 Worker 控制令牌。

查看运行状态：

```bash
docker compose ps
docker compose logs -f fnos-media-import
```

### 方法二：NAS Docker 面板部署

1. 打开 NAS 的 Docker 管理页面。
2. 新建 Compose 或“项目”。
3. 上传项目目录，或把 `docker-compose.yml` 和需要的宿主机目录放在同一个项目目录。
4. 根据 `.env.example` 创建 `.env`（或在面板中添加环境变量），至少填写一条 64 位 hex 的
   `FNOS_ROOT_KEY`；需要自定义存储位置时，建议同时填写绝对路径。
5. 启动项目。
6. 浏览器打开 `http://NAS-IP:5251/admin/login`。

默认登录信息：

```text
用户名：admin
密码：admin
```

登录后页面会持续提示修改默认密码。

### 配置 rclone

只有启用跨 Provider 来源落地、受控本地下载或通用 OpenList 目标时，才需要完成这一步。
移动云和天翼作为目标时，rclone 只把来源下载到本地 relay，最终写入分别由移动云 API 和
189CloudPC API 完成，不通过 rclone/WebDAV 上传文件字节。

登录管理后台，进入“系统设置 → 飞牛与 rclone”，填写以下内容：

- Remote 名称：默认 `MP`
- OpenList WebDAV URL：例如 `http://host.docker.internal:5244/dav`
- OpenList 用户名和密码

点击“保存并检测”。连接成功后即可使用，不需要进入容器，也不需要手工处理密码。

只有页面无法使用时，才需要手工编辑 `rclone/config/rclone.conf`：

```ini
[MP]
type = webdav
url = http://你的OpenList地址:5244/dav
vendor = other
user = OpenList用户名
pass = rclone处理后的密码
```

`MP` 是默认 remote 名称，不要随意修改。手工配置时，密码可以这样处理：

```bash
docker exec fnos-rclone-worker rclone obscure '你的OpenList密码'
```

把输出内容填到 `pass`。然后检查是否连接成功：

```bash
docker exec fnos-rclone-worker rclone listremotes --config /config/rclone/rclone.conf
docker exec fnos-rclone-worker rclone lsd "MP:" --config /config/rclone/rclone.conf
```

更详细的说明见 [rclone 配置](rclone/README.md)。

### 配置管理后台

进入“系统设置”后，建议按以下顺序配置：

1. 在“个人设置”修改管理员密码。
2. 搜索：默认不内置。想用内置 PanSou 时在 `.env` 加 `COMPOSE_PROFILES=pansou` 后重新编排；
   使用自建外部 PanSou 时，在“搜索与线路”填写地址。
3. 在“网盘入库 → 云盘账号”登录夸克、天翼、移动云或 6盘账号。
4. 在“网盘入库 → OpenList”创建连接并检测挂载。
5. 在“网盘入库 → 保存位置 / 入库规则”选择目标并生成分类目录。
6. 在“OpenList/TMDB/AI”中仅调整通用运行参数、TMDB 和 AI；OpenList 身份凭证不在高级配置中保存。
7. 需要自动整理时再开启 Organizer，需要通知时配置 SMTP 或 Webhook。

高级配置的保存与导入统一使用 `fnos-media-import/advanced-config`、`version: 1` 的版本化文档。
后台导出的 JSON 可以重新导入；裸 `config` 对象、旧版全局网盘凭证和旧分类路径字段会被直接拒绝，
不会自动迁移或静默回退。

配置完成后，建议先提交一个小资源测试完整流程，再开始批量使用。

### 编排目录设置

`docker-compose.yml` 不需要手工修改。默认会在编排文件所在目录创建 `config`、`data`、`logs`
和 `rclone` 子目录。

飞牛、群晖等 Docker 面板可以直接在 Compose 项目或编排的“环境变量”区域添加下表变量，
不需要创建 `.env` 文件。命令行部署需要自定义时，才复制环境变量模板：

```bash
cp .env.example .env
```

#### 正式部署变量

以下是 `docker-compose.yml` 支持的全部用户配置变量。新部署只需生成一把根密钥。

| 变量 | 默认值 | 是否建议配置 | 说明 |
| --- | --- | --- | --- |
| `APP_VERSION` | `latest` | 正式环境建议 | 应用镜像标签。固定为具体版本可避免重新编排时意外升级。 |
| `APP_PORT` | `5251` | 按需 | Web 页面映射到宿主机的端口。只填写端口数字，例如 `5251`。 |
| `TZ` | `Asia/Shanghai` | 按需 | 应用和 rclone 容器使用的时区。 |
| `APP_CONFIG_HOST_PATH` | `./config` | 按需 | 可选 `config.yaml` 所在的宿主机目录。 |
| `APP_DATA_HOST_PATH` | `./data` | 强烈建议确认 | SQLite 数据库、后台设置、数据库备份和 Worker 控制令牌的持久化目录。必须是当前 NAS 的本地文件系统，重新编排必须继续使用原目录。 |
| `APP_LOGS_HOST_PATH` | `./logs` | 按需 | 应用运行日志目录，分别保存 `web.log` 和 `worker.log`。 |
| `RCLONE_CONFIG_HOST_PATH` | `./rclone/config` | 强烈建议确认 | `rclone.conf` 持久化目录，保存 OpenList WebDAV Remote 配置。 |
| `RCLONE_CACHE_HOST_PATH` | `./rclone/cache` | 按需 | rclone 缓存目录，可清理，但运行中不要删除。 |
| `RCLONE_TEMP_HOST_PATH` | `./rclone/temp` | 建议配置 | 搬运完整媒体文件的临时目录，应放在空间充足的磁盘。 |
| `FNOS_RELAY_HOST_PATH` | `./rclone/relay` | 建议配置 | 跨 Provider 写入移动云或天翼时的共享中继目录，Web 与 Worker 都挂载到 `/data/relay`。rclone 只负责下载，随后由目标 Provider 原生 API uploader 上传；应放在空间充足的磁盘。 |
| `RCLONE_LOCAL_STORAGE_HOST_PATH` | `./rclone/local-storage` | 按需 | 本地存储目标的宿主机挂载目录，挂载到 Worker 的 `/storage-targets`。仅当使用本地文件夹目标时配置，必须指向预先挂载的独立目录，不能复用 `APP_DATA_HOST_PATH`。 |
| `FNOS_ROOT_KEY` | 无 | 必填 | 唯一的 32 字节根密钥，填写 64 位 hex。程序通过 HKDF-SHA256 为 Flask 会话、账号、OpenList、授权会话、来源快照和通知派生隔离子密钥。可用 `openssl rand -hex 32` 生成。 |
| `RCLONE_WORKER_CONTROL_TOKEN` | 空，自动生成 | 新部署不要填写 | Web 与独立 rclone Worker 之间的内部 Bearer Token。留空时生成并保存到 `APP_DATA_HOST_PATH/.secrets/rclone_worker_control_token`，重新编排会继续使用原值。 |
| `COMPOSE_PROFILES` | 空 | 按需 | 启用可选服务。填写 `pansou` 时编排会额外启动内置 PanSou 聚合搜索容器；留空则只有 Web 和 Worker 两个容器。 |
| `PANSOU_USERNAME` | `fnos-search` | 按需 | 内置 PanSou 聚合搜索的登录账号。该服务仅容器内网可达，不对宿主机开放端口。 |
| `PANSOU_PASSWORD` | `fnos-search` | 按需 | 内置 PanSou 聚合搜索的登录密码。修改后需要重新编排才能生效。 |
| `PANSOU_IMAGE` | `ghcr.io/fish2018/pansou:latest` | 正式环境建议 | 内置 PanSou 镜像。固定为具体版本可避免重新编排时意外升级。 |
| `PANSOU_CACHE_HOST_PATH` | `./pansou/cache` | 按需 | 内置 PanSou 搜索缓存目录，可清理。 |

NAS 面板示例：

```env
APP_VERSION=latest
APP_PORT=5251
TZ=Asia/Shanghai

APP_CONFIG_HOST_PATH=/你的目录/fnos-search/config
APP_DATA_HOST_PATH=/你的目录/fnos-search/data
APP_LOGS_HOST_PATH=/你的目录/fnos-search/logs
RCLONE_CONFIG_HOST_PATH=/你的目录/fnos-search/rclone/config
RCLONE_CACHE_HOST_PATH=/你的目录/fnos-search/rclone/cache
RCLONE_TEMP_HOST_PATH=/你的大容量目录/fnos-search/temp
FNOS_RELAY_HOST_PATH=/你的大容量目录/fnos-search/relay
FNOS_ROOT_KEY=请填写64位hex根密钥
```

面板中只添加需要覆盖的变量即可，不必逐项填写。相对路径以 Compose 项目目录为基准；NAS
面板的项目目录不直观时，建议六个目录变量全部填写绝对路径。最重要的是
`APP_DATA_HOST_PATH`，重新编排时不能把它改到新的空目录。

管理员初始账号固定为 `admin/admin`，首次登录后直接在管理后台修改。PanSou 内置服务默认关闭
（`COMPOSE_PROFILES=pansou` 启用），OpenList、网盘、TMDB、通知和分类目录全部在管理后台配置，
不放进 `.env`。

内置 PanSou 说明：

- 内置服务默认不启动。在 `.env` 加一行 `COMPOSE_PROFILES=pansou`（或每次启动命令加
  `--profile pansou`）后重新编排，即随编排启动，应用自动通过容器内网地址 `http://pansou:8888`
  连接，开箱即用。
- 不启用内置服务时，在后台“搜索与线路”填写自建外部 PanSou 地址即可；两种方式不要同时指向
  不同地址造成混淆。
- 后台保存过外部 PanSou 地址的部署，数据库配置优先于编排默认值；想切回内置时，先启用
  `COMPOSE_PROFILES=pansou`，再在“搜索与线路”把地址改回 `http://pansou:8888`
  （不要留空，留空等于关闭搜索）。
- 内置 PanSou 的 TG 频道搜索在国内直连网络下可能超时，插件搜索源不受影响；需要 TG 来源时，
  在 `docker-compose.yml` 的 pansou 服务中取消 `PROXY` 注释并指向可用代理。
- 内置镜像托管在 `ghcr.io`，个别国内网络拉取较慢或失败。此时把 `.env` 中的 `PANSOU_IMAGE`
  改为可用的镜像代理地址（保持 `pansou:标签` 结尾），例如：
  `PANSOU_IMAGE=ghcr.nju.edu.cn/fish2018/pansou:latest`。


### 更新版本

完整维护窗口、冒烟与回滚清单见 [V1 v29 硬切换发布与回滚手册](docs/v1-hard-cutover-deploy.md)。

拉取新镜像不会覆盖宿主机上的 `app.db`。但从旧版单容器/辅助 rclone 容器架构升级时，
**必须同时替换 `docker-compose.yml`，不能只拉取新镜像**。该版本包含数据库 v29 破坏性硬切换，
必须使用维护窗口，禁止新旧容器并存。完整顺序如下：

1. 备份当前 `APP_DATA_HOST_PATH`、`FNOS_ROOT_KEY`、配置目录和 rclone 配置。
2. 停止 Web、Worker、Scheduler 及旧版辅助容器，确认没有进程继续连接 `app.db`。
3. 下载新的 `docker-compose.yml`，确认仍使用原 `APP_DATA_HOST_PATH`，然后拉取新镜像。
4. 在不启动服务的情况下运行硬切换预演，审核保留/删除数量、关联清理数量和 ID 集合哈希：

```bash
docker compose down
docker compose pull
docker compose run --rm --no-deps fnos-media-import \
  python -m fnos_media_import.v1_cutover \
  --database /app/data/app.db --dry-run
```

5. 确认预演结果后执行 `--apply`。命令会获取独占锁、生成迁移前数据库备份、升级到 v29 并执行外键检查：

```bash
docker compose run --rm --no-deps fnos-media-import \
  python -m fnos_media_import.v1_cutover \
  --database /app/data/app.db --apply
```

6. 只有迁移成功后才启动全部新版本进程并执行健康检查：

```bash
docker compose up -d --remove-orphans
docker compose ps
curl -fsS "http://127.0.0.1:${APP_PORT:-5251}/readyz"
```

7. 使用非生产目录验证任务创建、四 Provider 只读探测、移动云/天翼原生 API relay、OpenList、Organizer、取消与恢复后，再解除维护模式。

`--remove-orphans` 只有在新编排已经生效后，才能识别并删除旧版辅助容器。若仍使用旧编排，
新镜像不会再兼容旧角色变量，也不会以 Web-only 模式降级；旧编排缺少显式
`FNOS_PROCESS_ROLE` 时容器会直接启动失败，因此必须先替换编排再更新镜像。

迁移框架仍保留数据库升级能力，但现有部署不得依赖应用启动时自动完成 v29；发布流程必须先离线运行上述
硬切换 CLI。CLI 会在 `APP_DATA_HOST_PATH/backups` 中创建迁移前备份并在 JSON 摘要中返回路径。

`FNOS_ROOT_KEY` 不写入数据库，必须与数据备份分开保存；修改或丢失后，新版敏感数据将无法解密，
现有浏览器登录会话也会全部失效。应用签名密钥由根密钥按 `app-session` 用途稳定派生，不写入
`app.db`；Worker 控制令牌仍保存到 `APP_DATA_HOST_PATH/.secrets`。

硬切换升级前，把现有账号主密钥的值原样改名为 `FNOS_ROOT_KEY`，不要同时保留两个变量名，
也不能临时生成另一条值。程序只接受新版 `v2:<purpose>` 密文；旧 `v1`、`ol:v1` 和旧通知
密文不会自动迁移或回退解密，相关账号、OpenList 连接或通知凭据必须在后台重新登录/保存。
根密钥轮换需要先执行密文迁移，不能直接替换环境变量。

如果需要从源码构建：

```bash
docker compose -f docker-compose.yml -f compose.dev.yaml up -d --build
```

#### 源码构建变量

以下变量只用于 `compose.dev.yaml` 自行构建镜像，使用正式镜像的普通部署无需设置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_IMAGE` | `fnos-search-local` | 本地构建后的镜像名称。 |
| `APP_VERSION` | `dev` | 本地镜像标签，同时写入镜像版本元数据。 |
| `PYTHON_IMAGE` | `python:3.11-slim` | 构建应用镜像使用的 Python 基础镜像。 |
| `RCLONE_IMAGE` | `rclone/rclone:1.70.3` | 构建时提取 rclone 可执行文件使用的官方基础镜像。 |
| `APP_UID` | `10001` | 镜像内应用用户 UID。当前 rclone 共享目录也使用 `10001`，不要单独修改。 |
| `APP_GID` | `10001` | 镜像内应用用户 GID。当前 rclone 共享目录也使用 `10001`，不要单独修改。 |
| `VCS_REF` | `unknown` | 写入镜像 OCI 标签的源码提交标识。 |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | 首选 Python 包索引。 |
| `PIP_EXTRA_INDEX_URLS` | 阿里云、腾讯云、清华镜像 | 首选索引失败后依次尝试的备用索引，多个地址用空格分隔。 |

## 4. 注意事项

### 及时修改默认密码

默认账号是 `admin/admin`，只用于第一次登录。修改前不要把管理后台开放到公网。

### 保留持久化目录

默认数据都保存在项目目录中：

```text
config/
data/
logs/
rclone/config/
rclone/temp/
rclone/cache/
```

其中最重要的是：

- `data/`：数据库、任务、后台设置和自动生成密钥。
- `rclone/config/`：rclone 的 OpenList WebDAV 配置。
- `rclone/temp/`：搬运过程中的临时媒体文件。

更新或重建容器不会删除这些目录。迁移机器、重装系统或删除 Compose 项目前必须先备份。

`APP_DATA_HOST_PATH` 中的 SQLite 数据库由 Web 和 Worker 共同访问，必须放在同一台 NAS 的
ext4、Btrfs、XFS、ZFS 等本地文件系统。不要放在 SMB、NFS、CIFS 或其他网络共享中；
SQLite 的 WAL 和文件锁在网络文件系统上无法提供可靠的一致性保证。

### 独立 rclone Worker

标准编排由两个同镜像容器组成：`fnos-media-import` 只提供 Web 和任务生产入口，
`fnos-rclone-worker` 负责调度、后台任务和直接执行 rclone。Worker 不映射宿主机端口，
Web 通过 Compose 内部网络和自动生成的 Bearer Token 调用它。

两个容器都不挂载 `/var/run/docker.sock`，镜像中也不包含 Docker CLI。rclone 仅能访问分配给
Worker 的配置、缓存、临时目录以及应用共享数据目录，不能管理宿主机上的其他容器。

### 日志与健康检查

- `APP_LOGS_HOST_PATH/web.log`：Web 请求、设置保存和 Worker 调用日志。
- `APP_LOGS_HOST_PATH/worker.log`：调度、后台队列、Organizer 和 rclone 执行日志。
- `/health`、`/livez`：只表示当前 HTTP 进程存活。
- `/readyz`：同时检查数据库、部署安全状态和 Worker 核心运行时；返回 503 时优先查看上述两个日志。

Worker 核心线程持续异常时会主动退出当前应用进程，由 Gunicorn 重建；若整个容器退出，则由
Docker 重启策略恢复。短暂重启期间，Web 查询会自动进行有限重试；启动、停止、取消等有副作用
命令不会自动重试，以免重复执行。

### OpenList 挂载与保存位置必须对应

夸克、移动云等 Provider 的原生目录、OpenList 挂载和最终媒体目录可能位于不同命名空间。
请在“网盘入库”的 OpenList、保存位置和入库规则中完成绑定并执行完整预检；不要再通过
`quark_save_path`、`sixpan_save_path`、`cloud139_target_path` 等旧分类字段手工拼接路径。

不确定时先关闭 Organizer，确认资源能够正常转存后，再单独测试整理功能。

### 合集或识别不准时不要强行整理

遇到电影合集、错误标题或无法确定的文件，建议进入 Organizer 人工审核并选择“跳过整理”。
系统会保留原文件名，只把内容移动到正确分类目录，后续交给 OpenList 或媒体库自行刮削。

### 一把根密钥，按用途派生

应用只接受 `FNOS_ROOT_KEY`。程序通过 HKDF-SHA256 派生 Flask 会话、账号、OpenList、授权会话、
来源快照和通知等用途隔离子密钥；不再配置或持久化第二把应用签名密钥。Worker 控制令牌只用于
进程间认证，不是数据加密主密钥。迁移或重装时必须同时保留 `data/` 和原 `FNOS_ROOT_KEY`；
任一丢失都会导致已加密凭据无法解密，替换根密钥还会使现有浏览器会话失效。

### 固定版本更适合长期使用

`latest` 适合首次体验。长期运行时建议把 `APP_VERSION` 固定为明确版本，例如：

```env
APP_VERSION=1.2.0
```

回滚旧镜像时，必须先停止全部新版本进程，再恢复 v29 前的 `data` 备份并启动旧镜像；不能只修改镜像标签，
旧镜像也不能直接读取 v29 数据库。

### 当前兼容范围

- 支持 Linux 容器、Docker Compose v2、`amd64` 和 `arm64`。
- Windows 和 macOS 需要使用 Docker Desktop 的 Linux Container 模式。
- Podman、Kubernetes、Docker Swarm 和 Windows Container 暂不作为支持目标。
- SELinux 严格环境可能需要额外设置挂载目录标签。

### 开源协议

本项目使用 [MIT License](LICENSE)。

遇到问题时，先查看：

```bash
docker compose ps
docker compose logs --tail=200 fnos-media-import
docker compose logs --tail=200 fnos-rclone-worker
```

部署目录和更新规则见上面的“编排目录设置”和“更新版本”。
