# V1 v29 硬切换发布与回滚手册

## 1. 适用范围

本手册适用于已有数据库从 v1-v28 离线升级到 v29。升级工具会先在同目录临时副本中补齐普通迁移至 v28，再执行 v29 硬切换和完整性校验，最后原子替换源数据库。该发布不支持滚动升级、新旧容器并存、旧 API 字段别名或旧任务执行回退。

应用启动不再自动迁移已有数据库：

- 全新数据库会直接初始化为 v29；
- 已有 v29 数据库正常启动；
- v1-v28 数据库拒绝启动，并提示运行本手册中的离线命令；
- 高于 v29 的数据库拒绝降级启动和降级迁移。

移动云和天翼的跨 Provider 目标链路均为直连 Provider API 上传：

```text
冻结来源
  -> OpenList 来源落地
  -> rclone 下载到 /data/relay/job-<id>
  -> Mobile 或 Tianyi 冻结账号修订对应的原生 API uploader
  -> 原生目标校验
  -> ACK
  -> 清理
```

rclone 不向移动云或天翼上传文件字节；WebDAV writable 也不能替代这两个 Provider 的 `upload_verified` 与 `organizer_mutation_verified` 双证明。

## 2. 发布前检查

1. 固定新镜像版本，不使用临时构建或漂移的 `latest`。
2. 备份并单独保存：
   - `APP_DATA_HOST_PATH`；
   - `FNOS_ROOT_KEY`；
   - 配置目录；
   - rclone 配置；
   - 当前镜像标签与 Compose 文件。
3. 确认磁盘有足够空间同时保存数据库、迁移备份和 Provider relay 临时文件。
4. 记录维护窗口开始时间，停止公共提交、追更、热榜和 Scheduler。
5. 停止 Web、Worker、Scheduler 及所有旧版辅助容器，确认没有进程继续连接 SQLite。

## 3. 离线预演

```bash
docker compose down
docker compose pull
docker compose run --rm --no-deps fnos-media-import \
  python -m fnos_media_import.v1_cutover \
  --database /app/data/app.db --dry-run
```

预演通过 SQLite backup API 克隆源库，只在临时副本中模拟 v1-v28 到 v29 的完整升级，不修改源数据库。输出脱敏 JSON，发布人员必须审核：

- `current_schema_version`、`pre_cutover_migrations` 和目标 `schema_version=29`；
- `simulation_validated=true`；
- 保留、删除任务数量；
- 无效原因分布；
- Worker、Transfer、Organizer、Rclone、访客请求、追更和热榜关联清理数量；
- 保留/删除任务 ID 集合的 SHA-256，而不是明文 ID。

若删除量或原因分布与预期不符，不得执行 `--apply`。

## 4. 执行硬切换

```bash
docker compose run --rm --no-deps fnos-media-import \
  python -m fnos_media_import.v1_cutover \
  --database /app/data/app.db --apply
```

`--apply` 会：

1. 获取数据库旁路独占锁；
2. 确认数据库没有活跃写事务，并执行 WAL checkpoint；
3. 使用 SQLite backup API 创建 `backups/app_before_schema_migration_v29_*.db`；
4. 在同目录临时副本中将 v1-v27 普通迁移补齐至 v28；v28 数据库直接进入下一步；
5. 使用重建表方式更新 `import_jobs`；
6. 保留合法 canonical V1 任务，删除旧版、缺失或损坏计划的任务及执行关联；
7. 按既定规则解除访客请求、追更和热榜候选的任务绑定；
8. 执行 `PRAGMA foreign_key_check` 和 `PRAGMA integrity_check`；
9. 校验通过后清理 WAL sidecar，并使用 `os.replace` 原子替换源数据库；
10. 输出原版本、普通迁移列表、备份路径、清理统计和 ID 集合哈希。

副本迁移、完整性校验或原子替换任一步失败，源数据库不会被迁移副本覆盖。迁移失败时不得启动新版本；保留 CLI 输出和数据库备份，先关闭遗漏的 SQLite 查看工具或服务，定位错误后重试，必要时直接回滚。

## 5. 启动与健康检查

```bash
docker compose up -d --remove-orphans
docker compose ps
curl -fsS "http://127.0.0.1:${APP_PORT:-5251}/readyz"
docker compose logs --tail=200 fnos-media-import
docker compose logs --tail=200 fnos-rclone-worker
# 仅在启用 COMPOSE_PROFILES=pansou 时执行：
docker compose logs --tail=200 pansou
```

随后验证：

1. 新建任务返回 `source_provider_kind`、`source_form`、`search_source_id`、`transfer_strategy`、`destination_id` 和 `destination_provider_kind`，不含 `route`、`target_route` 或任务语义 `source_type`。
2. Quark、Tianyi、Mobile、SixPan 使用非生产账号完成只读探测。
3. 至少完成一次对应传输流程：
   - Quark 同源 `native_save`；
   - Tianyi 同源 `native_save`；
   - Mobile 同源 `native_save`；
   - SixPan `native_offline`；
   - 跨 Provider 到 Mobile 的移动云 API relay；
   - 跨 Provider 到 Tianyi 的 189CloudPC API relay。
4. 验证 OpenList 可见性、Organizer MOVE/DELETE、取消、重试、Worker 重启恢复和清理。

全部冒烟通过后才能解除维护模式。

## 6. 回滚

1. 立即重新进入维护模式并停止全部新版本进程。
2. 导出 v29 发布后任务与外部 Provider operation 清单，避免回滚后重复提交。
3. 恢复 CLI 输出中记录的 v29 前数据库备份，并恢复与其匹配的配置和根密钥。
4. 部署发布前旧镜像与旧 Compose。
5. 执行旧版本健康检查和最小业务冒烟。

旧镜像不能直接读取 v29 数据库；应用镜像和数据库必须作为同一回滚单元。

数据库恢复示例：

```bash
docker compose down
cp /app/data/app.db /app/data/app.db.failed-v29
cp /app/data/backups/app_before_schema_migration_v29_YYYYMMDD_HHMMSS.db \
  /app/data/app.db
rm -f /app/data/app.db-wal /app/data/app.db-shm
```

## 7. 当前验证边界

本地自动化已覆盖迁移、契约、Adapter、Planner、Validator、RuntimeBuilder、Repository 边界和静态门禁。当前开发环境未访问真实 Provider 账号，也未执行真实外部上传；真实非生产账号探测与传输仍是发布维护窗口内的必做门槛。
