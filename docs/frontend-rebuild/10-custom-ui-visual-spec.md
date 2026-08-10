# 10｜定制页面 UI、组件呈现与视觉验收规范

> 文档状态：定稿，作为页面结构、组件呈现和视觉验收的共同真源  
> 适用范围：管理端、访客端、登录页、状态页、组件和响应式布局  
> 视觉策略：不锁定单一品牌方向，通过 Theme、Component Recipe、Layout Recipe 和 UI Pack 切换  
> 实现前置：先阅读 `03-design-system-and-components.md` 与 `11-theme-engine-and-ui-customization.md`  
> 核心要求：Naive UI 只能提供底层行为，页面和组件必须可定制、可换肤、可替换呈现，不能绑定默认组件库或某一固定主题

## 1. 文档目标

本文件解决“工程重构完成了，但页面仍像默认组件库 Demo”的问题。

它不是一张固定效果图，也不是只规定颜色的 Theme 文件，而是面向 AI 开发的页面与组件合同，明确：

- 哪些信息结构和交互原则在不同主题下必须稳定；
- 管理端与访客端如何在不同 Theme/UI Pack 下保持同一产品语义；
- 页面布局、信息密度、组件合同和交互反馈如何统一；
- Dashboard、Storage、Jobs、Organizer、日志、设置和访客搜索应如何排版；
- 桌面、平板和手机分别如何重排；
- Loading、Empty、Error、Unavailable、Success 等状态如何表现；
- 哪些能力可由 Naive UI 承担，哪些组件呈现必须由项目或 UI Pack 定制；
- Theme、Component Recipe、Layout Recipe 和 Feature Renderer 分别负责什么；
- AI 如何通过截图、交互和自动化检查证明页面达到设计要求。

本文件优先级如下：

```text
业务行为与接口契约
  > 可访问性和可用性
  > 本文件的页面结构与组件合同
  > 当前启用的 Theme/UI Pack 规则
  > 单个效果图中的偶然像素
  > Naive UI 默认表现
```

若原型与本文冲突，以本文为准；若本文与真实业务行为冲突，先保持业务正确，再更新本文，不得暗中删减功能。

## 2. 已锁定的可定制 UI 决策

以下内容视为本轮前端重建的固定决定：

1. 页面不得直接套用 Naive UI 默认亮色或暗色主题。
2. 不锁定某一种品牌色、背景风格、圆角、阴影或组件皮肤。
3. 必须具备 Light、Dark、System、High Contrast 参考主题，并允许后续新增 Theme。
4. Button、Input、Card、Table、Dialog、Navigation 等基础组件必须支持 Component Recipe。
5. Provider Card、Job Timeline、Resource Card 等复杂业务组件必须支持稳定 Renderer Contract。
6. 管理端和访客端可以使用不同 Theme、UI Pack 和 Layout Recipe。
7. 页面信息架构、业务状态、可访问性和操作语义不能因换肤而改变。
8. 页面不是“若干相同卡片的堆叠”。统计、列表、流程、编辑器、日志和状态应使用与任务匹配的信息结构。
9. 成功、警告、失败必须同时依赖图标、文案或结构，不能只依赖颜色。
10. 表格、文件树、日志、时间线等高密度区域必须保证可读性；具体表面由 Theme/UI Pack 决定。
11. 页面级状态必须就地展示；Toast 只负责短时操作反馈。
12. 页面与组件视觉验收属于功能完成条件，不是最后“有空再美化”的阶段。
13. 任何 AI 新增页面或组件前，必须确定其业务合同、可定制层级和移动端降级方式。
14. 第一版不允许任意 CSS/HTML/JavaScript 主题注入；完整组件定制通过受版本控制的 UI Pack 交付。

## 3. 可定制产品表达

### 3.1 稳定体验原则

无论启用何种 Theme 或 UI Pack，界面都应传达：

- **可靠**：状态与结果可信，错误可以定位；
- **清晰**：用户能知道资源现在位于哪一阶段；
- **可理解**：不靠装饰掩盖复杂业务；
- **专业但不生硬**：运维信息完整，中文文案直接；
- **高效**：常用操作路径短，批量任务和复杂配置不迷路。

### 3.2 Theme、Recipe 与 UI Pack 的关系

```text
Theme
  负责颜色、字体、圆角、间距、阴影和动效值

Component Recipe
  负责 Button/Input/Card/Table/Dialog 等基础组件皮肤

Layout Recipe
  负责 Sidebar/Topbar/Page Header/Workspace 的结构组合

Feature Renderer
  负责 Job Timeline/Provider Card/Resource Card 等复杂组件呈现

UI Pack
  组合 Component Recipe、Layout Recipe 和 Feature Renderer
```

详细合同、目录和切换流程见 `11-theme-engine-and-ui-customization.md`。

### 3.3 管理端与访客端的关系

两端共享：

- 业务词汇、状态语义和交互反馈原则；
- 组件 Props、Events、Slots 和可访问性合同；
- 资源、Provider、任务状态的命名；
- 错误、空态和成功反馈原则。

两端不同：

| 维度 | 管理端 | 访客端 |
|---|---|---|
| 核心目标 | 监控、配置、处理异常 | 找到资源并完成提交 |
| 信息密度 | 高 | 中低 |
| 主导航 | 默认可用 Sidebar，也允许 UI Pack 切换布局 | 默认顶部精简导航，也允许 Public Layout Recipe |
| 页面宽度 | 尽量利用桌面空间 | 内容区限制最大阅读宽度 |
| 卡片使用 | 少而明确，更多列表/面板 | 内容卡片和步骤卡可适当增加 |
| 状态细节 | 展示 Code、Trace ID、阶段和依赖 | 翻译为用户可理解的进度与下一步 |
| 日志/技术字段 | 可见 | 默认隐藏 |

## 4. 视觉设计原则

### 4.1 结构优先于装饰

- 先通过栅格、留白、分组标题和分隔线建立层级；
- 再使用背景层级；
- 最后才使用阴影、渐变和透明度；
- 不允许用“每块内容都加 Card”代替信息架构。

### 4.2 主任务优先

每个页面必须只有一个视觉主任务：

| 页面 | 视觉主任务 |
|---|---|
| Dashboard | 判断系统是否正常、是否有需要处理的任务 |
| Storage Setup | 完成账号到保存位置和路由的可执行配置 |
| Requests | 审核和跟踪访客请求 |
| Jobs | 定位任务状态、失败位置和可执行动作 |
| Organizer | 审核并修改标准化映射 |
| rclone | 查看运行、定位日志、执行安全重试 |
| Settings | 修改配置并明确保存范围与脏状态 |
| Public Search | 搜索、选择、提交资源 |
| Request Status | 看懂进度并保留查询编号 |

页面内不属于主任务的信息降为次要层级，不与主操作争夺视觉焦点。

### 4.3 颜色服务于语义

- 中性内容使用当前 Theme 的中性语义 Token；
- 主 CTA 使用当前 Theme 的 Primary Token；
- 状态色只出现在图标、短标签、轨道节点、左侧提示条或少量背景；
- 不把整行表格或整张大卡片填成高饱和状态色；
- 同一个状态在 Badge、Timeline、Toast、图表和日志高亮中保持同一语义映射。
- 页面不得假设 Primary 一定是蓝色，也不得假设 Canvas 一定是暗色。

### 4.4 高密度不等于拥挤

- 桌面表格行默认紧凑，但主要名称和操作不能互相挤压；
- 次要元数据优先放到第二行或详情面板；
- 筛选条件超过 4 个时使用可折叠高级筛选，不把页面 Header 塞满；
- 详情信息超过一屏时使用锚点/分区，不使用无限嵌套 Card。

### 4.5 操作结果可追踪

任何写操作都至少具有：

1. 触发前的明确动作名称；
2. Pending 状态和防重复；
3. 成功反馈；
4. 失败原因和可复制 Trace ID；
5. 对列表、详情或状态的同步刷新；
6. 危险操作的影响说明与确认。

## 5. 语义视觉令牌合同

### 5.1 基础颜色语义

页面和组件只使用稳定语义 Token，具体值由当前 Theme 提供：

| Token | 用途 |
|---|---|
| `--ui-color-canvas` | 浏览器或应用最外层背景 |
| `--ui-color-shell` | 应用壳背景 |
| `--ui-color-navigation` | 导航区域背景 |
| `--ui-color-workspace` | 页面工作区背景 |
| `--ui-color-surface-1` | 默认面板、表格、表单区域 |
| `--ui-color-surface-2` | 抬升面板、弹窗、Drawer |
| `--ui-color-surface-3` | Hover、选中、内层交互面 |
| `--ui-color-surface-selected` | 当前对象和当前路由 |
| `--ui-color-text-primary` | 主文本 |
| `--ui-color-text-secondary` | 次文本 |
| `--ui-color-text-muted` | 辅助说明、时间 |
| `--ui-color-text-disabled` | 禁用文本 |
| `--ui-color-border-default` | 默认边框和分隔线 |
| `--ui-color-border-strong` | 当前区域和强边界 |

### 5.2 操作与状态语义

| Token | 用途 |
|---|---|
| `--ui-color-primary` | 主按钮、当前路由、关键进行态 |
| `--ui-color-primary-hover` | 主操作 Hover |
| `--ui-color-primary-active` | 主操作 Active |
| `--ui-color-primary-soft` | 主操作柔和背景 |
| `--ui-color-success` | 成功、就绪、健康 |
| `--ui-color-warning` | 等待、部分可用、需注意 |
| `--ui-color-danger` | 失败、阻断、危险操作 |
| `--ui-color-info` | 系统提示和辅助状态 |

Theme 必须同时提供每种状态的 Soft Surface 和可读 Text Token。状态色不要求固定为某个具体色相，但必须保持用户可理解的稳定语义和足够对比度。

### 5.3 背景与装饰

- 背景可以是纯色、轻渐变、材质或品牌图形，由 Theme/UI Pack 决定；
- 页面结构不能依赖某个背景一定是暗色或透明；
- 面板、边框和阴影必须来自 Semantic/Component Token；
- 装饰不能降低表格、树、日志和表单的可读性；
- Theme 可以完全不使用玻璃、渐变或 Glow；
- 背景资源加载失败时仍有可用的纯色 fallback；
- 具体主题值、Theme Schema 和安全边界见 `11-theme-engine-and-ui-customization.md`。

### 5.4 字体

Standard UI Pack 的中文字体 fallback：

```css
font-family:
  Inter,
  "PingFang SC",
  "Microsoft YaHei",
  "Noto Sans CJK SC",
  system-ui,
  sans-serif;
```

日志与技术标识：

```css
font-family:
  "JetBrains Mono",
  "Cascadia Mono",
  "SFMono-Regular",
  Consolas,
  monospace;
```

Standard UI Pack 的默认字体层级：

| 类型 | 桌面 | 手机 | 字重 | 用途 |
|---|---:|---:|---:|---|
| Display | 32px | 28px | 600 | 访客首页主标题，少量使用 |
| Page Title | 24px | 22px | 600 | 页面标题 |
| Section Title | 18px | 18px | 600 | 工作区分区 |
| Card Title | 15px | 15px | 600 | 小面板标题 |
| Body | 14px | 14px | 400 | 默认正文 |
| Compact Body | 13px | 13px | 400 | 表格、密集列表 |
| Caption | 12px | 12px | 400/500 | 时间、说明、标签 |
| Mono | 12px/13px | 12px | 400 | ID、路径、日志 |

规则：

- 中文正文不使用 11px 以下字号；
- 页面标题不使用全大写英文代替中文；
- 数字统计使用 Tabular Numbers；
- 不使用 700～900 的超粗字体制造“科技感”；
- 辅助文字通过颜色和位置降级，不只靠缩小字号。
- Theme 可以从安全字体白名单中替换字体族；UI Pack 可以调整 Type Scale，但正文不得低于可访问性下限。

### 5.5 间距与栅格

Standard Density 的基础间距：`4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64`。

常用规则：

| 场景 | 间距 |
|---|---:|
| 图标与文字 | 8px |
| 表单 Label 与控件 | 8px |
| 相关控件 | 12px |
| 卡片内部紧凑区 | 16px |
| 默认面板内边距 | 20px |
| 主要工作区内边距 | 24px～32px |
| 页面 Header 到内容 | 24px |
| 大分区之间 | 32px～40px |

管理端内容栅格：

- 桌面采用 12 列，列间距 20px；
- 统计摘要最多 4 列；
- 主要列表/工作区占 8～9 列，辅助状态占 3～4 列；
- 不为了填满 12 列虚构统计卡；
- 宽屏内容可以扩展，但正文和表单单列阅读宽度不超过 760px。
- Comfortable/Compact Density 可以改变组件内部间距和行高，但不能改变触控下限或导致信息裁切。

### 5.6 圆角

| Component Token | Standard Recipe 参考值 | 用途 |
|---|---:|---|
| `--fn-radius-xs` | 6px | Checkbox 内部、短标签 |
| `--fn-radius-sm` | 8px | Input、小按钮、表格选中 |
| `--fn-radius-md` | 12px | Button、Filter、普通交互块 |
| `--fn-radius-lg` | 16px | 面板、Card、Drawer 内容块 |
| `--fn-radius-xl` | 20px | Dialog、访客端重点卡 |
| `--fn-radius-pill` | 999px | 仅状态点、短筛选 Chip、头像 |

圆角由 Theme/UI Pack 提供。禁止所有容器无差别使用同一大圆角；表格、日志和代码区必须保持工具属性与可读性。

### 5.7 边框、阴影与模糊

- 默认面板使用 `--ui-card-border`；
- 当前交互区使用 `--ui-color-border-strong` 或 Recipe 定义的选中标记；
- 内部分区优先使用底部分隔线，不再嵌套第二层 Card；
- 面板、Dialog、Drawer 和 Dropdown 的阴影来自 Component Token；
- UI Pack 可以使用 Flat、Bordered 或 Raised Recipe；
- 主按钮不应依赖大面积阴影才能被识别；
- 透明度和 `backdrop-filter` 由 Theme/UI Pack 控制，但不得降低文字和数据可读性。

### 5.8 图标

- Standard UI Pack 默认使用 Lucide Vue；其他 UI Pack 可通过 Icon Registry 替换图标体系；
- Provider 品牌图标可以继续使用项目现有 SVG；
- 普通图标默认 16px，导航/关键状态 18px～20px；
- 图标线宽保持一致；
- Icon-only Button 必须有 Tooltip 和 `aria-label`；
- 不使用 Emoji 作为正式状态图标；
- 空态插图优先使用简洁线性图形，不引入与产品不一致的卡通图库。
- 图标切换不能改变按钮可访问名称、状态语义和操作含义。

### 5.9 动效

| 动效 | Standard Recipe 参考时长 | 曲线 | 说明 |
|---|---:|---|---|
| Hover/颜色 | 120ms | ease-out | 按钮、行、导航 |
| Popover/Dropdown | 160ms | ease-out | 轻微位移 4px～6px |
| Drawer | 220ms | cubic-bezier(.2,.8,.2,1) | 不超过 24px 位移感 |
| Dialog | 180ms | ease-out | 缩放只允许 0.98 → 1 |
| 页面内容切换 | 160ms | ease-out | 轻微位移，不整页淡出 |
| 状态节点推进 | 240ms | ease-out | 只在状态真实变化时执行一次 |

禁止：

- 无限呼吸灯；
- 循环旋转的装饰元素；
- 卡片 Hover 大幅上浮；
- 页面路由大范围缩放；
- 让 Skeleton 和真实内容产生明显布局跳动。

`prefers-reduced-motion: reduce` 下关闭位移、缩放和非必要过渡，仅保留即时状态变化。

Theme/UI Pack 可以缩短动效或使用无动效 Recipe，不允许突破 Reduced Motion 约束或引入循环装饰动画。

## 6. 状态语言

### 6.1 状态层级

状态展示按四层组织：

1. **页面级**：整个功能不可访问、权限不足或首屏失败；
2. **区块级**：某个统计、列表、日志或依赖失败；
3. **对象级**：某个账号、任务、文件或规则的状态；
4. **操作级**：本次保存、重试、复制或提交的即时结果。

不得用一个全局 Toast 替代页面或对象状态。

### 6.2 标准状态表现

| 状态 | 视觉 | 必须包含 |
|---|---|---|
| Loading | Skeleton 或局部进度 | 正在加载什么；超过 8 秒给出说明 |
| Empty | 中性图标 + 标题 + 一句解释 | 有明确下一步时显示 CTA |
| Error | Danger Token 标记或错误图标 | 用户语言、重试、Trace ID（管理端） |
| Unavailable | Warning/Neutral Token 提示 | 不可用原因、依赖和修复入口 |
| Permission | 锁图标、中性面板 | 需要的权限或重新登录入口 |
| Success | Success Token 短反馈 | 完成对象、下一步或可查看入口 |
| Partial | Warning Token 状态 | 已完成部分、失败部分、是否可继续 |

### 6.3 Badge 使用限制

- Badge 只承载短状态或短分类；
- 一行最多出现 2 个高语义 Badge；
- 能力列表不应堆成一排彩色胶囊，应使用图标列表或分组说明；
- “正常”“可用”“成功”只有在确实需要用户判断时展示；
- 状态旁必须保留可读文本，不能只显示颜色点。

## 7. 管理端 App Shell

### 7.1 Standard Admin Layout 参考结构

```text
┌──────────── Sidebar 240 ────────────┬──────────────────────────────────────┐
│ 品牌                                │ Topbar 68                            │
│ 分组导航                            ├──────────────────────────────────────┤
│                                    │ Page Header                          │
│                                    │ 标题 / 说明 / 主操作 / 辅助操作      │
│                                    ├──────────────────────────────────────┤
│                                    │ Workspace                            │
│ NAS / 版本 / 用户                   │ 页面内容                             │
└────────────────────────────────────┴──────────────────────────────────────┘
```

Standard Layout Recipe 参考尺寸：

- Sidebar 展开宽度：240px；折叠宽度：72px；
- Topbar：68px；
- 页面 Header 最小高度：76px，不强制固定；
- 主内容左右内边距：桌面 28px，宽屏 32px；
- 主内容最大宽度不统一锁死，日志/表格可利用全宽；
- Sidebar 与主内容的分隔、表面和阴影由 Layout/Theme Token 决定。

UI Pack 还可以提供 `sidebar-compact`、`top-navigation` 或 `hybrid-navigation`。无论采用哪种 Layout Recipe，都必须保留品牌槽位、一级导航、全局状态、用户菜单、Page Header 和内容槽位。

### 7.2 Sidebar

导航分组建议：

```text
工作台
  概览

任务中心
  访客提交
  入库任务
  定时追更
  每日热榜

存储与媒体
  网盘入库
  Organizer
  媒体库

运行与日志
  rclone 日志

系统
  通知
  设置
```

规则：

- 一级路由以图标 + 文字呈现；
- 当前路由使用 Navigation Recipe 定义的 Indicator 和 Selected Surface，颜色来自 Primary Token；
- 非当前导航不得错误使用 Primary 强调；
- 数量只在需要处理时显示，如待审核、失败任务；零值默认隐藏；
- 折叠模式保留 Tooltip；
- Sidebar 型布局底部可显示部署/版本/用户摘要，但不放无关宣传标语；
- 导航组标题弱化，不与导航项同等醒目。

### 7.3 Topbar

Topbar 承担：

- 移动端菜单；
- 面包屑或当前业务域；
- 全局依赖状态入口；
- 访客入口；
- 用户菜单。

Topbar 不重复展示页面大标题。页面标题位于 Page Header。

### 7.4 Page Header

标准结构：

```text
Eyebrow / Breadcrumb（可选）
页面标题                        主操作
一句说明                        次要操作 / 更多
```

规则：

- 主操作最多 1 个；
- 次要操作最多直接显示 2 个，其余进入 More Menu；
- 筛选器通常位于 Header 下方的 Filter Bar，不和标题挤在同一行；
- 列表总数属于 Filter Bar 或列表标题，不做超大统计卡。

### 7.5 移动端管理壳

- Sidebar Recipe 在 `< 1024px` 进入 Drawer；Top Navigation Recipe 使用适合移动端的 Menu/Sheet；
- Topbar 保留菜单、页面简称和用户入口；
- Page Header 主操作可固定在内容顶部，但不得 `position: fixed` 遮挡内容；
- 表格优先转对象卡列表；
- Drawer/详情在手机全屏；
- 危险操作放在底部操作区，避免与返回键相邻；
- 不把桌面 Sidebar 缩成一排横向滚动 Tab。

## 8. 管理端页面规范

本节定义业务信息、主任务、必备状态和 Standard UI Pack 的参考布局。自定义 UI Pack 可以替换组件和布局呈现，但必须保留本节列出的业务信息、操作、URL 恢复、响应式和可访问性合同。

### 8.1 管理员登录页

#### 页面目标

快速、安全地进入管理端，并明确产品身份和安全状态。

#### 桌面布局

- 左侧约 56%：品牌背景、产品名称、三条能力摘要；
- 右侧约 44%：实体登录面板；
- 页面最大宽度约 1080px，垂直居中但不依赖固定视口高度；
- 小于 768px 只保留 Logo、标题、登录表单和必要说明。

#### 表单

- 用户名、密码、登录按钮；
- 字段错误就地显示；
- 登录失败保留用户名，不保留密码；
- 提交中按钮宽度不变；
- 失败超过一次仍不添加干扰性验证码，除非后端真实要求；
- 默认密码提醒在登录成功后进入管理端展示，不在登录页泄露账户策略。

#### 视觉验收

- 不像 Naive UI `NCard + NForm` 默认示例；
- 有稳定品牌区和清晰的安全提示；
- 320px 不横向溢出；
- 键盘焦点顺序为用户名 → 密码 → 登录。

### 8.2 Dashboard / 概览

#### 页面目标

用户进入后台 10 秒内能够回答：

1. 系统核心链路是否正常；
2. 是否有待审核、失败或卡住的任务；
3. 当前正在执行什么；
4. 下一步最值得处理什么。

#### 页面结构

```text
Page Header：早上好 / 最近刷新 / 刷新
Attention Strip：仅有异常时出现
4 个摘要指标：待审核、运行中、失败、今日完成
主区 8 列：最近任务列表
侧区 4 列：当前 Pipeline / 待处理事项
底区：系统链路健康 + 最近动态
```

#### 指标规则

- 只展示能引导操作的指标；
- 数字是主视觉，变化说明为辅助；
- 零失败使用中性或 Success Token 小状态，不制作高强调成功大卡；
- 点击指标跳转到带筛选条件的真实列表。

#### 最近任务

每行展示：

- 资源名称；
- Provider 与分类；
- 当前阶段；
- 更新时间；
- 进度或状态；
- 进入详情操作。

进行中任务使用单条细进度轨道，不显示多个彩色 Badge。

#### 系统健康

按链路展示：Web、Worker、Database、Provider、OpenList、rclone、Organizer。

- 健康：图标 + “正常”；
- 降级：说明仍可使用的能力；
- 失败：提供修复入口或日志入口；
- 单个依赖失败不得让整页变成 Error State。

#### 状态与刷新

- 首屏 Skeleton 与最终布局尺寸接近；
- 自动刷新时不覆盖整个页面；
- 更新失败保留旧数据并标注“数据更新失败”；
- 用户正在操作下拉菜单或文本选择时不强制重排。

### 8.3 Storage Setup / 网盘入库

#### 页面目标

让管理员以可理解的顺序完成：账号 → OpenList → 保存位置 → 路由 → 预检。

#### 页面布局

桌面采用“步骤轨道 + 对象工作区 + 上下文编辑器”：

```text
┌──────── 180 ────────┬──────────── 主工作区 ────────────┬──── 340 ────┐
│ 1 账号              │ 当前对象列表 / 目录树 / 路由矩阵 │ 编辑/详情   │
│ 2 OpenList          │                                   │ 字段/状态   │
│ 3 保存位置          │                                   │ 保存动作    │
│ 4 入库规则          │                                   │             │
│ 5 完整预检          │                                   │             │
└─────────────────────┴───────────────────────────────────┴─────────────┘
```

在 1024px～1279px 时右侧编辑器下沉为工作区内 Drawer；小于 768px 时步骤轨道变顶部横向步骤摘要，编辑器全屏。

#### 步骤轨道

每步显示：

- 序号和名称；
- 完成/需注意/阻断状态；
- 一句摘要，如“2 个账号，1 个已激活”；
- 当前步骤；
- 阻断后续步骤时明确原因。

步骤不是装饰性 Stepper，必须与路由和真实配置状态同步。

#### 账号工作区

Provider 账号使用对象卡，而不是普通表格。每张卡包括：

- Provider 图标和名称；
- 账号显示名/身份；
- 激活、禁用、认证失效等状态；
- 最近检测；
- 关键能力摘要；
- Test、登录、编辑、更多操作。

卡片选中后，右侧编辑器显示详细信息。添加账号使用“添加网盘账号”主按钮，不为每个 Provider 建一个颜色不同的大按钮。

#### OpenList 与保存位置

- Connection、Mount、Location 分别使用清晰对象层级；
- Connection 与 Mount 通过缩进/关联线表达，不做多个嵌套 Card；
- FolderTree 左侧显示树，右/下方显示已选路径、定位器和能力；
- 路径使用等宽字体并允许复制；
- Secret 只显示“已配置/未配置”，永不回显；
- 读取目录失败时在失败节点就地重试，不清空整个树。

#### 路由矩阵

- 行：来源或分类；
- 列：默认目标、覆盖目标、可执行性；
- 不可执行单元显示原因入口；
- 保存前显示变更摘要；
- 手机端转为“来源卡 + 目标选择”，不强行保留矩阵。

#### 完整预检

预检使用全宽结果区或大 Dialog，内容为：

- 顶部总结果：通过、部分通过、未通过；
- 分组：账号、OpenList、保存位置、写入/上传能力、路由；
- 每项显示名称、结果、耗时、说明；
- 失败项显示 Code、Trace ID 和修复入口；
- “重新预检”是主操作，“仍然保存”只有业务允许时出现且需说明风险。

#### Storage 视觉验收

- 用户始终知道自己在哪一步；
- 创建/编辑不通过多个叠加 Modal 完成；
- 对象、关联和错误位置清楚；
- 1280px 下三栏无文字挤压；
- 768px 下编辑器切换自然；
- 320px 下可完成账号登录、目录选择和预检查看。

### 8.4 Requests / 访客提交

#### 页面结构

- Page Header：标题、说明、可选刷新；
- Filter Bar：状态、Provider、分类、关键词、日期；
- 待审核快捷视图；
- 请求列表；
- 路由详情页或 URL 同步 Drawer。

#### 列表字段

桌面优先：资源名称、来源、提交人摘要、当前状态、提交时间、最后变化、操作。

规则：

- Token 不是主列，放在次要信息或详情；
- 待审核请求可突出，但不整行红色；
- Approve/Reject 不同时做成两个高强调按钮；
- 主操作按当前状态变化，例如待审核时“查看并审核”；
- 详情中显示资源、文件选择、计划摘要、状态历史和操作原因。

### 8.5 Jobs / 入库任务列表

#### 页面结构

- 顶部视图切换：全部、运行中、需处理、已完成；
- Filter Bar：关键词、状态、Provider、分类、时间；
- 列表/紧凑卡片；
- 批量操作条只在选中后出现。

#### 行设计

每行分三层：

1. 资源标题 + Provider；
2. 当前阶段 + 简短说明；
3. 时间、任务 ID 和可用操作。

进度表现：

- 已知离散阶段使用阶段轨道；
- 有真实百分比时显示进度条；
- 不知道百分比时不要伪造 60%；
- 等待状态用文案说明在等待什么；
- 失败显示失败阶段，不只显示“失败”。

### 8.6 Job Detail / 任务详情

#### 页面目标

让用户快速定位任务当前阶段、失败原因、执行证据和下一步动作。

#### 页面结构

```text
返回任务列表 / 任务 ID
资源标题                        Retry / Cancel / More
Provider · 分类 · 创建时间

阶段轨道：接收 → 计划 → 搬运 → 整理 → 入库完成

主区 8 列                          侧区 4 列
事件时间线                         执行摘要
错误与恢复                         Provider / Worker / rclone / Organizer
计划摘要                           关联请求 / Trace ID

底部：原始日志入口或关联运行
```

#### 阶段轨道

- 完成节点：Success Token + 勾选；
- 当前节点：Primary Token 实心节点 + 当前说明；
- 等待节点：中性空心节点；
- 失败节点：Danger Token + 失败符号；
- 跳过节点：灰色带“已跳过”；
- 轨道下只展示当前阶段核心文案，不把所有事件塞到轨道上。

#### 事件时间线

每项包含：

- 时间；
- 事件标题；
- 人类可读说明；
- 来源模块；
- 可展开技术细节。

默认折叠大段 JSON 和日志。技术详情使用等宽字体，可复制，Secret 脱敏。

#### 错误恢复

- 页面顶部错误摘要只在当前仍失败时出现；
- 显示失败阶段、错误说明、是否可重试；
- Retry 按钮说明重试范围；
- Cancel/Delete 必须区分影响；
- 执行操作后阶段轨道和时间线同步刷新。

### 8.7 Organizer

#### 页面目标

高效审核资源映射，清楚区分原文件、目标名称、变更状态和执行结果。

#### 桌面布局

```text
Task Header + 状态 + 主操作
批量操作栏（选中后出现）
┌──────────────────────── Mapping Workspace ─────────────────────────┐
│ 左：映射表 / 分组列表 70% │ 右：当前项检查器 30%                 │
└────────────────────────────────────────────────────────────────────┘
底部：运行历史 / 回滚记录
```

#### Mapping Table

列建议：

- 选择；
- 原始文件；
- 识别结果；
- 目标名称；
- 分类/季集；
- 校验状态；
- 操作。

规则：

- 文件名允许两行并支持 Tooltip/复制；
- 编辑目标名称使用明确编辑态，不让整表每格都常驻 Input；
- 脏行显示小圆点和“未保存”；
- 批量修改前显示影响条数和 Preview；
- 校验失败定位到具体字段；
- Apply、Skip、Rollback 使用不同确认文案和危险级别。

#### 移动端

- 映射表转为逐项审核卡；
- 顶部显示 `当前 3 / 18`；
- 上一个/下一个切换；
- 批量动作进入独立页面或底部 Sheet；
- 不在手机上展示 7 列横向表格。

### 8.8 rclone 日志中心

#### 页面结构

- 顶部运行状态条：服务状态、当前运行、Start/Stop；
- 二级视图：运行记录、文件事件、任务日志、WebDAV 配置；
- 左侧列表 + 右侧详情，或上列表下日志；
- Log Viewer 占主要空间。

#### Log Viewer

- 使用当前 Theme 下高对比、低干扰的实体日志表面，不依赖透明玻璃；
- 顶栏包含搜索、级别、自动滚动、复制和下载；
- 行号弱化；
- 时间、级别、模块、正文结构对齐；
- Error 行只高亮级别和左侧标记，不整行鲜红；
- 搜索命中使用低饱和黄色背景；
- 默认限制 DOM 行数并显示“已截断”；
- 自动滚动开启时，用户手动向上滚动应暂停并提示。

### 8.9 媒体库、追更、热榜和通知

#### 媒体库

- Library 对象列表；
- 当前刷新任务单独展示；
- 刷新历史使用时间线或表格；
- “刷新全部”是危险度中等的明确动作，不使用图标暗示。

#### 定时追更

- 订阅列表 + 详情编辑；
- 调度状态和下次运行时间放在 Header 下的状态条；
- TMDB 搜索结果使用内容卡，不混入配置表单；
- 路径快照和候选使用分区展示。

#### 每日热榜管理

- 左侧候选队列，右侧资源检查与处理；
- 搜索、入库、订阅、忽略属于当前候选的上下文操作；
- 不把每个动作都做成同等权重按钮。

#### 通知

- 渠道、事件规则、投递记录为三个路由或清晰子页；
- 渠道使用对象卡和编辑器；
- 规则使用事件 × 渠道矩阵；
- 手机端矩阵转为按事件分组的 Checkbox 列表；
- 投递记录使用列表 + 详情；
- Secret 只显示已配置状态。

### 8.10 设置中心

#### 信息架构

```text
个人与安全
站点与品牌
搜索与资源源
TMDB 与内容识别
AI 与代理
历史与维护
高级配置
```

#### 布局

- 左侧设置分区导航 220px；
- 中间表单最大宽度 760px；
- 右侧可选说明/状态 280px；
- 小屏使用页面内 Select 或折叠导航；
- 不使用一个数百字段长页面。

#### 保存行为

- 每个设置 Section 独立保存；
- 表单有脏状态；
- 离开前提醒；
- 保存条位于 Section 底部并保持可见，但不得遮挡字段；
- 成功后显示“已保存于 HH:mm”；
- 后端字段错误映射到对应字段；
- 导入配置先 Preview 再确认覆盖范围。

## 9. 访客端页面规范

本节同样是信息与交互合同，不是固定品牌效果图。Public Theme、Public Layout Recipe 和 Resource Renderer 可以独立定制，不要求与管理端使用相同组件皮肤。

### 9.1 Public Shell

访客端不复刻管理端 Sidebar，也不在一个巨大玻璃框内承载全部页面。

桌面结构：

```text
品牌 Header：Logo / 搜索 / 链接提交 / 每日热播 / 查询 / 管理员入口
内容背景：更轻的深蓝层与有限最大宽度
页面主区
简短 Footer：服务说明 / 隐私提示 / 版本（可选）
```

规则：

- Header 高度约 64px～72px；
- 内容最大宽度 1120px；
- 搜索主区最大阅读宽度 920px；
- 移动端导航使用 Menu/Drawer 或紧凑 Tab，不横向挤出屏幕；
- 管理员入口降为低强调链接；
- 背景、内容表面和 Header 使用 Public Theme/UI Pack Token，不要求与管理端同色或同一明暗模式。

### 9.2 访客搜索首页

#### 首屏

- 标题：直接说明“搜索并提交影视资源”；
- 副标题：说明支持网盘、磁链和自动入库；
- 搜索框为页面唯一主焦点；
- 搜索源使用两到三个明确选项，不放在大型说明卡中；
- 最近热播或使用提示只能出现在搜索区下方，不抢首屏焦点。

#### 搜索框

- 桌面高度 56px，手机 52px；
- 搜索按钮与 Input 形成一个完整控件；
- 输入过程中不自动发起昂贵搜索；
- Enter 提交；
- 空关键词就地提示；
- 搜索中按钮进入 Loading，已有结果可保留并标注刷新中。

#### 搜索结果

桌面建议使用纵向资源卡列表，而不是海报瀑布流。每项包含：

- 标题和可选年份；
- 来源/Provider；
- 资源摘要、大小或文件数；
- 更新时间/热度（有真实数据时）；
- 能否直接入库；
- “查看文件”或“选择资源”主操作。

卡片只显示决定选择所需的信息，不直接展示复杂技术字段。

### 9.3 资源详情与文件选择

桌面使用右侧 Drawer 或独立详情路由；手机使用全屏页面。

结构：

```text
资源标题 / 来源 / 基本信息
可用性提示
文件选择工具条：全选、清空、已选数量/大小
ResourceFileTree
底部确认区：分类、通知选项、提交
```

规则：

- 目录和文件层级清晰；
- Checkbox 的触控区域覆盖整行合理区域；
- 已选摘要持续可见；
- “未选择 = 整个分享”必须用明确文案说明；
- Provider 不支持选择时不显示伪文件树；
- 目录加载失败仅影响当前节点；
- 提交前显示真实选择摘要。

### 9.4 手动链接提交

采用三步结构，而不是一次显示所有复杂字段：

1. 输入链接与提取码；
2. 检测并预览资源；
3. 补充标题、分类、文件选择、通知并确认。

规则：

- Detect 是步骤 1 的主操作；
- 后端识别出的 Provider、标题和资源信息进入 Preview；
- 用户可修改的字段清楚标注；
- Captcha 只在真实需要时插入当前步骤；
- 验证码失败保留其他表单状态；
- 慢速入库使用二次确认，说明原因和预计行为，不用恐吓性红色。

### 9.5 提交确认

确认面板至少展示：

- 资源名称；
- 来源；
- 选中范围；
- 分类；
- 预计处理方式；
- 邮件通知地址（如启用）；
- 风险或慢速说明；
- 确认提交与返回修改。

手机端使用 Bottom Sheet 或全屏确认页，主按钮位于底部安全区域。

### 9.6 提交成功

成功页不是单一 Toast，必须显示：

- 成功图标和明确标题；
- 提交编号；
- 复制按钮；
- 当前第一阶段状态；
- “查看进度”和“继续搜索”；
- 最近提交仅存安全摘要，不存链接密码或敏感载荷。

### 9.7 每日热播

- 顶部只显示日期、更新时间和分类；
- 榜单使用紧凑内容卡；
- 排名是视觉辅助，不用巨大数字压过标题；
- 已可观看、可搜索、暂无资源使用不同文案；
- 点击“搜索资源”将标题带入搜索页；
- 空态说明今日数据是否尚未生成或确实无内容。

### 9.8 提交查询与状态页

#### 查询页

- 编号输入为主任务；
- 最近提交显示标题、编号后四位、状态和时间；
- 不在公共页面暴露内部错误和技术堆栈。

#### 状态详情

使用访客语言的阶段：

```text
已收到 → 正在准备 → 正在搬运 → 正在整理 → 已完成
```

特殊状态：

- 等待审核；
- 需要补充信息；
- 暂时失败，可稍后重试/联系管理员；
- 已拒绝；
- 已取消。

页面展示：标题、编号、当前状态、更新时间、阶段轨道、公开事件和下一步。内部 Provider Token、路径、Trace ID 默认不公开。

## 10. 响应式规格

### 10.1 断点行为矩阵

| 区间 | 管理端 | 访客端 |
|---|---|---|
| `>= 1536px` | Sidebar 240，12 列，三栏工作区可用 | 内容最大宽度保持，不无限拉伸 |
| `1280～1535px` | 标准桌面，三栏 Storage/Settings 可用 | 4 列或 3 列内容网格 |
| `1024～1279px` | Sidebar 可保持，右侧检查器按需下沉 | 2～3 列内容网格 |
| `768～1023px` | Sidebar Drawer，两栏降一栏/一主一辅 | 2 列或单列，Header 简化 |
| `480～767px` | 卡片列表、全屏详情、单列表单 | 单列，Bottom Sheet，触控优先 |
| `< 480px` | 最小 320px，操作进入 More，标题换行 | 单列，主按钮全宽，导航折叠 |

### 10.2 表格降级规则

只有以下两种方式：

1. 保留关键列并允许受控横向滚动；
2. 转为对象卡列表。

选择依据：

- 日志、矩阵和文件映射需要列对齐，可横向滚动；
- Requests、Jobs、Runs 等对象列表优先卡片化；
- 横向滚动容器只能有一层；
- 操作列不得滚出后完全不可达，可改为卡片操作菜单。

### 10.3 触控

- 主要按钮和输入最小高度 44px；
- Icon-only 触控区域最小 40px；
- Tree 行最小高度 40px；
- 相邻危险与安全操作至少间隔 8px；
- Bottom Sheet 兼容安全区；
- 不依赖 Hover 才能发现关键操作。

## 11. Naive UI 使用边界

### 11.1 原则

Naive UI 的价值是：

- 可访问的基础交互；
- Overlay、Focus Trap、Keyboard Navigation；
- Select、Date Picker、Virtual List、Data Table 等复杂能力；
- 稳定的 Vue 组件实现。

Naive UI 不负责决定：

- 页面信息架构；
- 品牌外形；
- 状态文案；
- 业务流程；
- 页面级响应式；
- 列表、时间线、Storage 工作区和 Public 搜索的最终样式。

基础组件必须保持稳定 Contract，外形由当前 Component Recipe 提供；复杂业务组件通过 Feature Renderer Registry 获取当前 UI Pack 实现。详细机制见 `11-theme-engine-and-ui-customization.md`。

### 11.2 组件映射

| 底层能力 | 项目组件/页面 | 定制要求 |
|---|---|---|
| `NButton` | `AppButton` | 自定义尺寸、渐进层级、Loading、防重复、危险语义 |
| `NInput` | `AppInput/AppTextarea` | 自定义表面、Focus、前后缀、错误和配置态 |
| `NSelect` | `AppSelect` | 统一菜单表面、空态、搜索、字段错误和泛型 |
| `NModal` | `AppDialog/ConfirmDialog` | 自定义 Header、Footer、错误区、移动全屏 |
| `NDrawer` | `AppDrawer` | 路由同步、详情布局、移动全屏 |
| `NDataTable` | `AppDataTable` | 项目列样式、空态、Loading、移动卡片降级 |
| `NTabs` | `AppTabs/路由子导航` | 只做局部视图；页面导航必须 Router 化 |
| `NSteps` | Storage/Job 阶段轨道 | 业务状态映射和外形完全定制 |
| `NTree` | `FolderTree/ResourceFileTree` | 懒加载、路径、错误、选择摘要完全定制 |
| `NDatePicker` | `AppDateRange` | 统一主题浮层、快捷范围和 URL 同步 |
| `NDropdown` | `AppActionMenu` | 项目菜单层级、危险动作分组 |
| `NTag` | `StatusBadge` | 稳定语义，不允许页面随意选色 |
| `NProgress` | Job/Upload Progress | 真实进度、阶段说明和未知进度处理 |
| `NAlert` | `InlineNotice/PageState` | 项目图标、标题、说明、动作和 Trace ID |
| `NSpin/NSkeleton` | `PageState` | 页面骨架和局部加载结构由项目定义 |
| `NNotification` | `AppToast` | 文案、持续时间、去重和错误边界统一 |

### 11.3 必须由项目或 UI Pack 定制的部分

以下组件不得只换 Theme Token 后直接交付，必须由 Standard UI Pack 提供参考实现，并允许后续 UI Pack 在稳定 Contract 下替换：

- Admin App Shell；
- Sidebar Navigation；
- Page Header；
- Dashboard Metrics 与 Pipeline；
- Storage Step Rail 和对象工作区；
- Provider Account Card；
- Job Stage Track；
- Job Event Timeline；
- Organizer Mapping Workspace；
- Log Viewer；
- Public Search Hero；
- Resource Card；
- Resource File Selection Summary；
- Request Status Track；
- Empty/Error/Unavailable 页面插图和布局。

## 12. 交互规范

### 12.1 页面导航

- 一级页面使用 Router；
- 列表筛选写入 Query；
- 详情使用独立路由或 URL 同步 Drawer；
- 页面刷新能够恢复当前路由、筛选和详情对象；
- 浏览器前进/后退可用；
- 不使用大量 `v-show` 将所有页面常驻 DOM。

### 12.2 筛选

- 关键词输入 300ms～500ms Debounce，或明确点击搜索；
- Select 改变可立即筛选；
- 当前筛选以简短 Chip 展示并可逐项清除；
- “重置”只在确实存在非默认条件时显示；
- 筛选更新时保留列表区域尺寸，避免页面跳动；
- 总数和当前范围清楚可见。

### 12.3 写操作

- 主按钮 Pending 时锁定重复提交；
- 写操作不默认自动重试；
- 删除、取消、回滚、停止、覆盖导入必须确认；
- Confirm 文案说明对象、影响和可恢复性；
- 成功后优先局部更新/失效 Query；
- 失败保留用户输入和选中状态。

### 12.4 轮询

- 仅在运行态轮询；
- 页面不可见时降低频率或暂停；
- 终态自动停止；
- 轮询错误保留最后成功数据；
- 明确显示“最后更新于”；
- 用户手动刷新不创建第二条并发轮询链。

### 12.5 复制

可复制内容包括：任务 ID、请求编号、Trace ID、路径、日志片段。

- 复制成功用短 Toast；
- 复制按钮有文本或 Tooltip；
- Secret、Token、Cookie 不提供复制；
- 长路径默认省略中间部分，但复制完整值。

## 13. 文案规范

### 13.1 原则

- 先说结果，再说原因，再给下一步；
- 使用用户理解的业务词，不直接显示内部枚举；
- 按钮使用动词 + 对象，如“重新检测账号”“取消任务”；
- 避免“操作失败”“参数错误”这种无信息文案；
- 管理端可以显示错误码和 Trace ID，但仍需有人类可读说明；
- 访客端不显示内部堆栈、数据库信息或 Provider Secret。

### 13.2 示例

错误：

```text
无法读取夸克目录
账号登录状态已失效，请重新登录后再试。
错误码：PROVIDER_AUTH_EXPIRED · Trace ID：xxxx
[重新登录] [重试]
```

部分成功：

```text
预检完成，2 项需要处理
账号和保存位置可用，但 Organizer 暂时无法写入目标目录。
[查看失败项] [重新预检]
```

访客提交成功：

```text
资源已提交
提交编号 RQ-260810-8K3P，可随时查询处理进度。
[查看进度] [复制编号]
```

## 14. 可访问性

- 页面只有一个主 `h1`；
- 标题层级连续；
- 导航当前项使用 `aria-current="page"`；
- Tab 只用于同一路由内局部内容；
- Dialog、Drawer 和 Bottom Sheet 管理焦点并恢复触发点；
- 表单错误与字段关联；
- Tree、Menu、Select 使用标准键盘行为；
- Timeline、状态轨道提供可读文本替代；
- 图标按钮有名称；
- 动态结果使用适当 `aria-live`；
- 正文对比度达到 WCAG AA；
- Focus Ring 清晰，不被 Overflow 裁剪；
- 200% 缩放下主要路径仍可完成；
- 320px 宽度无不可控横向溢出；
- `prefers-reduced-motion` 生效；
- Playwright + axe 扫描登录、Dashboard、Storage、Job Detail、Public Search、Submit Confirm 和 Request Status。

## 15. 前端实现结构

### 15.1 样式目录

```text
frontend/src/ui/
  contracts/              # Theme、Recipe、Layout、Renderer、UI Pack 合同
  runtime/                # Registry、Resolver、Bootstrap、Provider
  themes/                 # 内置与自定义 Theme
  recipes/                # 基础组件皮肤
  layouts/                # Admin/Public/Workspace Layout Recipe
  renderers/              # 默认复杂业务组件 Renderer
  packs/                  # 可选择 UI Pack

frontend/src/styles/
  reset.css               # 最小 Reset
  semantic.css            # Runtime 输出的语义变量桥接
  typography.css          # 排版结构，不写固定品牌颜色
  utilities.css           # 少量稳定布局工具，禁止发展成第二个 Tailwind
  motion.css              # 过渡与 reduced-motion
```

Feature 样式建议：

```text
features/jobs/
  components/
    JobStageTrack.vue
    JobStageTrack.module.css
```

规则：

- 页面样式跟随 Feature；
- 只有稳定跨 Feature 规则进入 `styles/`；
- 禁止重建一个 10000 行全局 `product.css`；
- 禁止在模板中写行内 Style；
- 页面禁止硬编码视觉颜色；
- CSS Module 或 Scoped CSS 中仍必须使用 Semantic/Component Token；
- Feature 不得判断 Theme ID 或 UI Pack ID；
- Renderer 通过 Registry 注入，不从 Feature 直接 import Pack 私有组件。

### 15.2 页面骨架组件

建议提供：

- `AdminAppShell`；
- `AdminSidebar`；
- `AdminTopbar`；
- `PageHeader`；
- `PageToolbar`；
- `FilterBar`；
- `SplitWorkspace`；
- `DetailInspector`；
- `PublicAppShell`；
- `PublicHeader`；
- `PublicContent`；
- `StickyActionBar`（仅内容内粘性，不遮挡页面）。

骨架组件只处理结构，不直接请求业务数据。

UI Pack 可以通过 Layout Recipe 替换骨架组合，但必须实现相同 Slot Contract。

### 15.3 动态 Theme Overrides

Theme Runtime 根据当前 Theme 和 Component Recipe 动态生成 Naive UI Overrides，至少统一：

- 字体与字号；
- Primary/Success/Warning/Error；
- Input、Select、DatePicker 的高度、表面、边框和 Focus；
- Button 四个层级；
- Dropdown、Popover、Dialog、Drawer 表面；
- Table Header、Row、Hover、Selected、Border；
- Checkbox、Radio、Switch；
- Pagination；
- Tooltip；
- Scrollbar。

Theme Overrides 不应包含具体 Feature Selector，也不得维护一份只适配默认暗色的固定对象。

## 16. AI 页面开发流程

AI 实现任一页面时必须按以下顺序：

1. 阅读本文件对应页面章节；
2. 读取旧页面，列出必须保持的行为；
3. 确认路由、API、服务端状态和写操作；
4. 写出页面信息结构，不先堆组件；
5. 识别复用组件与 Feature 组件；
6. 判断定制应落在 Theme Token、Component Recipe、Layout Recipe 还是 Feature Renderer；
7. 确认组件 Contract 不依赖具体 Theme/UI Pack；
8. 先完成 Desktop 主任务路径；
9. 同一任务内完成 Loading、Empty、Error、Unavailable 和 Success；
10. 完成 1024px、768px、480px、320px 重排；
11. 加入键盘、ARIA、Reduced Motion 和 High Contrast；
12. 使用 Fixture 截取 Reference Light/Dark 视觉基线；
13. 运行组件测试、Contract Test、E2E、axe 和视觉回归；
14. 对照本文件与 `11-theme-engine-and-ui-customization.md` 验收，不以“功能能点”为完成标准。

AI 不得：

- 先把所有页面写完，再统一补响应式；
- 使用 Naive UI 默认 Demo 布局作为页面；
- 为了快速完成把所有内容包进 `NCard`；
- 将旧 HTML 字符串原样放入 Vue；
- 用随机假数据填满 Dashboard；
- 省略错误态和不可用态；
- 因移动端复杂而只添加横向滚动；
- 在没有真实进度时伪造百分比；
- 以“后续再定制”为由绕过 Theme、Recipe 或 Renderer 合同。
- 为不同主题复制整份页面或业务组件；
- 在 Feature 中使用 `if (themeId === ...)` 改变业务逻辑。

## 17. 视觉验证基线

### 17.1 必须保存的截图

每个核心页面至少保存：

- 1440 × 900 默认态；
- 1024 × 768 默认态；
- 768 × 1024 平板态；
- 390 × 844 手机态；
- 320 × 720 最小宽度；
- 至少一个 Loading；
- 至少一个 Empty 或 Unavailable；
- 至少一个 Error；
- 有 Dialog/Drawer 的页面需保存 Overlay 状态。
- Reference Light 和 Reference Dark 至少各有一组完整核心页面；
- High Contrast 覆盖登录、导航、表单、Job Detail 和 Public Submit；
- 每个新增 UI Pack 使用其主 Theme 保存核心组件与核心页面。

建议目录：

```text
frontend/tests/visual/
  themes/
    default-light/
    default-dark/
    high-contrast/
  ui-packs/
    standard/
  admin-dashboard/
  admin-storage/
  admin-job-detail/
  admin-organizer/
  admin-rclone/
  public-search/
  public-resource-detail/
  public-request-status/
```

### 17.2 视觉回归策略

- Playwright 使用稳定 Fixture、固定时间和固定时区；
- 关闭非必要动画；
- Skeleton 与真实内容分别截图；
- 动态 ID、时间和进度通过 Fixture 固定；
- 字体在 CI 中固定或使用系统字体回退基线；
- 不因整个页面像素变化就盲目更新 Snapshot；
- 更新基线时在任务报告说明视觉变化原因。
- 不执行所有 Theme × UI Pack 的无意义笛卡尔积，按 `11-theme-engine-and-ui-customization.md` 的覆盖矩阵执行。

### 17.3 人工检查清单

每页检查：

- 3 秒内能否识别页面主任务；
- 主操作是否唯一且位置稳定；
- 标题、工具栏、内容层级是否清楚；
- 是否出现“卡片套卡片”；
- 是否使用过多彩色 Badge；
- 错误是否靠近出错对象；
- 空态是否给出真实下一步；
- 表格/树/日志是否可读；
- 320px 是否仍能完成核心操作；
- 键盘焦点是否可见；
- 页面是否明显像默认 Naive UI；
- 管理端与访客端是否既统一又有明确差异。
- 切换 Light/Dark 后是否仍然可读；
- 切换 Component Recipe 后按钮、输入、表格和 Dialog 是否保持合同；
- UI Pack 是否只改变呈现，没有改变业务行为。

## 18. 页面完成定义

一个页面只有同时满足以下条件才可标记完成：

1. 旧版对应核心行为已迁移或明确记录差异；
2. 页面结构符合本文件；
3. 使用项目 Wrapper、Semantic Token 和 Component Token；
4. 没有大面积直接暴露 Naive UI 默认样式；
5. 主任务和主操作清楚；
6. Loading、Empty、Error、Unavailable、Success 完整；
7. 异步写操作防重复并有结果反馈；
8. URL 可恢复筛选或详情状态；
9. 桌面、平板、手机布局完成；
10. 320px 无不可控横向溢出；
11. 键盘和 ARIA 可用；
12. 组件/Feature 测试通过；
13. 核心路径 Playwright 通过；
14. axe 无严重问题；
15. 视觉截图已生成并人工检查；
16. 中文文案为 UTF-8 常量；
17. 无 Secret、Token、内部堆栈泄露；
18. 页面代码未重新演变成巨型单文件组件。
19. Reference Light/Dark 下均可用；
20. Feature 不判断 Theme/UI Pack ID；
21. 可替换复杂组件已实现 Renderer Contract 和 Contract Test；
22. Theme 或 UI Pack 切换不会丢失页面状态。

## 19. 明确禁止的视觉反模式

- 所有页面都是“标题 + 四张统计卡 + 一张大表”；
- 每个小区块都有圆角、边框、阴影和渐变；
- 所有状态都做成彩色胶囊；
- 同一高饱和颜色描边覆盖所有面板；
- Sidebar、Topbar、页面内容同时使用强玻璃效果；
- 透明表格叠在复杂渐变背景上；
- 主按钮、成功按钮和普通按钮都使用同等高强调效果；
- 使用巨大图标和空白填充不存在的信息；
- 移动端只把桌面页面整体缩小；
- Dialog 中再打开 Dialog；
- 设置页无限向下堆字段；
- FolderTree、Timeline、LogViewer 直接使用组件库默认样式；
- 将后台技术错误原样展示给访客；
- 使用假百分比、假健康分数或无业务来源的趋势图；
- 为“科技感”加入无限动画、扫描线、粒子背景或过量霓虹。
- 为每个 Theme 复制一套页面；
- 通过全局 `!important` 覆盖实现 UI Pack；
- 允许主题配置注入任意 CSS、HTML 或 JavaScript；
- Component Recipe 修改表单值、业务状态或 Mutation 行为。

## 20. 最终设计验收问题

最终切换生产入口前，产品、开发或 AI Review 必须能明确回答：

1. 这套页面和组件是否形成项目自己的合同，而不是 Naive UI 示例？
2. Dashboard 是否真的能指导处理问题，而不是装饰性数据面板？
3. Storage 是否让账号、连接、位置、路由和预检关系变得更清楚？
4. Job Detail 是否能定位失败阶段并完成恢复？
5. Organizer 是否比旧版更适合批量审核和修改？
6. 日志中心是否能处理大日志且不牺牲可读性？
7. 设置是否按职责拆分并能安全保存？
8. 访客是否能用最短路径完成搜索、选择和提交？
9. 手机端是否是真实重排，而不是缩小桌面？
10. 所有异常是否都有用户可理解的原因和下一步？
11. 页面是否在不牺牲可维护性的前提下适配多 Theme 和 UI Pack？
12. Button、Input、Card、Table、Dialog 和复杂业务组件是否可以在稳定合同下定制？
13. 管理端与访客端是否可以分别选择 Theme 和 Layout Recipe？
14. 后续 AI 新增页面是否可以仅凭本文件、Theme/UI Pack 合同和 Component Gallery 继续保持一致？

只有这些问题都能以代码、截图或测试证据回答“是”，本轮定制 UI 才算真正完成。
