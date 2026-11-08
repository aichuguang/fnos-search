# 新入库身份整理链路（2026-11-08）

## 已生效的规则

- 仅面向新入库；不扫描修复、改名合并或兼容历史无标签脏目录。
- 身份键为 `(media_type, namespace, value)`，唯一性限定在任务冻结的 OpenList 连接和目标媒体库中，不跨网盘改路由。
- 新作品根：`标题 (年份) [tmdb-ID]`；没有年份就省略年份，不编造。无 TMDB 可采用已提交外部身份或确定性 local 身份。
- 同身份复用已存在目录，不因任务标题、TMDB 中文译名变化另建根。追更找不到已有根进入审核；允许在已确认根内创建 `Season 02` 等新季。
- 只解析真实视频白名单。ISO、TAR、ZIP 等不作为视频；正确关联的字幕、NFO、海报继续随视频整理，不允许只靠字幕创建“剧集”。Organizer 不主动删除不支持文件或无法解析的小文件。
- 显式季号优先，保留 S00；季号证据冲突、标题错配、缺集号、身份冲突均阻止自动写盘。管理员可修订映射和 TMDB ID 后确认；不再提供“跳过整理、原名直通”绕过入口。
- 已存在目标不覆盖，不以相同集号或相同画质推定内容相同并删除源文件；无法安全分辨的重复交人工审核。

## 身份清单

每个标准作品根包含系统附件 `.fnos-identity.json`，保存版本、主身份和已验证身份别名。它不是待解析的视频，也不是用户提交的 NFO。

目录是实际位置来源，清单使别名不依赖 import_jobs/update_subscriptions 表。local 哈希只保证相同标题、年份、媒体类型输入一致，不能凭空识别未知译名，因此新 local 身份必须人工确认后首入。后续确认 TMDB 时可以将其记录为别名，保持原根不动，不自动改名。

保留标签的同身份目录改名可被定位；下一次成功整理通过有日志的操作更新清单中的展示标题。目录标签的媒体类型/主 ID 与清单冲突、损坏清单或身份重复均拒绝写入。

## 写入、恢复与审核

计划生成只读。apply 获取任务租约及媒体库锁后，重新扫描并校验已保存的路径、身份、操作顺序和前置状态；过期计划拒绝执行，不悄悄换路径。

执行顺序：新作品根（如需）→ 身份清单 → 季目录 → 视频/关联附件 → 目标确认 → 订阅目录缓存回写 → 原有 STRM 刷新链。

身份写入失败立即停止媒体移动。上传成功但响应丢失可按内容对账续跑。逆向日志覆盖清单及文件移动；后续任务改变了清单、目录仍有其他文件依赖身份时，回滚拒绝撤销别名，不覆盖后续工作。

## 发布

未新增环境变量。新增依赖能力：目标 OpenList 存储必须支持小文本上传 `/api/fs/put`、文件读取及现有移动/改名权限。身份清单单文件上限 64 KiB；上传同步执行，成功后重读核验。

数据库当前版本 **v30**。新空库正常初始化；v29→v30 仅给 `update_subscriptions` 增加三列，不清理、回填或改写历史媒体。旧于 v29 的部署仍涉及原有硬切换，应先阅读对应发布手册并审核 dry-run，不能把它当作本次纯增量升级。

对本地源码部署，在维护窗口停止所有使用数据库的服务后：

```bash
docker compose down
docker compose -f docker-compose.yml -f compose.dev.yaml build fnos-media-import fnos-rclone-worker
docker compose -f docker-compose.yml -f compose.dev.yaml run --rm --no-deps fnos-media-import \
  python -m fnos_media_import.v1_cutover --database /app/data/app.db --dry-run
# 确认预演为预期的 v30 增量迁移后：
docker compose -f docker-compose.yml -f compose.dev.yaml run --rm --no-deps fnos-media-import \
  python -m fnos_media_import.v1_cutover --database /app/data/app.db --apply
docker compose -f docker-compose.yml -f compose.dev.yaml up -d --force-recreate
```

升级前另行保存 data、配置及 FNOS_ROOT_KEY；离线迁移自身也生成数据库备份。本轮没有执行上述部署或真实数据库迁移。

回退代码前停止所有新进程，并恢复相应数据库版本的备份；代码/数据库回退不撤销网盘移动。需要撤销具体入库时使用其操作日志回滚；不要启动旧版 Organizer 继续处理新库，也不要靠切换 legacy 模式恢复媒体。

## 验证边界

已用本地单测、mock OpenList 以及真实临时 SQLite 验证解析、身份、首入/追更、审核/apply、上传确认失败恢复、订阅回写及完整回滚。未登录真实 TMDB/OpenList、未执行 NAS 部署或浏览器点击验证。

身份扫描会读取带标签目录的清单，plan/apply 都要求可验证快照。尚未做真实网盘时延测量，不能承诺原方案的“232 目录小于 2 秒”；媒体库增长后应在保留锁与新鲜度校验的前提下优化扫描，而不是用未经校验的缓存放行写入。
