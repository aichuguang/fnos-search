# 09｜AI 重构任务板

## 1. 使用规则

- 状态：`未开始`、`进行中`、`完成`、`阻塞`。
- AI 一次只领取一个依赖已满足的任务，或用户明确要求的完整阶段。
- 完成任务必须满足验收并记录测试，不得只修改状态文字。
- 任务编号是稳定引用，后续即使调整顺序也不改编号。
- 所有任务遵循 [08-ai-execution-playbook.md](./08-ai-execution-playbook.md)。
- 所有 UI/Page 任务同时遵循 [10-custom-ui-visual-spec.md](./10-custom-ui-visual-spec.md) 与 [11-theme-engine-and-ui-customization.md](./11-theme-engine-and-ui-customization.md)，视觉截图、响应式、页面状态、Theme/Recipe/Renderer Contract 属于任务验收，不是可选美化项。

## 2. 里程碑

| 里程碑 | 完成条件 |
|---|---|
| M0 API Ready | BASE-001、API-001～API-007 完成，OpenAPI 与路由一致 |
| M1 Foundation Ready | FE-100～FE-173、UI-100～UI-110 完成 |
| M2 Storage Ready | FE-200 系列完成 |
| M3 Core Admin Ready | FE-300 系列完成 |
| M4 Advanced Admin Ready | FE-400/500 系列与 API-008 完成 |
| M5 Public Ready | FE-600 系列完成 |
| M6 Production Cutover | BUILD-700 系列完成 |
| M7 Legacy Removed | CLEAN-800 系列完成 |

---

## 3. 阶段 0：基线与 API

### BASE-001｜记录并保护当前工作区基线

- 状态：未开始
- 优先级：P0
- 依赖：无
- 目标：区分当前已有改动与前端重构改动。
- 工作：
  - 记录 `git status --short --branch`；
  - 记录当前修改文件和未跟踪文件；
  - 运行 scoped `git diff --check`；
  - 不提交用户未要求提交的现有改动；
  - 在后续任务报告中持续标注基线外改动。
- 验收：工作区无文件被意外回退或整体格式化。

### API-001｜修复公共配置可选 Provider 500

- 状态：进行中（工作区已有未提交实现，待验证收口）
- 优先级：P0
- 依赖：BASE-001
- 目标：SixPan 等未激活时 `/api/public/config` 返回正常不可用态。
- 工作：
  - 修复动态代理 `hasattr/__getattr__` 触发 resolve 的问题；
  - 统一未配置 Provider 描述；
  - 补充接口回归；
  - 验证其他 Provider 正常。
- 验收：四 Provider 任意缺失组合均返回 200；无 500。
- 测试：相关 public capability、runtime、public config 测试。

### API-002｜建立 url_map 与 OpenAPI 对齐测试

- 状态：未开始
- 优先级：P0
- 依赖：BASE-001
- 目标：以后新增路由不能忘记 OpenAPI。
- 工作：
  - 临时配置构造 Flask app；
  - 规范化 Flask 参数路径；
  - 排除 static 和内部白名单；
  - 比较 path+method；
  - 输出易读缺失列表。
- 验收：测试能复现当前 57 个缺失操作，补齐后为 0。

### API-003｜补 Provider Accounts/Auth Sessions OpenAPI

- 状态：未开始
- 优先级：P0
- 依赖：API-002
- 范围：Provider Catalog、Accounts CRUD/Test/Activate/Disable、Auth Sessions。
- 验收：请求、成功和主要错误 Schema 完整；对齐测试对应缺失为 0。

### API-004｜补 OpenList OpenAPI

- 状态：未开始
- 优先级：P0
- 依赖：API-002
- 范围：Connections、Mounts、Test、Confirm Identity、Dirs。
- 验收：所有真实路径和方法有规范；Secret 字段说明明确。

### API-005｜补 Storage OpenAPI

- 状态：未开始
- 优先级：P0
- 依赖：API-002
- 范围：Storage Setup、Accounts Folder、Locations、Routes、Preflight、Targets、Bindings、Assignments、Matrix。
- 验收：Storage 首批前端可以从规范获得完整类型。

### API-006｜补剩余方法和标准错误 Schema

- 状态：未开始
- 优先级：P1
- 依赖：API-003、API-004、API-005
- 范围：Notifications PUT、Settings All、页面/系统公开说明、标准错误。
- 验收：对外/管理 API 对齐测试通过，缺失操作为 0。

### API-007｜生成脱敏前端 Fixture

- 状态：未开始
- 优先级：P1
- 依赖：API-003、API-004、API-005
- 目标：为 MSW 和组件测试提供真实结构。
- 工作：为 Session、Catalog、Accounts、Storage Setup、Jobs、Public Config 等准备脱敏 JSON。
- 验收：Fixture 无 Secret、Token、真实 URL、账号或访问载荷。

---

## 4. 阶段 1：工程基础

### FE-100｜创建 Vue 3 + TypeScript + Vite 工程

- 状态：未开始
- 优先级：P0
- 依赖：BASE-001
- 目标：建立 `frontend/` 和锁文件。
- 验收：`npm ci`、`npm run dev`、`npm run build` 成功。

### FE-110｜配置 TypeScript、ESLint 和路径别名

- 状态：未开始
- 优先级：P0
- 依赖：FE-100
- 验收：strict 规则开启；`@/` 可用；无隐式 any。

### FE-120｜配置 Vitest、Testing Library 和 MSW

- 状态：未开始
- 优先级：P0
- 依赖：FE-100
- 验收：示例组件测试和 MSW 请求测试通过。

### FE-130｜配置 Playwright

- 状态：未开始
- 优先级：P1
- 依赖：FE-100
- 验收：可以启动前端预览并执行一个 Smoke Test。

### FE-140｜实现统一 API Client 与 ApiError

- 状态：未开始
- 优先级：P0
- 依赖：FE-110、FE-120
- 范围：JSON、FormData、204、错误、Trace ID、Abort、Timeout、CSRF Hook。
- 验收：401/403/409/429/500、非 JSON 错误测试通过；无直接 endpoint 业务逻辑。

### FE-150｜配置 Pinia、Vue Query 和 Router

- 状态：未开始
- 优先级：P0
- 依赖：FE-110
- 验收：Providers 统一安装；Route 懒加载；Query Devtools 仅开发环境。

### FE-160｜建立 Component Gallery 与基础样式入口

- 状态：未开始
- 优先级：P1
- 依赖：FE-150
- 验收：开发路由可展示 Token、Theme/UI Pack 切换器和组件占位，生产默认不可见。

---

## 5. 阶段 2：主题引擎、设计系统与应用壳

### UI-100｜建立 UI Contract、Registry 与首屏 Bootstrap

- 状态：未开始
- 优先级：P0
- 依赖：FE-160
- 工作：
  - 定义 `ThemeDefinition`、`ThemeTokenSet`、`ComponentRecipeSet`、`LayoutRecipeSet`、`UiPackDefinition` 和 `FeatureRendererMap`；
  - 建立 Theme、UI Pack、Renderer Registry；
  - 实现用户偏好、Admin/Public 站点配置、System Scheme、Preview Override 的解析优先级；
  - 在 Vue 挂载前输出首屏 Theme，避免闪烁；
  - 实现未知 Theme/UI Pack fallback 和 UI Contract Version 检查。
- 验收：Registry/Resolve/Bootstrap 单测通过；不得执行任意 CSS、HTML、JavaScript 或 Vue 代码；Feature 不读取具体 Theme/UI Pack ID。

### UI-101｜实现语义 Token、参考 Theme 与动态 Naive Overrides

- 状态：未开始
- 优先级：P0
- 依赖：UI-100
- 工作：实现稳定语义 Token、Light/Dark/System/High Contrast 参考 Theme、Reduced Motion、Focus Ring，以及根据 Theme + Component Recipe 动态生成 Naive UI Overrides。
- 验收：颜色、字体、间距、圆角和阴影无页面硬编码扩散；Overlay/Dropdown/Dialog 同步换肤；High Contrast 对比度校验通过；Naive UI 默认绿色和默认 Demo 外形不可见。

### UI-107｜实现 Component Recipe Registry

- 状态：未开始
- 优先级：P0
- 依赖：UI-101
- 工作：建立 Button、Input、Select、Card、Table、Dialog、Drawer、Toast、Navigation、PageState Recipe 与 Component Token 输出；至少提供 `standard`、`compact` 两套组合。
- 验收：Recipe 只能改变结构允许范围内的几何和视觉值，不能改变 Props、Events、Slots、表单值、键盘行为或 Focus Trap；切换 Recipe 无 Reload。

### UI-102｜实现表单基础组件

- 状态：未开始
- 优先级：P0
- 依赖：UI-107
- 范围：Button、FormField、Input、Textarea、Select、Switch、Checkbox。
- 验收：状态、ARIA、组件测试和 Gallery 完整；`standard`/`compact` 下 Contract 相同、外形差异可见。

### UI-103｜实现反馈与容器组件

- 状态：未开始
- 优先级：P0
- 依赖：UI-107
- 范围：Card、Dialog、Drawer、Confirm、Toast、Loading/Empty/Error。
- 验收：焦点、ESC、异步确认、错误区测试通过；Recipe 切换不关闭已打开 Overlay 或丢失焦点。

### UI-104｜实现表格、分页和状态组件

- 状态：未开始
- 优先级：P1
- 依赖：UI-102、UI-103、UI-107
- 范围：DataTable Wrapper、Pagination、StatusBadge、AsyncActionButton。
- 验收：服务端分页和移动卡片模式可用；Table Density 与状态语义由 Recipe/Theme 驱动。

### UI-105｜实现 Layout Recipe 与定制工作区组件

- 状态：未开始
- 优先级：P0
- 依赖：UI-102、UI-103、UI-104
- 范围：AdminAppShell、AdminSidebar、AdminTopbar、PageHeader、PageToolbar、FilterBar、SplitWorkspace、DetailInspector、PublicAppShell、PublicHeader、PublicContent。
- 验收：实现 Admin/Public/Workspace Layout Recipe；管理端与访客端可使用不同布局和 Theme/UI Pack；Sidebar、Page Header、工作区和 Public Header 均为项目或 UI Pack 定制外形；1024/768/390/320 重排可用；不使用默认 Naive UI Layout 拼装交付。

### UI-108｜建立 Feature Renderer Registry 与 Standard UI Pack

- 状态：未开始
- 优先级：P0
- 依赖：UI-105、UI-107
- 工作：
  - 建立 Provider Card、Storage Step Rail、Job Stage/Timeline、Organizer Mapping、Log Viewer、Resource Card/File Tree、Request Status Track 的 Renderer Contract；
  - 实现 Standard UI Pack 注册、缺失 Renderer 检查和懒加载 fallback；
  - 为每个 Renderer 建立共享 Contract Test Harness。
- 验收：UI Pack 只替换呈现，不复制 Feature API、Query、Mutation、权限或状态机；缺失 Renderer 时阻止发布或安全回退。

### UI-109｜实现 Theme/UI Pack Switcher 与偏好隔离

- 状态：未开始
- 优先级：P0
- 依赖：UI-100、UI-108
- 工作：实现 Theme、UI Pack、Density 切换；Admin/Public 独立选择；Preview Override；刷新恢复；System Scheme 监听；切换时保持页面状态。
- 验收：切换无 Reload、无 FOUC，不清表单、筛选、Drawer、路由和 Vue Query Cache；管理员预览不影响正式访客端；未知 ID 自动 fallback。

### UI-106｜建立视觉回归与页面状态 Gallery

- 状态：未开始
- 优先级：P0
- 依赖：FE-130、UI-109
- 工作：配置固定 Fixture、固定时间/时区、禁用非必要动画、Playwright Screenshot、视觉基线目录和人工检查模板。
- 范围：登录、Admin Shell、Dashboard、Storage、Job Detail、Organizer、rclone、Public Search、Resource Detail、Request Status 的占位/稳定状态。
- 验收：1440、1024、768、390、320 五档可生成稳定截图；Reference Light/Dark 覆盖全部核心页面，High Contrast 覆盖核心组件和关键流程，每个 UI Pack 覆盖主 Theme；Loading/Empty/Error/Unavailable 至少各有一个页面样例；CI 或本地命令可复现。

### UI-110｜建立 Theme/UI Pack Contract 与视觉矩阵门禁

- 状态：未开始
- 优先级：P0
- 依赖：UI-106、UI-108
- 工作：Theme Schema、对比度、Component Recipe Contract、Renderer Contract、UI Contract Version、未安装 Pack fallback、Theme/Pack 视觉覆盖矩阵和 axe 门禁。
- 验收：Light/Dark/High Contrast、`standard`/`compact`、Admin/Public 关键组合通过；不执行无意义的 Theme × UI Pack 全笛卡尔积；测试能阻止缺 Token、缺 Renderer、ARIA 丢失和低对比度发布。

### FE-170｜实现 Session Store 与 Auth API

- 状态：未开始
- 优先级：P0
- 依赖：FE-140、FE-150
- 验收：Session 初始化、CSRF 内存管理、清理 Query Cache 测试通过。

### FE-171｜迁移管理员登录页

- 状态：未开始
- 优先级：P0
- 依赖：UI-102、UI-103、UI-105、UI-109、FE-170
- 验收：登录、错误、Loading、键盘、移动端和 E2E 通过。

### FE-172｜实现管理端 Layout、导航和路由守卫

- 状态：未开始
- 优先级：P0
- 依赖：FE-171、UI-105、UI-109
- 验收：未登录跳转、原目标返回、Sidebar/Topbar、移动导航可用；当前路由、折叠态、Drawer、Page Header 视觉符合定制规范；Theme/UI Pack 切换不影响路由状态；1440/768/390/320 截图通过。

### FE-173｜实现 Profile 摘要、退出和默认密码提醒

- 状态：未开始
- 优先级：P1
- 依赖：FE-172
- 验收：头像/Logo fallback、退出清理和安全提示正确。

---

## 6. 阶段 3：Storage

### UI-201｜实现 ProviderIcon、ProviderBadge、CapabilityBadge

- 状态：未开始
- 优先级：P0
- 依赖：UI-104、UI-108
- 验收：已知/未知 Provider 均稳定，图标和能力状态有测试；Provider Account Card 通过 Renderer Registry 注册，不在 Storage Feature 中判断 UI Pack ID。

### UI-202｜实现通用 FolderTree

- 状态：未开始
- 优先级：P0
- 依赖：UI-102、UI-103
- 验收：懒加载、ID/Path、错误重试、键盘、Abort、空态测试通过。

### FE-200｜建立 Storage Feature API/Types/Adapters

- 状态：未开始
- 优先级：P0
- 依赖：FE-140、API-003、API-004、API-005
- 验收：Catalog、Accounts、Auth、OpenList、Locations、Routes、Preflight 都有集中 API。

### FE-201｜迁移 Provider Accounts 页面

- 状态：未开始
- 优先级：P0
- 依赖：FE-200、UI-201、FE-172
- 验收：对象卡、步骤轨道、上下文编辑器、创建、编辑、Test、Activate、Disable、Delete 完整；状态不依赖大量彩色 Badge；1280/768/390 截图通过。

### FE-202｜迁移 Provider Auth Session

- 状态：未开始
- 优先级：P0
- 依赖：FE-201
- 验收：表单、验证码、轮询、换图、取消、过期、成功刷新；Fake Timer 测试通过。

### FE-203｜迁移 OpenList Connections/Mounts

- 状态：未开始
- 优先级：P0
- 依赖：FE-200、UI-202
- 验收：CRUD、Test、Identity、Dirs 完整；Secret 不回显。

### FE-204｜迁移 Storage Locations 和原生目录树

- 状态：未开始
- 优先级：P0
- 依赖：FE-201、FE-203、UI-202
- 验收：Mobile/Quark/Tianyi 根和定位器模式正确；创建/编辑/删除完整；目录失败就地重试；已选路径/定位器清楚；320px 可完成选择。

### FE-205｜迁移 Source Assignments/Routing Matrix

- 状态：未开始
- 优先级：P1
- 依赖：FE-204
- 验收：默认目标、分类覆盖和不可执行原因完整。

### FE-206｜迁移完整预检

- 状态：未开始
- 优先级：P0
- 依赖：FE-205
- 验收：分项结果、耗时、Trace ID、修复入口、重试、部分失败和成功状态完整；默认、部分失败和全失败进入视觉回归。

### FE-207｜Storage Feature E2E 与真实只读冒烟

- 状态：未开始
- 优先级：P0
- 依赖：FE-202、FE-203、FE-204、FE-205、FE-206
- 验收：Mock 全流程 E2E；非生产环境目录浏览；无危险真实写入或明确记录；1280 三栏、768 下沉编辑器、390/320 全屏流程视觉检查通过。

---

## 7. 阶段 4：核心后台

### FE-300｜Dashboard Feature

- 状态：未开始
- 优先级：P1
- 依赖：FE-172、UI-104
- 验收：可操作摘要、最近任务、当前 Pipeline、系统链路健康、最近动态、局部错误和自动刷新完整；不虚构健康分/趋势；默认、异常和局部刷新失败多断点截图通过。

### FE-310｜Requests API、列表和筛选

- 状态：未开始
- 优先级：P0
- 依赖：FE-172、UI-104
- 验收：分页/筛选 URL 同步，空态/错误态完整。

### FE-311｜Request Detail 与操作

- 状态：未开始
- 优先级：P0
- 依赖：FE-310
- 验收：Approve/Reject/Cancel、防重复、确认和错误处理。

### FE-320｜Jobs API、列表和筛选

- 状态：未开始
- 优先级：P0
- 依赖：FE-172、UI-104
- 验收：分页、状态/Provider/分类筛选和 URL 恢复。

### UI-301｜JobStatusBadge、JobStageTrack 与 JobTimeline

- 状态：未开始
- 优先级：P0
- 依赖：UI-104、UI-108
- 范围：Job/Transfer/Worker/rclone/Organizer 状态映射、阶段轨道和事件时间线。
- 验收：状态映射集中并测试；完成/当前/等待/失败/跳过结构清楚；未知进度不伪造百分比；键盘和移动单列可用；Stage/Timeline 实现共享 Renderer Contract Test，可由 UI Pack 替换呈现但不改变任务语义。

### FE-321｜Job Detail

- 状态：未开始
- 优先级：P0
- 依赖：FE-320、UI-301
- 验收：详情路由、定制阶段轨道、计划摘要、Timeline、错误恢复、关联执行信息完整；不以默认 `NSteps + NDescriptions` 交付；运行/失败/取消状态和 390px 截图通过。

### FE-322｜Job Mutations

- 状态：未开始
- 优先级：P0
- 依赖：FE-321
- 验收：Retry/Cancel/Delete/Batch Retry，无自动重试，Query 正确失效。

### FE-323｜Requests/Jobs E2E

- 状态：未开始
- 优先级：P0
- 依赖：FE-311、FE-322
- 验收：批准、拒绝、重试、取消和详情刷新流程通过；列表卡片降级、详情单列和关键 Overlay 视觉回归通过。

---

## 8. 阶段 5：高级后台

### UI-401｜LogViewer 和 DateRange Wrapper

- 状态：未开始
- 优先级：P1
- 依赖：UI-104、UI-108
- 验收：大日志、复制、搜索、级别、自动滚动暂停、截断提示、日期区间和移动端可用；日志为实体工具面，不使用透明玻璃或整行高饱和错误背景；LogViewer Renderer 保持数据、复制与键盘合同。

### FE-400｜Organizer Task List/Detail

- 状态：未开始
- 优先级：P0
- 依赖：FE-172、UI-104
- 验收：列表、详情、轮询、状态完整。

### FE-401｜Organizer Mapping Editor

- 状态：未开始
- 优先级：P0
- 依赖：FE-400
- 验收：映射工作区、当前项检查器、单条/批量编辑、Preview、脏状态、字段错误恢复完整；390px 转逐项审核卡。

### FE-402｜Organizer Actions/Runs/Rollback

- 状态：未开始
- 优先级：P0
- 依赖：FE-401
- 验收：Approve/Apply/Skip/Retry/Delete/Rollback 确认完整。

### FE-410｜rclone 状态、运行和日志

- 状态：未开始
- 优先级：P0
- 依赖：FE-172、UI-401
- 验收：Status、Start/Stop、Runs、Events、Files、Logs、Retry；服务状态条、运行列表和 Log Viewer 层级清楚；无双层横向滚动；运行/错误截图通过。

### FE-411｜rclone WebDAV Config

- 状态：未开始
- 优先级：P1
- 依赖：FE-410
- 验收：配置、测试、Secret 状态和错误完整。

### FE-420｜Media Feature

- 状态：未开始
- 优先级：P1
- 依赖：FE-172、UI-104
- 验收：Libraries、Running、Logs、Refresh 完整。

### FE-430｜高级后台 E2E

- 状态：未开始
- 优先级：P1
- 依赖：FE-402、FE-411、FE-420
- 验收：Organizer、rclone、Media 关键 Mock 流程通过；Organizer 默认/脏状态/批量预览及 rclone 运行/错误多断点视觉回归通过。

---

## 9. 阶段 6：设置与内容维护

### FE-500｜Updates Feature

- 状态：未开始
- 优先级：P1
- 依赖：FE-172、UI-104
- 验收：Subscriptions、Runs、Candidates、TMDB、Scheduler 全部迁移。

### FE-510｜Trending Admin Feature

- 状态：未开始
- 优先级：P1
- 依赖：FE-172、UI-104
- 验收：Status、Run、Candidates、Search、Import、Subscribe、Ignore/Restore。

### FE-520｜Notifications Feature

- 状态：未开始
- 优先级：P1
- 依赖：FE-172、UI-102、UI-104
- 验收：Channels、Rules、Deliveries、Test、Retry、Secret 状态；规则矩阵桌面列对齐，390px 转按事件分组列表。

### FE-530｜Profile/System Feature

- 状态：未开始
- 优先级：P1
- 依赖：FE-173
- 验收：Profile、Avatar、Logo、Security、History Cleanup。

### FE-540｜Search Settings/Advanced Config

- 状态：未开始
- 优先级：P1
- 依赖：FE-172、UI-102
- 验收：Providers、Aliases、TMDB、AI、Proxy、Advanced Config、Export/Import；按设置 Section 拆分、独立保存、脏状态和离开提醒完整；无数百字段单页堆叠。

### API-008｜建立站点 UI 配置、校验与回滚接口

- 状态：未开始
- 优先级：P1
- 依赖：API-002、UI-100
- 范围：`GET/PUT /api/admin/ui/config`、Theme 校验/预览、版本列表/回滚、`GET /api/public/ui-config` 安全投影。
- 验收：只持久化结构化 Token、Recipe 选择、已安装 UI Pack ID 和安全品牌资源；不接受任意 CSS/HTML/JavaScript；Public 响应不含管理员草稿、文件路径或敏感配置；OpenAPI 与回滚测试通过。

### FE-541｜外观设置、实时预览与 Theme/UI Pack 发布

- 状态：未开始
- 优先级：P1
- 依赖：FE-540、API-008、UI-109、UI-110
- 范围：Admin/Public Theme 与 UI Pack、Density、品牌资源、Token 编辑、Schema 校验、Desktop/Mobile 预览、导入/导出、发布历史和回滚。
- 验收：预览不污染正式配置；发布后 Public Config 生效；无效 Theme JSON 和未知 Pack 被拒绝；完整组件/UI Pack 只能选择仓库中已安装实现，不能上传运行时代码。

### FE-550｜设置与内容维护 E2E

- 状态：未开始
- 优先级：P2
- 依赖：FE-500、FE-510、FE-520、FE-530、FE-540、FE-541
- 验收：关键配置保存、Secret、文件上传下载和调度操作通过；Theme/UI Pack 预览、发布、Public 生效和回滚 E2E 通过；Settings、Appearance、Notifications、Trending Workbench 桌面/手机视觉基线通过。

---

## 10. 阶段 7：访客端

### FE-600｜Public Layout、路由与公共配置

- 状态：未开始
- 优先级：P0
- 依赖：API-001、FE-150、UI-103、UI-105、UI-108、UI-109
- 验收：Public Config、独立 Public Theme/UI Pack/Layout Recipe、Logo、顶部品牌导航、有限内容宽度、旧 Hash 兼容、Provider 不可用态；访客端不复制管理端 Sidebar/高密度工作台；未知 Pack fallback；390/320 截图通过。

### UI-601｜ResourceCard 与搜索公共组件

- 状态：未开始
- 优先级：P0
- 依赖：UI-201、UI-103、UI-108
- 验收：纵向内容卡、详情入口、来源/可用性状态、移动端和组件测试；Resource Card 通过 Renderer Registry 注册并运行共享 Contract Test；不使用默认 `NCard` 网格或无真实来源的装饰指标。

### UI-602｜ResourceFileTree

- 状态：未开始
- 优先级：P0
- 依赖：UI-202、UI-108
- 验收：文件/目录、Checkbox、懒加载、节点错误、Provider Adapter、已选数量/大小和“整份分享”语义测试；File Tree Renderer 可替换呈现但不改变选集语义；手机触控区域可用。

### FE-610｜Public Search

- 状态：未开始
- 优先级：P0
- 依赖：FE-600、UI-601、UI-602
- 验收：搜索源、关键词、结果、详情、文件选择和错误状态；搜索为首屏唯一主焦点；详情桌面 Drawer/路由与手机全屏可用；默认/Loading/Empty/Error 截图通过。

### FE-620｜Manual Detect/Preview

- 状态：未开始
- 优先级：P0
- 依赖：FE-600、UI-602
- 验收：URL、密码、Detect、Preview、标题、分类、备注完整；采用输入 → 检测预览 → 补充确认三步结构；检测失败保留用户输入。

### FE-621｜Public Confirm/Captcha/Submit

- 状态：未开始
- 优先级：P0
- 依赖：FE-610、FE-620
- 验收：分类确认、Captcha、Slow Confirm、通知、Payload 禁止字段和提交成功；确认页展示资源/来源/选集/分类/处理方式；成功页展示编号、复制和查看进度；手机 Bottom Sheet/全屏状态可用。

### FE-630｜Public Trending

- 状态：未开始
- 优先级：P1
- 依赖：FE-600、UI-601
- 验收：分类、榜单、可观看状态、搜索入口和空态。

### FE-640｜Request Query/Status

- 状态：未开始
- 优先级：P0
- 依赖：FE-600
- 验收：Token 查询、最近提交、详情路由、访客语言阶段轨道、轮询终止和复制；不公开内部路径、Token、Trace ID 或技术堆栈。

### FE-650｜Public E2E 与移动端验收

- 状态：未开始
- 优先级：P0
- 依赖：FE-621、FE-630、FE-640
- 验收：搜索提交、手动提交、Captcha、最近查询、390px/320px、axe 全部通过；Public Search、Resource Detail、Confirm、Success、Request Status 视觉回归通过。

---

## 11. 阶段 8：构建与切换

### BUILD-700｜Docker 前端构建 Stage

- 状态：未开始
- 优先级：P0
- 依赖：FE-100
- 验收：`frontend/dist` 进入当前应用镜像；现有 rclone/Python/用户步骤保持。

### BUILD-710｜Flask SPA 静态托管与 Fallback

- 状态：未开始
- 优先级：P0
- 依赖：BUILD-700
- 验收：深链刷新成功；`/api` 404 不返回 HTML；路径安全。

### BUILD-720｜缓存和版本信息

- 状态：未开始
- 优先级：P1
- 依赖：BUILD-710
- 验收：index no-cache、Hash Assets immutable、前端版本可诊断。

### BUILD-730｜CI 前端门禁

- 状态：未开始
- 优先级：P0
- 依赖：FE-120、BUILD-700
- 验收：npm ci/lint/typecheck/test/build 进入 CI；旧门禁保留；视觉回归命令和基线变更检查可执行。

### BUILD-740｜Compose 双容器和多架构验证

- 状态：未开始
- 优先级：P0
- 依赖：BUILD-710、BUILD-730
- 验收：Web/Worker Ready；amd64/arm64 构建；Worker 无 Node 运行依赖。

### BUILD-750｜生产候选冒烟和回滚演练

- 状态：未开始
- 优先级：P0
- 依赖：FE-207、FE-323、FE-430、FE-550、FE-650、BUILD-740
- 验收：管理/访客/健康/Storage/Jobs 冒烟；核心页面生产构建截图与基线无非预期差异；旧镜像回滚成功。

---

## 12. 阶段 9：旧前端清理

### CLEAN-800｜切换正式页面入口

- 状态：未开始
- 优先级：P0
- 依赖：BUILD-750
- 验收：正式 `/admin`、`/submit`、`/request` 使用 Vue；无旧脚本加载。

### CLEAN-810｜删除旧模板和脚本

- 状态：未开始
- 优先级：P1
- 依赖：CLEAN-800、稳定观察窗口完成
- 验收：无生产引用；旧功能测试已转移；Swagger/邮件页所需文件保留。

### CLEAN-820｜拆除旧 CSS 和静态测试

- 状态：未开始
- 优先级：P1
- 依赖：CLEAN-810
- 验收：删除无引用 CSS；无大面积视觉回归；旧字符串测试退役。

### CLEAN-830｜文档、README 和开发流程更新

- 状态：未开始
- 优先级：P1
- 依赖：CLEAN-820
- 验收：部署、开发、测试、目录和截图均指向新前端。

### CLEAN-840｜最终全量门禁

- 状态：未开始
- 优先级：P0
- 依赖：CLEAN-830
- 验收：Python/Frontend/E2E/Visual/Docker/Compose/UTF-8/Diff 全部通过；核心页面通过 `10-custom-ui-visual-spec.md` 人工检查清单；Theme/Recipe/Renderer/UI Pack 完成定义通过，任务板关闭。

---

## 13. 完成记录模板

在具体任务下追加：

```text
完成日期：YYYY-MM-DD
主要文件：
验证：
视觉截图/人工检查：
未验证：
风险/备注：
```
