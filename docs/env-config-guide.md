# 可选环境变量

项目不要求创建 `.env`，直接执行 `docker compose up -d` 即可启动。业务服务、云盘账号、
Token、分类目录和通知全部在管理后台配置。

## 1. 配置优先级

从低到高依次为：

```text
程序默认值 < config/config.yaml < 环境变量 < 管理后台高级配置
```

旧版业务环境变量仍尽量兼容，但新部署不建议把 PanSou、OpenList、TMDB 或云盘 Token 写进
`.env`。

## 2. 可覆盖的启动项

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `APP_VERSION` | `latest` | 镜像版本 |
| `APP_PORT` | `5251` | Web 端口 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `RCLONE_TEMP_HOST_PATH` | `./rclone/temp` | 大文件搬运暂存目录 |

管理员初始账号为 `admin/admin`，首次登录后在后台修改。

## 3. 自动管理的内容

以下内容不再要求用户通过 `.env` 配置：

- 应用签名密钥和通知加密密钥由容器生成并保存在 `data/`。
- 主容器与 rclone 容器名称固定，避免页面配置与实际容器不一致。
- 正式环境安全模式默认启用。
- 容器内业务进程固定使用非 root 用户 `10001:10001`。
- rclone 使用项目验证过的固定版本。

备份和迁移时必须保留整个 `data/` 目录，否则已保存的登录状态和加密凭据可能失效。
旧部署如果已经在 `.env` 中显式设置 `APP_SECRET_KEY` 或 `NOTIFICATION_ENCRYPTION_KEY`，升级时
继续保留原值即可；新部署无需添加。

## 4. 持久化目录

默认使用 Compose 项目目录下的相对路径：

```text
config/
data/
logs/
rclone/config/
rclone/temp/
rclone/cache/
```

媒体暂存空间不足时，通常只需要把 `RCLONE_TEMP_HOST_PATH` 改到大容量磁盘。

## 5. 源码构建

源码构建使用独立的 `compose.dev.yaml`：

```bash
docker compose -f docker-compose.yml -f compose.dev.yaml up -d --build
```

开发者需要替换基础镜像或 Python 软件源时，直接覆盖 `compose.dev.yaml` 中对应的构建参数。
完整用户示例见项目根目录的 [.env.example](../.env.example)。
