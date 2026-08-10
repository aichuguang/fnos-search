# 01｜当前前端与接口现状审计

## 1. 审计范围与结论

本审计基于 2026-08-10 工作区实际代码、路由注册、OpenAPI 输出、运行实例和 Git 状态，不仅依据 README 或设计文档。

结论：当前产品功能覆盖已经较完整，旧前端也投入了大量交互和边界处理，但其组织方式已经无法支持低成本持续开发。问题集中在以下四点：

1. 页面结构、业务状态、接口调用和 HTML 渲染混在大脚本中；
2. 公共能力通过 `window.*` 和 DOM ID 共享，没有稳定组件契约；
3. 页面重复实现请求、加载、错误、分页、弹窗、选择和轮询；
4. 后端 API 数量足够，但 OpenAPI 不完整且响应格式存在历史差异，不能直接生成可靠客户端。

这意味着重构不能只做“换成 Vue 语法”，否则会把原有巨型脚本转化成巨型 `.vue` 文件。必须同时建立 Feature 边界、API 适配层和组件系统。

## 2. 代码规模

### 2.1 总体规模

| 区域 | 文件数 | 约代码行数 |
|---|---:|---:|
| `fnos_media_import/` | 246 个 Python 文件 | 82,000+ |
| `tests/` | 95 个文件 | 28,000+ |
| `static/` | 26 个 JS/CSS 文件 | 24,000+ |
| `templates/` | 6 个模板 | 1,500+ |

### 2.2 前端热点文件

| 文件 | 约行数 | 主要职责 | 问题 |
|---|---:|---|---|
| `static/product.css` | 10,656 | 全产品主题、页面、组件、响应式 | 选择器范围过大，修改影响不可预测 |
| `static/admin.js` | 2,097 | 后台公共状态、设置、模块装配、接口、渲染 | 实际成为第二个应用组合根 |
| `static/public-submit.js` | 2,037 | 资源树、选集、确认、验证码、提交、状态 | 多个独立用例混在单文件 |
| `static/admin-storage-v1.js` | 1,343 | 账号、认证、OpenList、目录树、位置、规则、预检 | 一个脚本承载完整业务域 |
| `static/admin-updates.js` | 1,094 | 追更订阅、运行、候选、TMDB | 页面和业务逻辑耦合 |
| `static/admin-organizer.js` | 1,011 | Organizer 列表、映射、审批、执行、回滚 | 状态复杂、动态 HTML 较多 |
| `static/public.js` | 941 | 访客应用装配和公共行为 | 函数数量多，职责模糊 |
| `static/admin-jobs.js` | 887 | Dashboard、Requests、Jobs | 三个 Feature 混在一起 |
| `static/public-search.js` | 780 | 搜索和资源详情 | 与提交模块通过共享状态耦合 |
| `templates/admin.html` | 972 | 后台所有 Tab、设置、弹窗和容器 | 所有页面一次性加载 |
| `templates/submit.html` | 331 | 搜索、链接、热榜、查询和确认框 | 多页面行为集中在一个模板 |

### 2.3 当前全局模块

旧前端通过下列对象共享能力：

- `window.FnosUI`
- `window.FnosAdminBootstrap`
- `window.FnosAdminJobs`
- `window.FnosAdminMedia`
- `window.FnosAdminRclone`
- `window.FnosAdminOrganizer`
- `window.FnosAdminUpdates`
- `window.FnosAdminTrending`
- `window.FnosAdminNotifications`
- `window.FnosAdminAdapters`
- `window.FnosPublicBootstrap`
- `window.FnosPublicSearch`
- `window.FnosPublicSubmit`
- `window.FnosPublicTrending`
- `window.FnosResourceSearchWorkbench`
- `window.showConfirmDialog`
- `window.showPromptDialog`
- `window.showAlertDialog`
- `window.refreshCustomSelects`
- `window.syncCustomSelect`

这些对象缺少编译期依赖检查。脚本顺序、对象是否初始化、函数签名变化和共享状态字段变化都只能在运行时发现。

## 3. 当前页面清单

### 3.1 管理端入口

| 当前 URL | 页面/区域 | 当前主要文件 |
|---|---|---|
| `/admin/login` | 管理员登录 | `templates/admin_login.html`、`static/admin_login.js` |
| `/admin` | 后台单页壳 | `templates/admin.html`、`static/admin.js` |
| `/admin/requests` | 实际仍渲染相同后台壳 | `templates/admin.html` |
| `/admin/jobs` | 实际仍渲染相同后台壳 | `templates/admin.html` |

后台壳内部包含以下 Tab：

- `overview`：总览；
- `requests`：访客请求；
- `jobs`：入库任务；
- `updates`：追更；
- `trending`：热榜；
- `media`：媒体刷新；
- `organizer`：目录整理；
- `rclone`：rclone 与任务日志；
- `adapters`：搜索源、通知、网盘入库、系统设置、资料等设置工作区。

当前问题：URL、Tab 状态和真实页面边界不一致。大量功能在同一 HTML 中一次性存在，无法按路由分块加载，也不利于权限、错误边界和页面级测试。

### 3.2 访客端入口

| 当前 URL | 页面/区域 | 当前主要文件 |
|---|---|---|
| `/` | 302 跳转 `/submit` | Flask handler |
| `/submit` | 搜索、链接提交、热榜、编号查询 | `templates/submit.html`、`static/public*.js` |
| `/request/<token>` | 独立提交状态页 | `templates/request_status.html`、部分公共脚本 |

`/submit` 内部包含四个 Tab：

- 搜索资源；
- 链接提交；
- 每日热播；
- 提交查询。

当前页面已经处理移动端、ARIA、焦点、确认框、资源文件树和多 Provider 选集，新前端必须逐项迁移这些行为。

## 4. 当前脚本职责映射

| 旧文件 | 当前职责 | 目标归属 |
|---|---|---|
| `static/ui-core.js` | Toast、JSON 请求、Session、焦点工具 | `src/api/`、`src/components/ui/`、`src/composables/` |
| `static/ui-dialog.js` | Confirm/Prompt/Alert | `AppDialog`、`useDialog` |
| `static/ui-select.js` | 自定义 Select | `AppSelect` 封装 |
| `static/admin_login.js` | 登录与 Session 跳转 | `features/auth/` |
| `static/admin-bootstrap.js` | 后台 Tab、抽屉、加载、导航 | `app/layout/`、Vue Router |
| `static/admin.js` | 总装配、设置、表单、请求包装 | 拆到各 Feature，禁止保留等价巨型文件 |
| `static/admin-jobs.js` | Dashboard、Requests、Jobs | `features/dashboard/`、`features/requests/`、`features/jobs/` |
| `static/admin-storage-v1.js` | 账号、认证、OpenList、位置、规则、预检 | `features/storage/` |
| `static/admin-organizer.js` | Organizer | `features/organizer/` |
| `static/admin-rclone.js` | rclone、日志、日期选择 | `features/rclone/`、公共日期组件 |
| `static/admin-media.js` | 媒体库刷新 | `features/media/` |
| `static/admin-updates.js` | 追更 | `features/updates/` |
| `static/admin-trending.js` | 热榜管理 | `features/trending/` |
| `static/admin-notifications.js` | 通知 | `features/notifications/` |
| `static/admin-system.js` | 资料、安全、历史清理 | `features/profile/`、`features/system/` |
| `static/admin-adapters.js` | 搜索源和六盘检测 | `features/search-settings/`、`features/storage/` |
| `static/public-bootstrap.js` | 访客 Tab、Hash、初始化 | `app/router/`、`features/public-shell/` |
| `static/public-search.js` | 搜索、详情 | `features/public-search/` |
| `static/public-submit.js` | 文件树、选集、提交、查询 | `features/public-submit/`、`features/request-status/` |
| `static/public-trending.js` | 访客热榜 | `features/public-trending/` |
| `static/public.js` | 公共状态与模块装配 | 拆入 App 壳和 Feature；不得创建 `public.ts` 巨型替代物 |
| `static/resource-search-workbench.js` | 搜索结果公共渲染/轮询 | `features/resource-search/` |

## 5. 当前样式现状

### 5.1 已有优点

旧样式并非完全没有体系，已经存在主题变量：

```css
--ui-bg
--ui-bg-soft
--ui-card
--ui-card-strong
--ui-line
--ui-line-soft
--ui-text
--ui-muted
--ui-blue
--ui-green
--ui-orange
--ui-red
--ui-purple
```

同时已经覆盖：

- 深色玻璃拟态产品视觉；
- 多种屏幕宽度；
- `focus-visible`；
- `prefers-reduced-motion`；
- 移动端触控尺寸；
- 状态颜色、按钮、卡片、表格、抽屉和弹窗。

新前端应提取并继承这些产品识别，而不是默认套用第三方组件库主题后丢失现有视觉。

### 5.2 当前问题

- 页面样式和组件样式没有边界；
- 同一个控件在不同区域存在多个近似实现；
- 响应式断点数量多且缺少统一变量；
- 选择器大量依赖 `body[data-page]`、ID 和深层结构；
- CSS 修改难以判断影响范围；
- 第三方组件接入时没有统一主题桥接层。

## 6. 当前接口事实

### 6.1 实际应用与 OpenAPI 差距

通过临时数据库构造真实 Flask 应用并读取 `app.url_map`：

| 指标 | 数量 |
|---|---:|
| 实际 Flask 路径 | 160 |
| 实际 HTTP 操作 | 193 |
| OpenAPI 路径 | 117 |
| OpenAPI HTTP 操作 | 136 |
| OpenAPI 缺少操作 | 57 |

缺失项主要集中在：

- Provider Catalog；
- Provider Accounts；
- Provider Auth Sessions；
- OpenList Connections；
- OpenList Mounts；
- Storage Setup 聚合接口；
- Storage Targets；
- Source Assignments；
- Routing Matrix；
- `PUT /api/admin/notifications`；
- `POST /api/admin/settings/all`；
- SPA 页面路由。

因此当前不能直接执行“根据 OpenAPI 自动生成全部前端 SDK”。阶段 0 必须先补齐规范或为首批 Feature 编写经过测试的手工类型适配层。

### 6.2 API 已具备的关键能力

现有后端已经足以支撑 SPA：

- 管理员登录、退出和 Session；
- Cookie 认证与 CSRF；
- Provider 账号与授权会话；
- OpenList 连接与挂载；
- Storage Setup 聚合读写接口；
- Requests、Jobs、Organizer、rclone、Updates、Trending、Notifications；
- 公共搜索、预览、文件树、提交、验证码和状态查询；
- `/health`、`/readyz` 和 `/openapi.json`。

本轮无需重新开发一套后端，只需修复阻断错误、补齐契约并增加 SPA 静态承载。

### 6.3 审计时确认的运行错误

当前运行实例：

- `/health` 返回 200；
- `/readyz` 返回 200；
- `/api/public/config` 在六盘账号未激活时返回 500。

错误链路：

```text
public_web._adapter_placeholders()
  -> hasattr(importer, "describe")
  -> ActiveProviderImporter.__getattr__()
  -> resolve inactive sixpan account
  -> ActiveProviderUnavailable
  -> HTTP 500
```

文档编写期间，工作区已经出现针对该问题的未提交修复：新增安全获取可选 Provider 方法的逻辑，并补充 SixPan 未激活回归。由于改动仍处于工作区状态，必须在访客端迁移前完成定向测试、真实接口复测和全量回归后，才能将该问题标记为关闭。

## 7. 当前交互复杂度清单

以下区域不能按普通 CRUD 页面处理：

### 7.1 Provider 登录

- 不同 Provider 有不同认证方式；
- 可能存在二维码、设备码、密码、短信或验证码；
- Auth Session 需要轮询；
- 失败响应包含 `stage`、`code`、`field_errors`、`retryable`；
- 登录成功后还要创建/更新账号并进行只读探测。

### 7.2 原生目录和 OpenList 目录

- 懒加载树；
- 根目录 ID 因 Provider 不同而不同；
- 有的目标保存路径，有的目标保存 `folder_id`；
- 需要保留显示路径和真实定位器；
- 展开、折叠、重试、选择、滚动和键盘行为必须稳定。

### 7.3 资源文件选集

- Quark、移动云、天翼返回字段不同；
- 文件夹支持继续展开；
- 需要维护已选文件/文件夹、忽略列表和 Provider 专属快照；
- 未选择时可能代表保存整个分享；
- 选集需要在详情、确认和提交之间保持一致。

### 7.4 Jobs 与 Organizer

- 状态不是单一字符串展示，而是包含事件、执行计划、Worker、rclone 和 Organizer 多阶段信息；
- 操作按钮需要根据状态、能力和可取消性动态显示；
- 重试、取消、删除等操作必须防止重复提交；
- 日志和时间线数据量可能较大。

### 7.5 轮询

当前多个脚本自行使用 `setTimeout`/`setInterval`。新前端必须统一轮询规则：

- 页面不可见时暂停或降低频率；
- 路由离开时取消；
- 请求重叠时不重复发起；
- Mutation 不得因网络错误自动重复提交；
- 终态后停止轮询。

## 8. 应保留的旧前端行为

重构不是只追求代码整洁，以下现有能力必须保留：

- 中文错误提示和业务状态说明；
- 默认密码安全提醒；
- ARIA Tab、Dialog、Status 和 Alert；
- 弹窗焦点锁定和关闭后焦点恢复；
- 键盘操作和 `focus-visible`；
- 移动端响应式和触控交互；
- 慢速入库二次确认；
- 任务操作的防重复提交；
- 目录读取错误局部展示和重试；
- 后端 `trace_id` 的错误诊断价值；
- 空态、加载态和业务不可用态；
- 现有站点 Logo、头像和暗色视觉。

## 9. 风险优先级

### P0：开始重构前必须解决

1. `/api/public/config` 可选 Provider 未激活导致 500。
2. 建立前端专用 API Client 和统一错误模型。
3. 明确 OpenAPI 缺失范围，禁止直接生成错误 SDK。
4. 当前功能改动先形成可测试基线，避免与重构混入同一提交。

### P1：首个 Feature 迁移时必须解决

1. Session/CSRF 生命周期。
2. Provider 登录状态机。
3. 通用懒加载目录树。
4. Query 与 Pinia 的状态所有权。
5. 组件库封装和主题令牌。
6. 路由级错误边界和按需加载。

### P2：生产切换前必须解决

1. OpenAPI 全量覆盖。
2. Playwright 核心流程。
3. 可访问性扫描。
4. 生产缓存策略。
5. Docker 多架构前端构建。
6. 旧入口回滚和静态资源清理。

## 10. 审计结论

现有前端不适合继续做局部整理，正式迁移到 Vue 是合理选择。但新实现必须以“Feature 边界、统一 API、组件契约和自动化验收”为核心，而不是简单把每个旧 JS 文件转换成同名 Vue 页面。

后端已经具备主要业务能力。本轮只需要：

- 修复接口错误；
- 补齐 OpenAPI；
- 对不一致响应做前端适配；
- 增加 SPA 托管与构建；
- 不改变核心业务和数据库。
