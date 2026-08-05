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
- 支持 PanSou 搜索，也可以启用 BTBTLA 作为补充磁链来源。
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

项目不会自动提供 PanSou、OpenList 或第三方网盘账号。它负责把这些已有服务连接起来，
并统一管理整个入库过程。

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
| PanSou | 使用搜索功能时需要 | 搜索夸克、移动云、磁链等资源 |
| Quark Auto Save | 使用夸克线路时需要 | 把夸克分享保存到自己的网盘 |
| OpenList | 使用目录整理或 STRM 时需要 | 统一访问网盘、整理真实目录、生成 STRM |
| 139 移动云认证 | 使用移动云线路时需要 | 官方直转或上传文件 |
| 6盘账号 | 使用磁链离线下载时需要 | 解析磁链并创建离线任务 |
| TMDB Token | 建议配置 | 帮助识别影视名称、年份和季集 |
| AI 接口 | 可选 | 仅用于低置信识别建议 |
| 飞牛媒体库接口 | 可选 | 整理完成后触发媒体库刷新 |

### 磁盘空间

数据库和配置占用空间很小，但 `rclone/temp` 可能临时保存完整视频文件。
如果系统盘空间有限，部署后可在 `.env` 中把 `RCLONE_TEMP_HOST_PATH` 改到容量较大的存储盘。

## 3. 如何部署

### 方法一：命令行部署

下载项目并启动：

```bash
git clone https://github.com/aichuguang/fnos-search.git
cd fnos-search
docker compose up -d
```

第一次启动会自动完成以下工作：

- 拉取 `aichuguang/fnos-search:latest`。
- 拉取官方 `rclone` 镜像。
- 创建数据库和全部表结构。
- 创建默认持久化目录。
- 生成应用签名密钥和通知加密密钥。
- 创建空的 `rclone/config/rclone.conf`。

查看运行状态：

```bash
docker compose ps
docker compose logs -f fnos-media-import
```

### 方法二：NAS Docker 面板部署

1. 打开 NAS 的 Docker 管理页面。
2. 新建 Compose 或“项目”。
3. 粘贴项目根目录的 `docker-compose.yml`。
4. 启动项目。
5. 浏览器打开 `http://NAS-IP:5251/admin/login`。

默认登录信息：

```text
用户名：admin
密码：admin
```

登录后页面会持续提示修改默认密码。

### 配置 rclone

只有使用夸克到移动云等 rclone 搬运线路时，才需要完成这一步。

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
docker exec rclone-server rclone obscure '你的OpenList密码'
```

把输出内容填到 `pass`。然后检查是否连接成功：

```bash
docker exec rclone-server rclone listremotes
docker exec rclone-server rclone lsd "MP:"
```

更详细的说明见 [rclone 配置](rclone/README.md)。

### 配置管理后台

进入“系统设置”后，建议按以下顺序配置：

1. 在“个人设置”修改管理员密码。
2. 在“搜索与线路”填写 PanSou 地址。
3. 在“网盘服务”配置实际使用的夸克、移动云或 6盘。
4. 在“OpenList/TMDB/AI”配置 OpenList 和 TMDB。
5. 在“分类路径”确认电影、电视剧、动漫和综艺目录。
6. 需要自动整理时再开启 Organizer。
7. 需要通知时配置 SMTP 或 Webhook。

配置完成后，建议先提交一个小资源测试完整流程，再开始批量使用。

### 可选修改

默认配置已经可以启动，不需要填写环境变量。只有需要修改版本、端口、时区或大文件暂存目录时，
才复制环境变量模板：

```bash
cp .env.example .env
```

常用配置：

```env
# 固定镜像版本，避免自动跨版本更新
APP_VERSION=latest

# Web 端口
APP_PORT=5251

# 把临时视频放到大容量磁盘
RCLONE_TEMP_HOST_PATH=/你的大容量目录/fnos-search/temp
```

管理员初始账号固定为 `admin/admin`，首次登录后直接在管理后台修改。PanSou、OpenList、网盘、
TMDB、通知和分类目录也全部在管理后台配置，不放进 `.env`。


### 更新版本

更新前建议先备份 `data` 目录，然后执行：

```bash
docker compose pull
docker compose up -d
```

数据库会自动升级。发生数据库迁移时，程序会在 `data/backups` 中自动创建迁移前备份。

如果需要从源码构建：

```bash
docker compose -f docker-compose.yml -f compose.dev.yaml up -d --build
```

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

### Docker Socket 权限较高

当前主容器通过 `/var/run/docker.sock` 调用 `rclone-server`。这相当于给主容器较高的
Docker 管理权限，因此：

- 只使用可信镜像。
- 管理后台只开放给可信网络。
- 不要公开 `/admin`、`/swagger` 和 Docker API。

标准编排使用 `/var/run/docker.sock`。Rootless Docker、Podman 等非标准 Socket 环境当前需要自行
调整编排文件，暂不属于默认支持范围。

### OpenList 路径必须对应

夸克保存目录、移动云实际目录、OpenList 挂载目录和 STRM 目录可能是不同路径。
后台“分类路径”中的配置必须与 OpenList 真实目录一致，否则可能出现找不到文件、整理失败或
刷新了错误目录。

不确定时先关闭 Organizer，确认资源能够正常转存后，再单独测试整理功能。

### 合集或识别不准时不要强行整理

遇到电影合集、错误标题或无法确定的文件，建议进入 Organizer 人工审核并选择“跳过整理”。
系统会保留原文件名，只把内容移动到正确分类目录，后续交给 OpenList 或媒体库自行刮削。

### 密钥由系统自动管理

新部署会在持久化数据目录中自动生成应用签名密钥和通知加密密钥，用户无需填写。迁移或重装时
必须完整保留 `data/`，否则已经加密保存的 SMTP 密码和 Webhook 凭据将无法解密。

### 固定版本更适合长期使用

`latest` 适合首次体验。长期运行时建议把 `APP_VERSION` 固定为明确版本，例如：

```env
APP_VERSION=1.2.0
```

回滚旧镜像时，需要同时恢复升级前的 `data` 备份，不能只修改镜像标签。

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
docker compose logs --tail=200 rclone-server
```

更多部署细节见 [docs/compose-deploy.md](docs/compose-deploy.md)。
