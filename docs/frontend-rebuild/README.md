# 前端重构总纲（AI 执行版）

> 文档状态：已定稿，可作为 AI 开发主索引
> 适用项目：`fnos-search`
> 目标前端：Vue 3 + TypeScript + Vite + TWCSS
> 后端策略：保留现有 Flask、SQLite、Worker 与 Provider 业务链路，只做 API 契约补齐和 SPA 承载改造
> 编写日期：2026-08-10

## 1. 文档目的

本目录是一套可直接交给 AI 编码代理执行的前端重构规格。它不是方向性建议，而是包含以下内容的实施基线：

- 当前前端与后端接口的事实审计；
- 已锁定的技术选型与目录结构；
- 公共组件、业务组件和状态管理边界；
- 多主题、组件皮肤、页面级布局、响应式和定制 UI 验收基线；
- 前后端 API 契约、认证、错误和类型生成规则；
- 从旧 Jinja + 原生 JavaScript 到 Vue SPA 的逐阶段迁移计划；
- 单元、组件、接口、端到端、可访问性与视觉回归门禁；
- Docker 构建、Flask 静态托管、发布、观察和回滚方案；
- AI 每次执行任务时必须遵守的步骤、禁止事项和交付格式；
- 可直接领取执行的任务编号、依赖关系和验收条件。

AI 在开始任何重构任务前，必须先阅读本文件以及对应阶段文档，不得只根据任务标题直接修改代码。

## 2. 已锁定的核心决策

以下决策在本轮重构中视为固定约束。除非用户明确改变要求，AI 不得自行替换：

1. **前端正式重建，不继续扩展旧的原生 JavaScript 架构。**
2. **后端不推倒重写。**保留 Flask、SQLite、Repository、Provider、Planner、Transfer、Organizer、Worker 和现有数据库迁移。
3. **使用 Vue 3 + TypeScript + Vite。**不同时引入 React、Nuxt 或第二套前端框架。
4. **继续使用同域 Cookie Session 和 CSRF。**生产环境不单独部署 Node API 服务，不引入跨域认证。
5. **采用模块化单体前端。**按业务 Feature 组织代码，不再按一个巨型 `admin.js` 或全局 `window.*` 模块组织。
6. **服务端状态使用 TanStack Vue Query。**Pinia 只管理登录摘要、布局和本地偏好，禁止把所有 API 数据塞入全局 Store。
7. **第三方 UI 组件必须经过项目封装层。**Naive UI 只提供底层行为，业务页面不得大面积直接依赖其组件细节或默认视觉。
8. **迁移期间保持现有 API 路径和业务语义。**禁止为了前端方便顺带修改数据库模型、任务状态机或 Provider 执行策略。
9. **OpenAPI 补齐后再进行自动类型生成。**当前规范缺少真实路由，不能把现有 `/openapi.json` 当作完整真源。
10. **每个阶段必须可独立构建、验证和回滚。**禁止一次提交整个前端重写。
11. **所有源文件使用 UTF-8 无 BOM 和 LF。**不得通过可能破坏中文编码的 PowerShell 拼接方式写入中文源文件。
12. **现有用户工作区修改优先保留。**重构任务不得覆盖当前未提交的 Provider、OpenList、Storage Setup 和测试改动。
13. **页面 UI 必须建立可定制合同。**内置 Light、Dark、System、High Contrast 参考主题，管理端与访客端允许分别选择 Theme 和 UI Pack；不得绑定单一品牌色或固定明暗模式。
14. **定制范围包含组件与布局。**Button、Input、Card、Table、Dialog、Navigation 等基础组件由 Component Recipe 控制，复杂业务组件由 Feature Renderer 控制，完整外观通过受版本控制的 UI Pack 组合；不得交付默认组件库 Demo 风格。
15. **视觉验收属于完成条件。**核心页面必须覆盖桌面、平板、手机和 Loading/Empty/Error/Unavailable 状态，并保存 Light、Dark、High Contrast 与主要 UI Pack 的稳定截图基线。

## 3. 为什么选择“重做前端、保留后端”

当前前端的主要问题不是视觉效果，而是工程结构：

- `static/admin.js` 超过 2000 行，承担大量跨模块协调；
- `static/public-submit.js` 超过 2000 行，同时负责资源选择、目录展开、提交、确认和通知；
- `static/product.css` 超过 10000 行，页面、组件、状态和响应式样式混在同一文件；
- 多个脚本通过 `window.Fnos*`、DOM ID 和字符串 HTML 隐式耦合；
- 请求、加载状态、错误处理、弹窗、分页、轮询和表单逻辑在不同页面重复实现；
- 旧模板和脚本已经具备较完整功能，继续在现有模式上新增功能会继续放大维护成本。

后端虽然也存在大文件和组合根过重的问题，但已经拥有：

- 完整的 Provider 账号、OpenList、存储目标和入库规则模型；
- V1 不可变执行计划、幂等、持久队列和 Worker；
- rclone、Provider relay、Organizer、更新、通知和热榜业务闭环；
- SQLite v29 迁移和大量回归测试；
- 管理端、访客端和内部 Worker API。

因此本轮最佳收益路径是：

```text
保留后端业务真源
  -> 修复阻断接口
  -> 补齐 OpenAPI 与前端适配层
  -> 建立 Vue 组件体系
  -> 按功能逐页迁移
  -> 切换生产入口
  -> 删除旧前端
```

## 4. 文档索引

| 文档 | 用途 | AI 何时阅读 |
|---|---|---|
| [01-current-state-audit.md](./01-current-state-audit.md) | 当前页面、脚本、样式、接口与风险清单 | 所有任务开始前 |
| [02-target-architecture.md](./02-target-architecture.md) | 技术栈、目录、依赖方向、路由、状态和编码规范 | 创建工程或新 Feature 前 |
| [03-design-system-and-components.md](./03-design-system-and-components.md) | 设计令牌、基础组件、业务组件和组件治理 | 编写任何 UI 前 |
| [04-api-contract-and-backend-boundary.md](./04-api-contract-and-backend-boundary.md) | API Client、认证、CSRF、错误、OpenAPI 与后端改动边界 | 接入接口前 |
| [05-migration-roadmap.md](./05-migration-roadmap.md) | 分阶段迁移顺序、输入、输出、验收和回滚 | 领取阶段任务前 |
| [06-testing-and-quality-gates.md](./06-testing-and-quality-gates.md) | 测试分层、CI、可访问性、性能和完成定义 | 实现和验收时 |
| [07-build-deploy-and-rollback.md](./07-build-deploy-and-rollback.md) | Vite、Docker、Flask SPA、缓存、发布和回滚 | 集成构建或发布时 |
| [08-ai-execution-playbook.md](./08-ai-execution-playbook.md) | AI 工作流、禁止事项、提示词模板和交付格式 | 每次 AI 执行时 |
| [09-task-board.md](./09-task-board.md) | 可领取任务、依赖和逐项验收 | 规划具体开发批次时 |
| [10-custom-ui-visual-spec.md](./10-custom-ui-visual-spec.md) | 页面与组件呈现、响应式、Naive UI 边界和视觉验收 | 设计或实现任何页面前 |
| [11-theme-engine-and-ui-customization.md](./11-theme-engine-and-ui-customization.md) | Theme、Component Recipe、Layout Recipe、Renderer 与 UI Pack 架构真源 | 实现主题、组件、布局或外观配置前 |

## 5. 目标产品范围

### 5.1 管理端

新前端必须覆盖现有后台全部能力：

- 登录、退出、Session、CSRF和默认密码提醒；
- 总览、系统健康、依赖状态和关键统计；
- 访客请求列表、详情、批准、拒绝和取消；
- 入库任务列表、详情、事件时间线、重试、取消、删除和批量重试；
- Provider 账号、登录流程、验证码、检测、激活、禁用和删除；
- OpenList 连接、挂载、目录浏览、检测和身份确认；
- 保存位置、原生目录树、绑定、入库规则和完整预检；
- Organizer 任务、映射编辑、批量修改、审批、执行、跳过、重试和回滚；
- rclone 状态、运行记录、文件事件、日志、启动、停止和重试；
- 媒体库刷新；
- 追更订阅、运行、候选和调度；
- 每日热榜候选、搜索、入库、订阅、忽略和恢复；
- 通知渠道、事件规则、投递记录和重试；
- 高级配置、搜索源、别名、TMDB、AI、代理、资料和历史清理。

### 5.2 访客端

新前端必须覆盖：

- 资源搜索和搜索源切换；
- 搜索结果详情与文件选择；
- 手动链接检测、预览和提交；
- Quark、移动云、天翼和六盘对应选择流程；
- 分类确认、验证码、邮件进度通知；
- 每日热播；
- 提交编号查询和最近提交；
- 独立状态页 `/request/:token`；
- 移动端布局、键盘操作和无障碍提示。

### 5.3 明确不做

本轮不包含：

- 重写 Flask 后端；
- 更换 SQLite；
- 拆分微服务；
- 更换 Provider、Planner、Transfer 或 Organizer 业务语义；
- 同时引入 React；
- 建设通用低代码页面生成器；
- 为尚未出现的业务预先开发大型抽象；
- 将管理端和访客端拆成两个独立仓库或两个生产 Node 服务；
- 在新前端中兼容旧浏览器或 IE。

## 6. 实施阶段总览

| 阶段 | 名称 | 主要结果 |
|---|---|---|
| 0 | 稳定基线与 API 审计 | 修复公共配置 500、补接口清单、锁定契约 |
| 1 | 前端工程与基础设施 | `frontend/`、构建、路由、API Client、测试框架 |
| 2 | 主题引擎、设计系统与管理壳 | Theme Runtime、Component Recipe、UI Pack、登录、布局、导航和视觉回归基线 |
| 3 | Storage Setup 试点 | Provider/OpenList/保存位置/目录树/预检完整迁移 |
| 4 | 总览、请求与任务 | Dashboard、Requests、Jobs、时间线、日志 |
| 5 | Organizer、rclone 与媒体 | 高复杂管理工作区迁移 |
| 6 | 更新、热榜、通知与设置 | 剩余后台功能迁移 |
| 7 | 访客端 | 搜索、预览、提交、热榜、查询和状态页 |
| 8 | 生产切换 | Flask SPA 承载、Docker 构建、观测、回滚演练 |
| 9 | 旧前端退役 | 删除旧模板/脚本/样式和临时兼容入口 |

任何 AI 不得跳过阶段 0、1、2 直接开始批量页面重写。Storage Setup 是验证架构是否可行的首个完整业务切片。

## 7. 全局完成标准

只有同时满足以下条件，前端重构才能标记完成：

1. 管理端和访客端现有核心功能全部迁移并通过验收。
2. 生产入口不再加载 `static/admin*.js`、`static/public*.js`、`static/ui-*.js`。
3. 业务页面不直接调用 `fetch`，全部通过 Feature API 模块和统一 API Client。
4. 不再向 `window` 注册业务模块或共享状态。
5. TypeScript 开启 `strict`，业务代码不存在未说明的 `any`。
6. 所有异步页面具有加载、空态、错误态和重试入口。
7. 管理端写操作正确携带 CSRF Token，401/403 行为明确。
8. OpenAPI 覆盖全部对外和管理 API，至少不再缺少 Provider、OpenList 和 Storage V1 路由。
9. 前端单元、组件和端到端测试通过。
10. 后端全量 pytest、Ruff、Compose 校验继续通过。
11. Docker amd64/arm64 构建成功，Web 和 Worker 镜像启动正常。
12. `/health`、`/readyz`、登录、存储预检、任务创建、任务详情和访客提交完成冒烟。
13. 已演练前端版本回滚，不依赖数据库回滚。
14. 旧前端文件删除后不存在生产路径引用和死代码。
15. 核心页面不呈现 Naive UI 默认 Demo 风格，并通过定制 UI 人工检查清单。
16. Light、Dark、System、High Contrast Theme 可运行时切换，切换时不刷新页面、不丢表单、筛选、Drawer 和 Query 状态。
17. Button、Input、Select、Card、Table、Dialog、Navigation 和 PageState 均通过稳定 Component Recipe 获取外形，不在业务页面硬编码主题分支。
18. Provider Account Card、Job Timeline、Organizer Mapping、Resource Card、Resource File Tree 和 Request Status Track 具备稳定 Renderer Contract；新增 UI Pack 不复制业务 Feature。
19. 管理端和访客端可分别选择 Theme/UI Pack，并具备安全的预览、发布、回滚与未安装 Pack fallback。
20. Dashboard、Storage、Job Detail、Organizer、rclone、Public Search 和 Request Status 已生成多断点、多主题视觉基线。

## 8. AI 执行的最短入口

当用户要求 AI 开始开发时，使用以下流程：

1. 阅读本文件。
2. 阅读 [08-ai-execution-playbook.md](./08-ai-execution-playbook.md)。
3. 在 [09-task-board.md](./09-task-board.md) 中选择一个依赖已满足的任务。
4. 阅读该任务涉及的架构、组件、API和迁移文档；只要涉及页面、组件、主题或布局，同时阅读 [10-custom-ui-visual-spec.md](./10-custom-ui-visual-spec.md) 与 [11-theme-engine-and-ui-customization.md](./11-theme-engine-and-ui-customization.md)。
5. 检查 Git 状态并保护用户未提交改动。
6. 使用 `update_plan` 给出一次性执行计划。
7. 完成代码、测试、构建、文档和验证，不在小步骤后要求用户确认。
8. 只有当前任务全部满足验收标准后，才能更新任务状态或进入下一任务。

## 9. 关键风险提示

- 当前工作区并非干净状态，存在 Provider、OpenList、Storage Setup、前端和测试改动；AI 必须先区分已有改动与本次改动。
- 审计时运行实例在六盘账号未激活时访问 `/api/public/config` 会返回 500；当前工作区已出现未提交修复代码，但仍需按阶段 0 完成定向与接口级验证后才能视为关闭。
- 实际 Flask 应用约有 160 条路径、193 个 HTTP 操作，而当前 OpenAPI 只记录 117 条路径、136 个操作，至少缺少 57 个操作。
- 旧前端虽然结构混乱，但已包含大量边界处理和无障碍细节。迁移必须以行为等价为前提，不能只复制视觉截图。
- Provider 登录、验证码、目录树、任务轮询和 Organizer 是高状态复杂度模块，不能用一个巨型 Pinia Store 重新实现旧问题。
- 主题切换不能退化为复制 Light/Dark 页面或全局 CSS 覆盖；Theme、Component Recipe、Layout Recipe、Feature Renderer 和 UI Pack 必须保持职责分离。
- 生产部署为 Web + Worker 双容器同镜像。前端构建改造不能破坏 Worker 入口、非 root 用户、rclone 二进制和多架构构建。
