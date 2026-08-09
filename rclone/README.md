# rclone 编排目录

本目录用于独立 `fnos-rclone-worker` 保存 rclone 配置、缓存和搬运临时文件。

## 配置文件

Docker Compose 首次启动会自动创建空文件：

```text
rclone/config/rclone.conf
```

启用跨 Provider 来源落地、受控本地下载或通用 OpenList 目标前，优先在管理后台“系统设置 → 飞牛与 rclone”中配置并检测连接。页面会通过 Worker 自动写入 `[MP]`，密码不回显，连接失败时会恢复原配置。

移动云和天翼作为跨 Provider 目标时，rclone **只负责把来源下载到**
`/data/relay/job-<id>`。最终文件写入分别由移动云原生 API uploader 和
189CloudPC 原生 API uploader 完成；OpenList/WebDAV 仅承担来源读取、目标可见性与
Organizer 的 MOVE/DELETE，不是这两个目标的上传数据面。

如果是源码调试或页面无法使用，也可以复制示例后手工编辑：

```bash
cp rclone/config/rclone.conf.example rclone/config/rclone.conf
```

确保里面存在：

```ini
[MP]
```

`MP` 是系统默认使用的 remote 名称。如果你想换名字，需要同步修改 `.env`：

```env
RCLONE_REMOTE_NAME=你的remote名
```

## 通过 OpenList WebDAV 读取来源

如果 OpenList 已经挂载 Provider，可以把 `MP` 配成 OpenList 的 WebDAV 根目录。

Worker 会按冻结执行计划访问任务级来源 landing，例如：

```text
MP:夸克账号/任务落地/job-123
```

跨 Provider 写入移动云或天翼时，rclone 的目标是受控本地目录：

```text
/data/relay/job-123
```

随后 Python Provider uploader 使用计划冻结的目标账号 ID、revision 和原生目录 ID 直连云盘 API 上传。

## 目录与能力边界

- 来源、目标、账号修订和任务目录全部来自 V1 冻结执行计划，不再读取旧版分类目录环境变量。
- `RCLONE_SRC_*_DIR`、`RCLONE_DST_*_DIR` 等旧配置不会参与新任务规划。
- 普通 `rclone_copy` 只能写入计划允许的通用 OpenList 目标或 `/storage-targets` 受控本地目标。
- `relay_upload_provider` 只能下载到 `/data/relay/job-<id>`；移动云和天翼的最终写入必须走各自原生 API。
- 移动云和天翼保存位置必须同时通过 `upload_verified` 与 `organizer_mutation_verified`，WebDAV writable 不能代替原生上传证明。

只读自检示例：

```bash
docker exec fnos-rclone-worker rclone lsf "MP:你的源目录" --max-depth 1 --config /config/rclone/rclone.conf --cache-dir /cache
```

## 密码处理

rclone 的 `pass` 字段需要使用 obscure 后的值：

```bash
docker exec fnos-rclone-worker rclone obscure '你的明文密码'
```

把输出结果填入：

```ini
pass = 输出结果
```

## 临时目录

`rclone/temp` 会挂载到容器 `/temp`。

如果视频文件较大，建议在 `.env` 中把它改成 NAS 大空间路径：

```env
RCLONE_TEMP_HOST_PATH=/volume1/docker/fnos-media-import-rclone-temp
```
