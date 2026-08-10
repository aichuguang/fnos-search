# 07｜构建、部署与回滚

## 1. 部署目标

保持现有部署模型：

```text
同一应用镜像
  ├─ fnos-media-import      FNOS_PROCESS_ROLE=web
  └─ fnos-rclone-worker     FNOS_PROCESS_ROLE=all
```

Vue 只改变 Web 静态页面来源，不新增生产 Node 服务，不改变 Worker、SQLite、rclone、Provider relay 或挂载目录。

## 2. 开发环境

### 2.1 启动后端

继续使用当前 Python/Compose 方式。开发者需要：

- 可访问 `http://127.0.0.1:5251`；
- `FNOS_ROOT_KEY` 有效；
- 使用非生产数据库或明确知道当前本地数据影响；
- 不为前端开发关闭 CSRF。

### 2.2 启动前端

```bash
cd frontend
npm ci
npm run dev
```

Vite 代理：

```ts
server: {
  proxy: {
    '/api': {
      target: 'http://127.0.0.1:5251',
      changeOrigin: false,
    },
    '/health': 'http://127.0.0.1:5251',
    '/readyz': 'http://127.0.0.1:5251',
    '/static/uploads': 'http://127.0.0.1:5251',
  },
}
```

开发时验证：

- Cookie 能通过代理工作；
- Session 接口正常；
- 写操作 CSRF 正常；
- 上传 FormData 正常；
- 不添加 `Access-Control-Allow-Origin: *`。

## 3. 前端构建产物

建议输出：

```text
frontend/dist/
  index.html
  assets/
    app.<hash>.js
    app.<hash>.css
    feature-storage.<hash>.js
    ...
```

要求：

- 文件名包含内容 Hash；
- Route Feature 动态分包；
- Source Map 根据发布策略决定；
- 不把 `.env` Secret 注入浏览器；
- 只有 `VITE_*` 公共构建变量可进入前端；
- 构建时生成前端版本信息，例如 Git SHA、应用版本和构建时间。

## 4. Docker 多阶段构建

目标 Dockerfile 结构：

```dockerfile
FROM node:22-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ${RCLONE_IMAGE} AS rclone

FROM ${PYTHON_IMAGE}
WORKDIR /app

# 现有 rclone、Python 依赖、用户和源码步骤保持

COPY --from=frontend-build /frontend/dist /app/frontend_dist

ENTRYPOINT ["python", "/app/scripts/container_entrypoint.py"]
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5251", "--timeout", "180", "--graceful-timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
```

实际编辑时必须保留当前 Dockerfile 的：

- rclone 二进制复制；
- Python 镜像参数；
- pip 镜像降级策略；
- 非 root 用户；
- UID/GID；
- OCI Labels；
- `/app` 权限；
- 入口脚本；
- amd64/arm64 兼容。

### 4.1 Worker 影响

Worker 使用同一镜像，因此镜像中会包含前端静态文件，但 Worker 不提供外部端口。这是可接受的，优先保持单镜像一致性。

不得为了减少 Worker 镜像体积立即拆成第二个应用镜像，除非后续有明确运维收益。

## 5. Flask SPA 承载

### 5.1 静态资产

Flask 从：

```text
/app/frontend_dist/assets/*
```

返回 Hash 资产。

### 5.2 History Fallback

以下页面路由返回 `frontend_dist/index.html`：

```text
/
/submit
/submit/*
/request/*
/admin
/admin/*
```

以下路径绝不能进入 SPA Fallback：

```text
/api/*
/health
/livez
/readyz
/dependencies
/openapi.json
/swagger
/api-docs
/static/*
```

错误实现会把 API 404 变成 HTTP 200 HTML，导致前端出现难以诊断的 JSON 解析错误。

### 5.3 Python 示例边界

可以实现一个小型静态服务函数，但不得把业务路由搬入其中：

```python
def serve_spa(path: str = ""):
    candidate = frontend_dist / path
    if path and candidate.is_file():
        return send_from_directory(frontend_dist, path)
    return send_from_directory(frontend_dist, "index.html")
```

必须防止路径穿越，并使用解析后的安全目录。

## 6. 缓存策略

### 6.1 `index.html`

```text
Cache-Control: no-cache
```

允许浏览器协商缓存，但每次重新打开会检查新版本。

### 6.2 Hash Assets

```text
Cache-Control: public, max-age=31536000, immutable
```

### 6.3 用户上传资源

头像和 Logo 当前 URL 可能稳定，更新后需要版本参数或后端 ETag，避免浏览器长期显示旧资源。

### 6.4 Service Worker

首版不启用 PWA/Service Worker，避免 NAS 用户在更新镜像后仍被旧缓存困住。

## 7. 构建变量

允许的公共变量示例：

```text
VITE_APP_VERSION
VITE_BUILD_SHA
VITE_BUILD_TIME
VITE_FRONTEND_BASE
VITE_ENABLE_COMPONENT_GALLERY=false
```

禁止：

- `FNOS_ROOT_KEY`；
- Worker Token；
- Provider 凭证；
- TMDB/OpenAI Secret；
- SMTP 密码；
- OpenList 密码。

Vite 构建变量会进入浏览器包，不能作为 Secret。

## 8. CI 更新

当前 CI 在 Python 测试后增加：

```yaml
- name: Set up Node
  uses: actions/setup-node@v4
  with:
    node-version: '22'
    cache: npm
    cache-dependency-path: frontend/package-lock.json

- name: Install frontend dependencies
  working-directory: frontend
  run: npm ci

- name: Lint and typecheck frontend
  working-directory: frontend
  run: |
    npm run lint
    npm run typecheck

- name: Test frontend
  working-directory: frontend
  run: npm run test:run

- name: Build frontend
  working-directory: frontend
  run: npm run build
```

原有 Python、Ruff、Compose 和 Docker 构建门禁不得删除。

旧 JavaScript `node --check` 在旧前端完全退役后才能移除。

## 9. Compose

正常生产 Compose 不需要新增 Node 服务。源码开发 Compose 继续构建一个镜像。

需要确认：

- `docker compose config --quiet`；
- Web 仍挂载 config/data/logs/relay；
- Worker 仍挂载 rclone config/cache/temp/relay/local target；
- 前端静态文件来自镜像，不使用开发 Bind Mount；
- `FNOS_PROCESS_ROLE` 保持现有值；
- 健康检查不依赖 SPA 页面。

## 10. Preview 发布

如果提供 `/next`：

- 默认关闭；
- 只用于开发/验收；
- 仍需要管理员登录；
- 不复制后端数据；
- 与旧前端共享同一数据库和 API；
- 不允许两个前端同时对同一个危险操作重复提交；
- 清晰标注“新前端预览”。

Preview 不是长期双轨架构。正式切换稳定后删除。

## 11. 发布前验证

### 11.1 自动化

```bash
python -m pytest -q
python -m ruff check .

cd frontend
npm ci
npm run lint
npm run typecheck
npm run test:run
npm run build

cd ..
docker compose config --quiet
docker compose -f docker-compose.yml -f compose.dev.yaml build
```

### 11.2 容器冒烟

```bash
docker compose -f docker-compose.yml -f compose.dev.yaml up -d
docker compose ps
curl -fsS http://127.0.0.1:5251/health
curl -fsS http://127.0.0.1:5251/readyz
```

再检查：

- `/admin/login` 返回 SPA；
- 深链 `/admin/storage` 刷新成功；
- `/submit/search` 刷新成功；
- `/api/public/config` 返回 JSON；
- 未登录管理 API 仍返回 401，而不是 SPA HTML；
- Hash Assets 返回长期缓存头；
- Index 返回 no-cache；
- Worker 状态 Ready。

### 11.3 业务冒烟

- 管理员登录/退出；
- Storage Setup 读取；
- Provider 只读检测；
- 目录浏览；
- Preflight；
- Jobs 列表/详情；
- Public Config；
- Public Search；
- Manual Preview；
- Request Status。

涉及真实 Provider 写入的操作使用非生产账号和小文件。

## 12. 观察指标

切换后至少观察：

- 前端 Chunk 加载失败；
- 401/403 激增；
- `/api/public/config` 5xx；
- JSON 解析错误；
- 页面路由 404；
- 静态资产 404；
- Jobs/Storage/Organizer API 5xx；
- Worker readiness；
- Web 日志和 Worker 日志；
- 用户反馈的白屏、旧缓存和移动端溢出。

建议前端错误上报先使用后端日志接口或控制台诊断，不必在首版引入外部 SaaS。若增加上报，必须脱敏。

## 13. 回滚策略

### 13.1 原则

前端重构不修改数据库 Schema，因此前端发布回滚应只涉及镜像和 Compose，不恢复数据库。

### 13.2 回滚步骤

```text
1. 停止候选 Web/Worker。
2. 将 APP_VERSION 切回上一稳定镜像。
3. docker compose up -d --remove-orphans。
4. 检查 /readyz。
5. 检查旧管理端和访客端。
6. 确认 Worker 未重复启动任务。
```

若本轮同时包含后端非 Schema Bug 修复，回滚旧镜像会同时回滚这些修复，应在发布说明中明确。

### 13.3 禁止的“回滚”

- 只覆盖容器内静态文件；
- 手工修改正在运行的 `dist`；
- 新旧 Web 容器同时对外提供不同页面且共享 Session；
- 删除数据库；
- 修改 `FNOS_ROOT_KEY`；
- 清空浏览器全部存储作为唯一解决方案。

## 14. 旧前端退役后的回滚

旧前端文件从新源码删除后，仍可以通过上一稳定镜像回滚。不要为了回滚在当前镜像长期保留两套生产前端。

发布文档必须记录：

- 上一稳定镜像标签；
- 新镜像标签；
- Git SHA；
- 前端 Build SHA；
- 是否包含后端修改；
- 回滚验证结果。

## 15. 部署完成标准

- [ ] 前端 Build 进入镜像；
- [ ] Web/Worker 双容器启动；
- [ ] SPA 深链可刷新；
- [ ] API 不被 Fallback 捕获；
- [ ] Cache-Control 正确；
- [ ] `/readyz` 通过；
- [ ] 管理端和访客端冒烟通过；
- [ ] amd64/arm64 构建通过；
- [ ] 回滚演练通过；
- [ ] 发布说明包含版本、风险和未验证真实链路。

