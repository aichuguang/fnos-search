# 03｜设计系统与组件规范

## 1. 目标

公共组件的目标不是“把所有页面变成配置”，而是统一重复出现的视觉、交互、无障碍和状态行为。

组件与定制能力分层：

```text
Theme Tokens
  -> Component Recipes
  -> UI Primitives
  -> Business Shared Components / Renderer Contracts
  -> Feature Components / Pages
  -> Layout Recipes / UI Pack
```

业务页面不得绕过基础组件随意重新实现按钮、输入框、弹窗、加载态、空态、错误态、分页和确认流程。

## 2. 可定制视觉方向

本项目不锁定具体品牌风格。设计系统必须支持：

- Light、Dark、System 和 High Contrast Theme；
- 管理端与访客端独立主题；
- 字体、颜色、间距、圆角、阴影、透明度和 Density 切换；
- Button、Input、Card、Table、Dialog、Navigation 等 Component Recipe；
- Admin/Public App Shell Layout Recipe；
- Provider Card、Job Timeline、Resource Card 等 Feature Renderer；
- 完整 UI Pack 的后期扩展；
- `prefers-reduced-motion`、Reduced Transparency 和可访问性约束。

Naive UI 只作为行为和基础控件基座，项目主题必须通过动态 Theme Overrides、项目 Wrapper 和 Component Recipe 体现，不得直接使用 Naive UI 默认视觉。

页面级信息结构、状态语言、响应式和视觉验收以 [10-custom-ui-visual-spec.md](./10-custom-ui-visual-spec.md) 为真源；Theme、Component Recipe、Layout Recipe、Renderer 和 UI Pack 以 [11-theme-engine-and-ui-customization.md](./11-theme-engine-and-ui-customization.md) 为真源。

## 3. Design Tokens

### 3.1 Semantic Token 合同

Runtime 必须输出稳定语义变量，具体值由当前 Theme 提供：

```css
:root {
  --ui-color-canvas: ...;
  --ui-color-shell: ...;
  --ui-color-navigation: ...;
  --ui-color-workspace: ...;
  --ui-color-surface-1: ...;
  --ui-color-surface-2: ...;
  --ui-color-surface-3: ...;
  --ui-color-surface-selected: ...;

  --ui-color-text-primary: ...;
  --ui-color-text-secondary: ...;
  --ui-color-text-muted: ...;
  --ui-color-text-disabled: ...;
  --ui-color-text-on-primary: ...;

  --ui-color-border-default: ...;
  --ui-color-border-strong: ...;
  --ui-color-divider: ...;

  --ui-color-primary: ...;
  --ui-color-primary-hover: ...;
  --ui-color-primary-active: ...;
  --ui-color-primary-soft: ...;
  --ui-color-success: ...;
  --ui-color-warning: ...;
  --ui-color-danger: ...;
  --ui-color-info: ...;

  --ui-space-1: ...;
  --ui-space-2: ...;
  --ui-space-3: ...;
  --ui-space-4: ...;

  --ui-radius-sm: ...;
  --ui-radius-md: ...;
  --ui-radius-lg: ...;

  --ui-shadow-overlay: ...;
  --ui-focus-ring: ...;

  --z-dropdown: 1000;
  --z-sticky: 1100;
  --z-drawer: 1200;
  --z-modal: 1300;
  --z-toast: 1400;
}
```

变量名称和语义必须稳定，页面不得使用具体 Theme Palette 名称，也不得在 Feature 中判断 Theme ID。完整 Theme Contract 见 `11-theme-engine-and-ui-customization.md`。

### 3.2 Breakpoints

统一断点：

| 名称 | 宽度 | 用途 |
|---|---:|---|
| `xs` | 480px | 小屏手机 |
| `sm` | 640px | 手机横屏/大屏手机 |
| `md` | 768px | 平板 |
| `lg` | 1024px | 小桌面 |
| `xl` | 1280px | 标准桌面 |
| `2xl` | 1536px | 宽屏 |

不得继续为单个页面任意新增 700、720、760、860、900、980 等大量近似断点。确有必要时必须记录原因。

### 3.3 Typography

至少定义：

- 页面标题；
- 区块标题；
- 卡片标题；
- 正文；
- 辅助文本；
- 表格文本；
- 单行代码/日志文本。

日志使用等宽字体，并限制敏感信息展示。

字号、字体栈、圆角、阴影、动效和信息密度由 Theme/Component Recipe 提供。组件不得各自定义一套固定视觉比例，也不得假设当前主题为暗色。

## 4. 第三方组件封装原则

业务页面优先使用项目组件：

```vue
<AppButton />
<AppSelect />
<AppDialog />
<AppDataTable />
```

而不是直接散落：

```vue
<NButton />
<NSelect />
<NModal />
<NDataTable />
```

允许 Feature 内部直接使用 Naive UI 的复杂低频组件，但需满足：

- 没有项目级行为需要统一；
- 不会在三个以上 Feature 重复；
- 主题已由全局 Provider 覆盖；
- 不把 Naive UI 类型泄漏到 Feature 公共 API。

以下部分即使内部使用 Naive UI，也必须完全呈现项目定制外形和业务结构：Admin App Shell、Sidebar、Page Header、Dashboard Pipeline、Storage Step Rail、Provider Account Card、Job Stage Track、Job Timeline、Organizer Workspace、LogViewer、Public Search Hero、Resource Card、ResourceFileTree 和 Request Status Track。

## 5. UI 基础组件清单

### 5.1 `AppButton`

职责：统一按钮样式、Loading、防重复提交、危险操作和图标间距。

建议 Props：

```ts
type AppButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
type AppButtonSize = 'sm' | 'md' | 'lg'

interface AppButtonProps {
  variant?: AppButtonVariant
  size?: AppButtonSize
  loading?: boolean
  disabled?: boolean
  block?: boolean
  iconOnly?: boolean
  loadingLabel?: string
}
```

规则：

- Loading 时保持宽度稳定；
- `iconOnly` 必须提供 `aria-label`；
- 危险操作不能只靠红色表达；
- Mutation Loading 时默认禁用重复点击。

### 5.2 `AppFormField`

职责：统一 Label、必填标识、说明、错误和 `aria-describedby`。

必须支持：

- `label`；
- `required`；
- `description`；
- `error`；
- `fieldId`；
- 默认 Slot。

字段错误必须靠近字段显示，不得只弹 Toast。

### 5.3 `AppInput` / `AppTextarea`

- 支持前后缀图标；
- 支持 Secret configured 状态；
- 统一最大长度提示；
- 不自动 Trim 密码；
- 支持后端字段错误；
- 移动端输入类型正确。

### 5.4 `AppSelect`

- 统一空选项、Loading、搜索、清除、禁用和错误状态；
- Select 的值必须有明确泛型；
- 禁止再次通过隐藏原生 Select + DOM 重建自定义控件；
- 键盘、焦点和 ARIA 由组件库与 Wrapper 保证。

### 5.5 `AppSwitch` / `AppCheckbox`

- Label 可点击；
- 禁用原因可展示；
- 支持说明文本；
- 不只依赖颜色表示状态。

### 5.6 `AppCard`

Variants：

- `default`；
- `soft`；
- `interactive`；
- `danger`；
- `success`。

卡片只负责布局和视觉，不负责业务请求。

### 5.7 `AppDialog`

统一：

- Modal 层级；
- ESC；
- 点击遮罩策略；
- 焦点锁定；
- 关闭后焦点恢复；
- 标题和描述 ARIA；
- 异步确认；
- 错误区；
- 移动端全屏模式。

### 5.8 `AppDrawer`

用于列表详情和编辑，但不得代替路由。复杂详情打开时 URL 必须同步，刷新后可以恢复。

### 5.9 `ConfirmDialog`

由 `useConfirm()` 调用：

```ts
const confirmed = await confirm({
  title: '确认取消任务？',
  message: '取消后可能触发来源和临时文件清理。',
  confirmText: '确认取消',
  tone: 'danger',
})
```

禁止页面直接调用 `window.confirm()`。

### 5.10 `AppToast`

适合：

- 保存成功；
- 操作完成；
- 已复制；
- 非阻断性提示。

不适合：

- 页面加载失败；
- 表单字段错误；
- 需要用户复制 Trace ID 的持久错误。

### 5.11 `PageState`

提供：

- `LoadingState`；
- `EmptyState`；
- `ErrorState`；
- `PermissionState`；
- `UnavailableState`。

页面不允许只显示空白或永久 Spinner。

### 5.12 `AppDataTable`

职责：

- 表头、行、空态、Loading；
- 服务端分页；
- 服务端排序；
- 行操作；
- 移动端降级为 Card List；
- 列可见性；
- 行键稳定；
- 选中和批量操作。

不得开发一个通过 JSON 配置完成所有业务操作的“万能表格”。列定义可以复用，业务动作留在 Feature。

### 5.13 `AppPagination`

- 页码和每页数量同步 Router Query；
- 更改筛选后回到第一页；
- 显示总数；
- 移动端使用简化模式；
- 后端没有总数时支持上一页/下一页模式。

### 5.14 `StatusBadge`

输入稳定语义，而不是任意 CSS Class：

```ts
type StatusTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'
```

由 Feature 把后端状态映射为 Label 和 Tone。

### 5.15 `AsyncActionButton`

用于有副作用操作：

- 自动绑定 Mutation Pending；
- 可选 Confirm；
- 防重复；
- 成功 Toast；
- 失败交给标准错误处理；
- 不自动重试危险 Mutation。

### 5.16 `LogViewer`

- 等宽字体；
- 文本搜索；
- 自动滚动开关；
- 行数限制；
- 复制；
- 下载（后端支持时）；
- Trace ID 高亮；
- Secret 遮罩；
- 大日志不一次性创建数万个 DOM 节点。

## 6. 跨业务公共组件

### 6.1 `ProviderIcon` / `ProviderBadge`

统一 Provider：

- `mobile`；
- `quark`；
- `tianyi`；
- `sixpan`；
- `local`；
- `generic`；
- 未来扩展 Provider。

未知 Provider 使用稳定 fallback，不抛异常。

### 6.2 `CapabilityBadge`

展示：

- 原生转存；
- 原生离线；
- OpenList 可读；
- OpenList 可写；
- Provider API 上传；
- Organizer Mutation；
- 当前不可用原因。

能力不能只显示一个“正常/异常”总状态。

### 6.3 `ProviderAccountCard`

包含：

- Provider 和账号身份摘要；
- 激活/禁用状态；
- 当前 revision；
- 最近探测；
- configured 状态；
- 登录、检测、激活、禁用、编辑和删除动作。

Secret 不回显。

### 6.4 `FolderTree`

这是重构首批核心组件，必须设计为通用但不过度抽象。

节点接口：

```ts
export interface FolderNode {
  id: string
  name: string
  path?: string
  parentId?: string
  hasChildren?: boolean
  disabled?: boolean
  meta?: Record<string, unknown>
}
```

Loader：

```ts
export type LoadFolderChildren = (
  node: FolderNode,
  signal: AbortSignal,
) => Promise<FolderNode[]>
```

必须支持：

- 懒加载；
- 根节点可选；
- ID 模式或 Path 模式由外部决定；
- 节点级 Loading/Error/Retry；
- 已选路径回显；
- 键盘上下移动、左右展开折叠、Enter 选择；
- 展开状态局部维护；
- 加载请求取消；
- 防止同节点重复加载；
- 树容器滚动位置稳定；
- 大树可后续增加虚拟化，但首版不提前复杂化。

禁止在通用树中写死 Quark 根目录 `0`、天翼 `-11` 或移动云 `/`。这些属于 Provider Manifest/Feature Adapter。

### 6.5 `PreflightResultDialog`

输入：

```ts
interface PreflightCheckView {
  key: string
  name: string
  ok: boolean
  message: string
  severity?: 'info' | 'warning' | 'error'
  traceId?: string
}
```

支持成功汇总、失败聚焦、重新执行、复制诊断，不把所有检查压成一个布尔值。

### 6.6 `JobStatusBadge` / `JobTimeline`

统一展示：

- Job 状态；
- Transfer phase；
- Worker 状态；
- rclone 状态；
- Organizer 状态；
- 失败码和时间。

状态映射集中在 Jobs Feature，不允许多个页面分别维护中文状态字典。

### 6.7 `ResourceCard`

用于访客搜索和后台热榜候选资源：

- 标题、年份、分类、来源；
- Provider 图标；
- 质量标签；
- 可用状态；
- 展开详情；
- 选择资源；
- Loading/Error。

### 6.8 `ResourceFileTree`

与普通 FolderTree 不完全相同：

- 文件与目录混合；
- 支持 Checkbox；
- Provider 选集规则不同；
- 需要文件大小、路径和分享 token 摘要；
- 选择父目录时不一定自动选择所有未加载子节点。

因此不得强行与 Storage FolderTree 合并成一个万能组件。可以复用底层 Tree Row、Loading 和键盘工具。

### 6.9 `MediaCategorySelect`

统一电影、电视剧、动漫、综艺和其他分类。后台配置返回分类列表时，以后端为真源；图标与常见排序可由前端映射。

## 7. Feature 组件边界示例

### Storage Feature

- `ProviderAccountList`
- `ProviderAccountEditor`
- `ProviderAuthDialog`
- `OpenListConnectionList`
- `OpenListConnectionEditor`
- `StorageLocationList`
- `StorageLocationEditor`
- `SourceAssignmentEditor`
- `StoragePreflightDialog`

这些组件不应移动到全局 `components/business`，因为只属于 Storage Feature。

### Jobs Feature

- `JobFilterBar`
- `JobList`
- `JobDetailHeader`
- `JobExecutionPlanPanel`
- `JobEventTimeline`
- `JobActionMenu`

### Organizer Feature

- `OrganizerTaskList`
- `OrganizerMappingTable`
- `OrganizerBatchEditDialog`
- `OrganizerRunHistory`
- `OrganizerRollbackDialog`

## 8. 响应式规则

### 管理端

- `>= 1024px`：Sidebar + 主内容；
- `< 1024px`：Sidebar 变 Drawer；
- 表格在窄屏优先转卡片，不简单横向塞入双重滚动；
- 操作按钮可进入 More Menu；
- 详情 Drawer 在手机变全屏；
- 表单双列在 `< 768px` 变单列。
- Storage、Organizer 和 Settings 的右侧检查器在中屏下沉，在手机变全屏，不允许挤成三窄列；
- Dashboard、Jobs、Requests 等对象表格在手机优先转卡片列表；
- 详细断点矩阵以 `10-custom-ui-visual-spec.md` 第 10 节为准。

### 访客端

- 移动优先；
- 搜索框和主要 CTA 触控高度至少 44px；
- 文件树行可触控，不要求精确点击小图标；
- 确认框在手机使用底部 Sheet 或全屏 Dialog；
- 热榜卡片使用一列/两列自适应；
- 避免横向滚动。
- 管理端视觉不得原样复制到访客端，访客内容宽度、Header 和卡片密度按页面级规范实现。

## 9. 可访问性标准

- 所有表单字段有可访问名称；
- 错误通过 `aria-describedby` 与字段关联；
- Dialog 有标题，焦点进入并可退出；
- Tab 使用 Router 时仍保持明确当前状态；
- Icon-only Button 必须有 `aria-label`；
- 状态不只靠颜色；
- 动态成功提示使用 `aria-live="polite"`；
- 阻断错误使用 `role="alert"` 或 assertive；
- Tree 支持键盘基本操作；
- `prefers-reduced-motion` 下关闭非必要动画；
- 对比度至少满足 WCAG AA 的正文要求；
- Playwright 使用 axe 扫描主要页面。

## 10. 组件治理

### 10.1 何时抽公共组件

满足任一条件可以抽取：

- 在两个以上 Feature 中行为和视觉都相同；
- 涉及无障碍、焦点、请求状态等需要统一保证；
- 更换实现时希望全局一致；
- 存在明确稳定的输入输出契约。

不满足时留在 Feature 内。

### 10.2 禁止的“复用”

- 一个组件通过几十个布尔 Props 支持所有页面；
- 用字符串配置执行任意 API；
- 让通用组件知道 Provider 业务规则；
- 把整页逻辑塞进 `useCommonPage()`；
- 为了少写几行代码创建无法理解的动态渲染器；
- 所有表单都由一个 JSON 配置文件生成。

### 10.3 组件展示页

开发环境提供 `ComponentGalleryPage`，展示：

- 颜色和排版 Token；
- Button、Input、Select、Switch；
- Dialog、Drawer、Toast；
- Loading、Empty、Error；
- Table、Pagination；
- Provider Badge、Status Badge；
- FolderTree 和 ResourceFileTree Mock；
- 移动端宽度预览。
- Admin/Public Shell、PageHeader、FilterBar、SplitWorkspace 等页面骨架；
- Dashboard、Storage、Job Detail、Organizer 和 Public Search 的稳定 Fixture 状态；
- 1440、1024、768、390、320 五档视觉预览入口。

该页面不得进入生产导航，生产构建可通过环境变量移除。

## 11. 组件完成定义

一个公共组件只有满足以下条件才算完成：

1. Props/Emits 类型明确；
2. 默认、Hover、Focus、Disabled、Loading 和 Error 状态完整；
3. 支持键盘或说明不适用；
4. 具有组件测试；
5. 在 Gallery 中有示例；
6. 不依赖具体 Feature；
7. 使用 Design Token；
8. 移动端可用；
9. 不把第三方类型泄漏给调用方；
10. 中文文案为 UTF-8 源码常量，不通过运行时乱码转换修补。
11. 不暴露 Naive UI 默认 Demo 风格；
12. 在 1440、768、390 和 320 宽度下完成视觉检查；
13. Loading、Empty、Error、Unavailable 和 Success 状态具有一致外形；
14. 如属于核心页面组件，已进入 Playwright 视觉回归基线。
