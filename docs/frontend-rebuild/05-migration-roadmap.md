# 05｜分阶段迁移路线图

## 1. 总体原则

所有包含页面或组件的阶段同时受 [10-custom-ui-visual-spec.md](./10-custom-ui-visual-spec.md) 约束。功能等价、工程重构和定制视觉必须在同一个阶段收口，不允许先交付默认组件库页面，再把“美化”无限延期。

1. 先稳定契约，再写页面。
2. 先完成基础组件和一个完整业务切片，再批量迁移。
3. 结构重构和业务改造分开提交。
4. 每阶段保留旧前端可运行，直到新页面达到功能等价。
5. 每阶段必须完成测试、构建、冒烟和文档，不以“页面能打开”作为完成。
6. 不允许 AI 同时开启多个未完成阶段。
7. 新页面不得调用旧全局脚本，不把旧脚本当临时依赖长期保留。

## 2. 迁移策略

### 2.1 开发期并行入口

迁移期间建议提供 Vue 预览入口：

```text
/next/admin/...
/next/submit/...
```

或者仅通过 Vite 开发服务器访问。若需要在 NAS 上持续验收，则实现 `/next/*` 静态托管。

要求：

- `/api/*` 始终使用同一后端；
- Preview 默认不替换现有生产入口；
- Preview 是否开放由环境变量控制；
- Preview 不得关闭认证或 CSRF；
- 最终切换时将 Router Base 改为正式路径，并保留旧镜像回滚。

### 2.2 旧前端删除时机

只有满足以下条件才删除对应旧代码：

- 新 Feature 功能等价；
- 新 Feature E2E 通过；
- 生产或类生产环境冒烟通过；
- 对应入口已经切换；
- 没有模板继续引用旧脚本；
- 回滚依赖旧镜像而不是当前镜像内双实现。

## 3. 阶段 0：稳定基线与 API 契约

### 3.1 目标

在开始 Vue 开发前，建立可信基线，避免把后端错误误判为新前端问题。

### 3.2 输入

- 当前未提交 Provider/OpenList/Storage Setup 改动；
- 现有 `verification.md`；
- 现有全量后端测试；
- 现有运行实例。

### 3.3 任务

1. 检查并保护当前工作区改动。
2. 修复 `/api/public/config` 可选 SixPan 未激活导致 500：
   - 禁止 `hasattr()` 触发动态 Provider resolve；
   - 未配置 Provider 返回 `configured=false`、`enabled=false`；
   - 增加公共配置接口回归。
3. 建立实际路由与 OpenAPI 对齐测试。
4. 补齐 Provider、Auth Session、OpenList、Storage 和遗漏方法的 OpenAPI。
5. 导出一份脱敏 API Fixture，用于前端首批 Mock。
6. 记录所有分页参数、状态枚举和错误格式。
7. 执行全量 pytest、Ruff、JS 语法和 Compose 校验。

### 3.4 后端允许改动

- `public_web.py`；
- OpenAPI 定义；
- 相关 Blueprint 契约测试；
- 不涉及数据库迁移的错误兼容。

### 3.5 验收

- `/api/public/config` 在四种账号未配置组合下均返回 200；
- OpenAPI 与实际对外路由无缺失；
- 后端全量测试通过；
- 当前业务功能无变化；
- 文档中记录接口基线版本。

### 3.6 回滚

纯接口修复，不涉及 Schema；通过 Git 回退代码即可。

## 4. 阶段 1：Vue 工程骨架

### 4.1 目标

建立可构建、可测试、可代理真实后端的 `frontend/` 工程，不急于批量写业务页面。

### 4.2 任务

1. 创建 Vue 3 + TypeScript + Vite 工程。
2. 配置：
   - TypeScript strict；
   - ESLint；
   - Vitest；
   - Vue Testing Library；
   - MSW；
   - Playwright；
   - 路径别名 `@/`。
3. 创建 App Providers：
   - Vue Query；
   - Pinia；
   - Router；
   - UI Runtime / Theme Bootstrap；
   - Naive UI Config Provider；
   - 全局 Error Boundary。
4. 实现统一 `ApiClient` 和 `ApiError`。
5. 实现 Session Store 和路由守卫骨架。
6. 实现基础 Layout 占位页。
7. 实现 Vite `/api` 代理。
8. 创建 Component Gallery 开发页。
9. 建立 Theme、Component Recipe、Layout Recipe、Renderer 和 UI Pack 的 TypeScript Contract 空壳与 Registry 接口，不在此阶段实现具体业务皮肤。
10. 增加 npm Scripts：

```json
{
  "dev": "vite",
  "build": "vue-tsc -b && vite build",
  "typecheck": "vue-tsc --noEmit",
  "lint": "eslint .",
  "test": "vitest",
  "test:run": "vitest run",
  "test:e2e": "playwright test"
}
```

### 4.3 产出

- `frontend/`；
- 空管理路由和访客路由；
- API Client；
- UI Runtime、Registry 和首屏 Theme Bootstrap 骨架；
- 测试基础设施；
- 开发说明。

### 4.4 验收

- `npm ci` 成功；
- `npm run lint` 通过；
- `npm run typecheck` 通过；
- `npm run test:run` 通过；
- `npm run build` 通过；
- Vite 可以通过代理访问 `/health` 和 `/api/admin/session`；
- 未关闭后端 CSRF；
- 没有业务页面直接 `fetch`；
- 首屏 Bootstrap 能在 Vue 挂载前解析默认 Theme，且不会写入任意 CSS/HTML/JavaScript。

## 5. 阶段 2：设计系统、登录和管理壳

### 5.1 目标

建立后续所有页面复用的视觉、组件、布局和交互基线，并证明后期更换主题或组件方案不需要复制业务页面。

### 5.2 Theme Runtime 与 UI 定制合同

必须先完成：

- `ThemeDefinition`、`ThemeTokenSet` 与 JSON Schema；
- Light、Dark、System、High Contrast 参考主题；
- `ThemeRegistry`、`UiPackRegistry`、`RendererRegistry`；
- `ComponentRecipeSet` 与 `LayoutRecipeSet`；
- Vue 挂载前 Theme Bootstrap，避免首屏闪烁；
- 当前用户偏好、Admin/Public 站点配置和 Preview Override 的解析优先级；
- 根据当前 Theme + Component Recipe 动态生成 Naive UI Theme Overrides；
- 未安装 Theme/UI Pack 的安全 fallback；
- Theme/UI Pack 切换时保持表单、筛选、Drawer、路由和 Query 状态。

### 5.3 基础组件与 Component Recipe

必须先完成：

- `AppButton`；
- `AppFormField`；
- `AppInput`；
- `AppTextarea`；
- `AppSelect`；
- `AppSwitch`；
- `AppCard`；
- `AppDialog`；
- `AppDrawer`；
- `ConfirmDialog`；
- `PageState`；
- `StatusBadge`；
- `AppPagination`；
- `AppToast`。

上述基础组件必须保持稳定 Props、Events、Slots、键盘和 ARIA 合同；Button、Input、Select、Card、Table、Dialog、Navigation、PageState 的具体外形由 Component Recipe 提供。第一阶段至少交付 `standard` 与 `compact` 两套可运行 Recipe 组合。

同时完成页面骨架：

- `AdminAppShell`、`AdminSidebar`、`AdminTopbar`；
- `PageHeader`、`PageToolbar`、`FilterBar`；
- `SplitWorkspace`、`DetailInspector`；
- `PublicAppShell`、`PublicHeader`、`PublicContent`；
- `sidebar`、`compact-sidebar`、`public-centered` 等 Layout Recipe；
- Standard UI Pack 与 Feature Renderer Registry 骨架；
- Theme/UI Pack/Density Switcher；
- Component Gallery 中的 Button、Input、Card、Table、Dialog、Navigation、Provider Card、Job Timeline、Resource Card，以及 Dashboard、Storage、Job Detail、Organizer 和 Public Search 稳定 Fixture。

### 5.4 登录

迁移：

- `/admin/login`；
- Session 初始化；
- 登录提交；
- 401 路由守卫；
- 原目标跳转；
- 退出登录；
- 默认密码警告。

### 5.5 管理壳

实现：

- Sidebar；
- Topbar；
- 用户头像/Logo；
- 桌面与移动导航；
- 页面标题；
- Breadcrumb；
- Route Loading；
- 全局 Error Boundary；
- Chunk 加载失败刷新提示。

Standard UI Pack 只作为参考实现，不锁定单一色系或固定组件外形。Sidebar、Topbar、Page Header、Workspace、Button、Input、Card、Table 和 Navigation 必须由 Theme、Component Recipe 与 Layout Recipe 驱动；避免复刻旧版巨大玻璃外框，也不得直接交付 Naive UI 默认 Layout。Feature 页面不得判断具体 Theme ID 或 UI Pack ID。

### 5.6 验收

- 登录/退出 E2E；
- 未登录访问管理页跳转登录；
- Session 过期正确处理；
- 键盘可完成登录和导航；
- 移动端 Sidebar 可用；
- 组件 Gallery 可切换 Theme、UI Pack 和 Density，并展示全部基础组件与关键 Renderer 状态；
- Light、Dark、System、High Contrast 可切换，Theme 切换无 Reload、无首屏闪烁且不丢交互状态；
- `standard` 与 `compact` Component Recipe 组合可运行，Button、Input、Card、Table、Dialog、Navigation 的差异可见但 Contract 一致；
- 管理端与访客端可以分别解析 Theme/UI Pack 配置，预览覆盖不污染正式配置；
- 1440、1024、768、390、320 五档页面骨架截图完成；
- Light、Dark、High Contrast 覆盖核心组件；每个 UI Pack 至少覆盖其主 Theme 和核心 Shell；
- 登录、管理壳和公共壳不呈现默认 Naive UI Demo 风格；
- Loading、Empty、Error、Unavailable 状态均有稳定视觉样例；
- 不加载任何旧 `admin*.js`。

## 6. 阶段 3：Storage Setup 首个完整业务切片

### 6.1 为什么先做 Storage

该模块同时覆盖：

- 动态表单；
- Provider Catalog；
- 多步骤认证；
- 轮询；
- 懒加载目录树；
- CRUD；
- 能力展示；
- 预检；
- 错误和字段错误；
- 复杂响应适配。

它能验证新架构是否真正解决旧前端问题。

### 6.2 页面信息架构

`/admin/storage` 内部使用子导航：

```text
账号
OpenList
保存位置
入库规则
完整预检
```

使用 Router Query 或嵌套路由保存当前子页，不使用隐藏 DOM Tab。

桌面视觉采用“步骤轨道 + 对象工作区 + 上下文编辑器”，中屏将编辑器下沉或转 Drawer，手机使用顶部步骤摘要与全屏编辑。具体尺寸、状态和对象卡结构以 `10-custom-ui-visual-spec.md` 第 8.3 节为准。

### 6.3 Provider Accounts

实现：

- Catalog 加载；
- 账号列表；
- 添加账号；
- 编辑非不可变字段；
- 登录/重新登录；
- 检测；
- 激活；
- 禁用；
- 删除；
- 最近检测和错误；
- Provider 能力 Badge。

### 6.4 Auth Session

实现 Provider Manifest 驱动的认证入口，但流程逻辑保持显式：

- 表单认证；
- 验证码；
- 二维码/设备授权；
- 等待轮询；
- 手工验证码提交；
- 换一张；
- 取消；
- 过期；
- 成功后刷新账号列表。

不得做一个“所有 Provider 通用万能认证状态机”。可以共享 Session Composable，但每种 Auth Method 有独立组件。

### 6.5 OpenList

实现：

- Connection 列表/创建/编辑/删除；
- Test；
- Mount 列表/创建/编辑/删除；
- Mount Test；
- Confirm Identity；
- 目录浏览。

### 6.6 保存位置

实现：

- Provider/账号选择；
- 原生定位器；
- OpenList 挂载和路径；
- 本地目标；
- Organizer 能力；
- 当前绑定修订；
- 创建、编辑、删除；
- 表单字段错误。

### 6.7 FolderTree

首版必须覆盖：

- Mobile `/`；
- Quark `0`；
- Tianyi `-11`；
- 懒加载；
- ID/Path 定位器；
- 节点错误重试；
- 键盘；
- 已选路径显示；
- 取消重复请求。

### 6.8 入库规则

实现：

- 搜索源默认目标；
- 分类覆盖；
- 不可执行原因；
- 路由矩阵；
- 保存后 Query 失效。

### 6.9 完整预检

- 分项显示；
- Provider 账号；
- OpenList；
- Mount；
- 原生上传/Organizer 证明；
- 保存位置；
- Assignment；
- 错误 Code、Message、Trace ID；
- 重新执行。

### 6.10 测试场景

- 无账号；
- 无激活账号；
- 登录等待；
- 验证码错误后继续；
- 登录过期；
- 目录空；
- 目录读取失败后重试；
- Tianyi folder_id；
- Quark path；
- Mobile provider_path；
- 原生空间不足；
- 认证失效；
- OpenList PUT 405 但 Provider relay 双证明有效；
- Preflight 部分失败；
- 删除被引用对象 409。

### 6.11 验收

- 旧 Storage 工作区全部功能等价；
- 页面代码没有 Provider 根目录字面量散落；
- API 只存在于 Storage API 模块；
- FolderTree 有独立组件测试；
- Auth Session 有 fake timer/Mock 测试；
- Playwright 覆盖创建账号到预检的 Mock 流程；
- 类生产环境至少完成真实目录只读浏览。
- Storage 默认、目录加载、部分预检失败和移动端编辑状态均进入视觉回归；
- 1280px 三栏、768px 下沉编辑器和 320px 全屏流程均通过人工视觉检查；
- 页面不存在“每个对象一个发光玻璃卡”和大量彩色 Badge 的默认拼装感。

## 7. 阶段 4：Dashboard、Requests 和 Jobs

### 7.1 Dashboard

- 统计卡；
- 系统状态；
- 最近请求；
- 最近任务；
- Worker/rclone readiness；
- 自动刷新；
- 局部错误不拖垮整页。

布局按“可操作摘要 + 最近任务 + 当前 Pipeline + 系统链路健康 + 最近动态”实现。只展示能够指导操作的指标，不为填满四张卡虚构健康分、趋势或百分比。

### 7.2 Requests

- 服务端分页；
- 状态筛选；
- 关键词；
- 详情；
- 批准；
- 拒绝；
- 取消；
- 操作原因输入；
- 操作后列表和详情同步。

### 7.3 Jobs

- 服务端分页；
- 状态、Provider、分类筛选；
- 详情路由；
- Execution Plan 摘要；
- Transfer 状态；
- Event Timeline；
- Worker、rclone、Organizer 信息；
- 重试、取消、删除；
- 批量重试；
- 日志链接；
- Trace ID 和错误码。

Job Detail 必须使用定制阶段轨道、事件时间线和执行摘要，不得只用 `NSteps + NDescriptions + NCard` 默认组合。未知进度只显示阶段和等待原因，不伪造百分比。

### 7.4 关键规则

- Requests 和 Jobs 是独立 Feature，不再放同一个 `admin-jobs.ts`；
- 详情路由可刷新；
- Mutation 不做乐观更新；
- 取消和删除必须确认；
- Running Job 使用 Query 轮询，终态停止；
- Timeline 状态映射集中。

### 7.5 验收

- 列表筛选反映到 URL；
- 浏览器前进/后退恢复筛选；
- 详情刷新不丢失；
- Mutation 防重复；
- 空态、错误态、Loading 完整；
- Playwright 覆盖批准请求、重试任务和取消确认。
- Dashboard、Jobs 列表、Job Detail 的默认/运行/失败/空态进入多断点视觉回归；
- Job Detail 在 390px 下转为单列，阶段和恢复动作仍完整可用。

## 8. 阶段 5：Organizer、rclone 与媒体

### 8.1 Organizer

拆分页面：

- Task List；
- Task Detail；
- Mapping Editor；
- Batch Edit Dialog；
- Run History；
- Rollback Dialog。

重点：

- 不把 Mapping Table 做成巨型页面组件；
- 原名、新名、分类、状态分别建字段组件；
- 批量修改有明确 Preview；
- Apply/Skip/Rollback 是危险操作；
- 任务运行中轮询；
- Manual Review 状态保持可编辑。

视觉采用“映射工作区 + 当前项检查器 + 运行历史”，桌面强调列对齐，手机转逐项审核卡。不得在手机保留七列压缩表格。

### 8.2 rclone

- 状态卡；
- Start/Stop；
- Runs；
- Events；
- File Events；
- File Retry；
- WebDAV Config；
- Task Logs；
- Date Range；
- Log Viewer。

日期选择使用组件库或项目 DateRange Wrapper，不迁移旧 DOM Date Picker 实现。

Log Viewer 使用实体深色工具面，统一搜索、级别、自动滚动、复制和截断提示；错误只高亮级别和左侧标记，不将整行日志涂成高饱和红色。

### 8.3 Media

- Library List；
- Running Refresh；
- Refresh Logs；
- Manual Refresh；
- 状态反馈。

### 8.4 验收

- Organizer 关键状态机通过 Mock E2E；
- Mapping 批量编辑可撤销未提交变更；
- rclone 日志不会造成页面卡死；
- Start/Stop 不重复提交；
- Media Refresh 反馈明确；
- 旧 Organizer/rclone/media 脚本不被新页面引用。
- Organizer 默认/脏状态/批量预览和 rclone 运行/日志错误状态进入视觉回归；
- 390px 下 Organizer 可逐项完成审核，Log Viewer 不发生双层横向滚动。

## 9. 阶段 6：Updates、Trending、Notifications、Settings

### 9.1 Updates

- 订阅列表；
- 创建/编辑；
- TMDB 搜索和详情；
- 路径快照；
- 预览；
- 手工执行；
- 暂停/启用；
- Runs；
- Candidates；
- 调度状态。

### 9.2 Trending

- 调度状态；
- 手工运行；
- 候选列表；
- 详情；
- 资源搜索；
- 文件详情；
- 入库；
- 订阅；
- 忽略/恢复。

资源搜索组件应复用访客资源搜索基础能力，但管理动作留在 Trending Feature。

### 9.3 Notifications

- 渠道配置；
- SMTP/Webhook；
- Digest；
- 事件规则；
- 测试发送；
- Delivery 列表；
- Retry；
- Secret configured 状态。

### 9.4 Settings / Profile / Search Settings

- Profile；
- Avatar/Logo；
- 默认密码提示；
- Search Providers；
- Aliases；
- TMDB Test；
- AI Test；
- BTBTLA Proxy Test；
- Advanced Config；
- Export/Import；
- 外观设置：Admin/Public Theme、UI Pack、Density 与品牌资源；
- Theme Token 编辑、Schema 校验、实时预览、导入、导出、发布和版本回滚；
- History Summary/Cleanup。

高级配置不应继续使用一个数百字段巨型组件。按 Section 拆分，并由统一 Form Dirty 状态协调保存。

Settings 使用左侧分区导航、中间有限宽表单和可选右侧说明；每个 Section 独立保存。Notifications 的规则矩阵在手机转按事件分组的 Checkbox 列表。

外观设置只允许提交结构化 Theme Token、Recipe 选择和安全品牌资源引用，不允许任意 CSS、HTML、JavaScript、Vue SFC 或外部脚本。高级 UI Pack 通过受版本控制的前端代码交付，配置页只选择已安装 Pack。

### 9.5 验收

- Secret 不回显；
- 文件上传/下载错误正确处理；
- 复杂设置表单有字段错误和离开提醒；
- Scheduler 时间输入统一；
- Trending 和 Updates 搜索组件不存在复制粘贴；
- 所有页面有 Feature 级测试；
- 外观预览不影响正式站点，发布后 Public Config 生效，回滚能恢复上一版本；
- 无效 Theme JSON、未知 UI Pack 和不安全品牌资源被拒绝；
- Settings、Notifications 和 Trending Workbench 的桌面/手机视觉基线完成；
- 长设置表单不存在无限 Card 嵌套，脏状态和保存范围在视觉上明确。

## 10. 阶段 7：访客端

### 10.1 Public Shell

- 品牌 Header；
- 搜索/链接/热榜/查询路由；
- 公共配置加载；
- 站点 Logo；
- 移动导航；
- 全局错误与 Toast；
- 兼容旧 Hash。

Public Shell 必须比管理端更轻、更内容化：顶部品牌导航、有限内容宽度、搜索为首屏唯一主焦点，不复制管理端 Sidebar 或巨大玻璃框。

Public Shell 使用独立的 Public Theme、Public Layout Recipe 和 Public UI Pack 解析结果；可以与管理端同品牌，也可以完全不同，但路由、搜索、提交、状态反馈和可访问性合同保持一致。

### 10.2 搜索

- 关键词；
- 搜索源；
- 分类；
- 搜索状态；
- 补充搜索；
- Resource Cards；
- Detail；
- 文件树；
- 选择资源。

搜索结果优先使用纵向内容卡列表；资源详情在桌面使用路由同步 Drawer/详情页，在手机使用全屏页面。文件选择持续显示已选数量/大小和“未选择代表整个分享”的真实语义。

### 10.3 手动链接

- URL；
- 提取码；
- 标题；
- 分类；
- 备注；
- Detect；
- Preview；
- Provider 特定文件选择；
- Captcha；
- Confirm；
- Slow Submit Confirm；
- 邮件通知。

### 10.4 热榜

- 分类 Tab；
- 榜单卡片；
- 已可观看状态；
- 搜索资源入口；
- 空态和更新时间。

### 10.5 查询与状态页

- Request Token 输入；
- 最近提交；
- 状态查询；
- `/request/:token`；
- 终态停止轮询；
- 复制编号。

### 10.6 资源选集注意事项

- Quark、Mobile、Tianyi Adapter 分离；
- 通用组件不猜 Provider 字段；
- 选集 Hash 由后端生成/校验；
- 前端不提交 destination、账号、mount 或路径；
- 客户端禁止字段必须有测试；
- 未选择时的“整个分享”语义必须与后端一致。

### 10.7 验收

- Public Config 可选 Provider 未配置仍正常展示；
- 搜索、详情、选集和提交 E2E；
- Captcha 失败可继续；
- 移动端 320px 无横向溢出；
- 键盘可完成主要路径；
- 最近提交不存敏感信息；
- 慢速提交确认不丢失选集。
- Public Search、Resource Detail、Submit Confirm、Success 和 Request Status 的 390px/320px 视觉回归通过；
- 访客端不泄露内部错误、Trace ID、Provider Token 或目标路径；
- 页面不呈现管理端高密度控制台的直接复制版本；
- 切换 Public Theme/UI Pack 不丢失关键词、资源选集、分类和提交状态；未安装 Pack 自动回退到 Standard Public Pack。

## 11. 阶段 8：生产集成与切换

### 11.1 任务

1. Docker 增加 Node Build Stage。
2. 将 `frontend/dist` 复制到应用镜像。
3. Flask 提供 SPA `index.html` 和静态资产。
4. 正确排除 `/api`、健康检查、Swagger 和 Worker 内部接口。
5. 配置 Cache-Control：
   - `index.html`: no-cache；
   - hash assets: immutable。
6. 更新 CI。
7. 在源码构建 Compose 验证 Web + Worker。
8. 执行管理端和访客端生产冒烟。
9. 演练旧镜像回滚。

### 11.2 切换顺序

```text
完成全功能构建
  -> Preview 环境验收
  -> 打包候选镜像
  -> 启动候选 Web/Worker
  -> /readyz
  -> 管理端冒烟
  -> 访客端冒烟
  -> 切换正式入口
  -> 观察日志和 4xx/5xx
```

### 11.3 验收

- 页面刷新不会 404；
- Worker 不需要 Node Runtime；
- 镜像仍支持 amd64/arm64；
- 静态资源使用内容 Hash；
- 浏览器不会长期缓存旧 index；
- `/readyz` 继续通过；
- 回滚只需切换旧镜像，不需要恢复数据库。

## 12. 阶段 9：旧前端退役

### 12.1 删除候选

在确认无引用后删除：

- `templates/admin.html`；
- `templates/admin_login.html`；
- `templates/submit.html`；
- `templates/request_status.html`；
- 对应 `static/admin*.js`；
- 对应 `static/public*.js`；
- `static/ui-core.js`；
- `static/ui-dialog.js`；
- `static/ui-select.js`；
- 已被 Vue 主题替代的 CSS；
- 只验证旧 DOM/字符串的测试。

Swagger 模板可以继续保留，除非同时改为本地静态 Swagger。

### 12.2 删除要求

- 使用 `rg` 确认无生产引用；
- 不删除仍被邮件确认页等使用的公共 CSS 前先迁移这些页面；
- 将有价值的旧行为测试转为 API/Playwright/组件测试；
- 删除不是注释保留；
- 更新 README 截图和开发说明；
- 更新 `.dockerignore` 和 Docker COPY 范围。

## 13. 每阶段交付模板

每阶段完成时，AI 必须报告：

```text
阶段：
完成任务：
新增页面/组件：
后端改动：
兼容行为：
测试命令与结果：
构建结果：
视觉截图与检查结果：
未覆盖项：
风险：
回滚方式：
下一阶段依赖：
```

不得只报告“页面已完成”或“构建通过”。
