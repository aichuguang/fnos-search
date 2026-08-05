# Docker Compose 部署说明

本文补充 README 中的部署细节。普通用户只需要 `docker-compose.yml`，不需要自行安装 Python、rclone 或数据库。

## 1. 支持范围

- Docker Compose v2
- Linux 容器
- `amd64`、`arm64`
- 飞牛、群晖、Unraid、普通 Linux 服务器和 Docker Desktop

项目使用两个容器：

- `fnos-media-import`：Web、后台任务和调度器
- `rclone-server`：执行 OpenList WebDAV 与媒体搬运命令

主容器通过 Docker Socket 调用 `rclone-server`，因此必须挂载 `/var/run/docker.sock`。不要把管理后台开放给不可信网络。

## 2. 使用正式镜像

在保存了 `docker-compose.yml` 的目录中执行：

```bash
docker compose up -d
```

默认拉取：

```text
aichuguang/fnos-search:latest
rclone/rclone:1.70.3
```

首次启动会自动创建 `config`、`data`、`logs`、`rclone/config`、`rclone/temp` 和 `rclone/cache`。`.env` 是可选文件，不创建也能启动。

浏览器访问：

```text
http://服务器IP:5251
```

初始账号为 `admin/admin`。首次登录后应立即修改密码。

## 3. NAS Docker 面板

在飞牛、群晖或其他 NAS 的 Compose/项目面板中导入 `docker-compose.yml`，选择一个用于保存项目文件的目录，然后启动即可。

默认相对目录会落在 Compose 项目目录下。只有需要更换端口、镜像版本、时区或大文件暂存位置时，才创建 `.env` 并覆盖相应变量。

如果 NAS 面板不支持 `host-gateway`，需要将后台配置中的 OpenList、PanSou 等服务地址改为宿主机局域网 IP。

## 4. 从源码构建

开发或本地测试时，同时加载开发覆盖文件：

```bash
docker compose -f docker-compose.yml -f compose.dev.yaml up -d --build
```

该命令构建本地镜像 `fnos-search-local:dev`。普通部署不要加载 `compose.dev.yaml`。

## 5. 配置 rclone

容器首次启动会自动创建空的 `rclone/config/rclone.conf`。登录管理后台，进入“系统设置 → 飞牛与 rclone”，填写 Remote 名称、OpenList WebDAV URL、用户名和密码，然后点击“保存并检测”。

没有 SSH 的 NAS 用户也可以直接在页面完成配置。密码不会回显；以后修改地址或用户名时，密码留空表示保留已有密码。

页面无法使用时，可以进入容器运行配置向导：

```bash
docker exec -it rclone-server rclone config
```

OpenList WebDAV remote 建议命名为 `[MP]`，配置方式见 [rclone/README.md](../rclone/README.md)。配置完成后验证：

```bash
docker exec rclone-server rclone lsd MP:
```

## 6. 持久化数据

以下目录必须保留：

| 目录 | 内容 |
| --- | --- |
| `config/` | 可选的文件配置 |
| `data/` | SQLite 数据库、后台设置、备份和自动生成的密钥 |
| `logs/` | 应用日志 |
| `rclone/config/` | rclone 配置 |
| `rclone/temp/` | 搬运暂存文件 |
| `rclone/cache/` | rclone 缓存 |

更新、迁移机器或删除 Compose 项目前，至少备份 `data/` 和 `rclone/config/`。

## 7. 更新和回滚

更新正式镜像：

```bash
docker compose pull
docker compose up -d
```

生产环境建议在 `.env` 中固定版本：

```env
APP_VERSION=1.2.0
```

回滚时不仅要改回旧镜像标签，还要恢复升级前的 `data` 备份，避免新数据库结构与旧程序不兼容。

## 8. 排查命令

```bash
docker compose ps
docker compose logs --tail=200 fnos-media-import
docker compose logs --tail=200 rclone-server
curl http://127.0.0.1:5251/health
```

两个容器都应为 `healthy`。如果主容器无法调用 rclone，优先检查 Docker Socket 和挂载目录权限。
默认编排固定使用 `/var/run/docker.sock` 以及 `rclone-server` 容器名；Rootless Docker 等环境需自行调整编排文件。
