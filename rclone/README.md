# rclone 编排目录

本目录用于一体化 Docker Compose 部署时给 `rclone-server` 容器使用。

## 配置文件

Docker Compose 首次启动会自动创建空文件：

```text
rclone/config/rclone.conf
```

使用夸克到移动云等 rclone 搬运线路前，优先在管理后台“系统设置 → 飞牛与 rclone”中配置并检测连接。页面会自动写入 `[MP]`，密码不回显，连接失败时会恢复原配置。

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

## 推荐：通过 OpenList WebDAV 统一访问

如果 OpenList 已经挂载了夸克和移动云，可以把 `MP` 配成 OpenList 的 WebDAV 根目录。

这样脚本会访问：

```text
MP:离线下载/电影
MP:移动云盘A/电影
```

实际对应 OpenList 中的目录。

## 目录映射必须一致

默认情况下，本系统认为：

```text
Quark 自动转存目录 = MP:离线下载/电影
rclone 搬运目标目录 = MP:移动云盘A/电影
飞牛媒体库扫描目录 = MP:移动云盘A/电影 对应的真实目录
```

其他分类默认如下：

```text
MP:离线下载/电视剧  -> MP:移动云盘A/电视剧
MP:离线下载/动漫    -> MP:移动云盘A/动漫
MP:离线下载/综艺    -> MP:移动云盘A/综艺
MP:离线下载/其他    -> MP:移动云盘A/其他
```

如果 OpenList 里的实际目录不同，优先在管理后台“分类路径”中配置。旧版环境变量仍然兼容：

```env
RCLONE_SRC_MOVIE_DIR=夸克/离线下载/电影
RCLONE_DST_MOVIE_DIR=移动云/影视/电影
RCLONE_SRC_TV_DIR=夸克/离线下载/电视剧
RCLONE_DST_TV_DIR=移动云/影视/电视剧
RCLONE_SRC_ANIME_DIR=夸克/离线下载/动漫
RCLONE_DST_ANIME_DIR=移动云/影视/动漫
RCLONE_SRC_VARIETY_DIR=夸克/离线下载/综艺
RCLONE_DST_VARIETY_DIR=移动云/影视/综艺
RCLONE_SRC_OTHER_DIR=夸克/离线下载/其他
RCLONE_DST_OTHER_DIR=移动云/影视/其他
```

自检：

```bash
docker exec rclone-server rclone lsf "MP:你的源目录" --max-depth 1
docker exec rclone-server rclone lsf "MP:你的目标目录" --max-depth 1
```

## 密码处理

rclone 的 `pass` 字段需要使用 obscure 后的值：

```bash
docker run --rm -it rclone/rclone obscure '你的明文密码'
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
