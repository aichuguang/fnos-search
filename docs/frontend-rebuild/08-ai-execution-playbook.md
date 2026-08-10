# 08｜AI 执行手册

## 1. 使用对象

本手册面向负责实际编码的 AI Agent。目标是避免 AI 看到“重构前端”后：

- 一次生成大量无法验证的代码；
- 随意更换技术栈；
- 为前端方便修改后端核心业务；
- 覆盖用户已有未提交改动；
- 把旧巨型脚本原样翻译成巨型 Vue 文件；
- 只实现 Happy Path；
- 没有测试却宣称完成。

## 2. 开始任务前必须阅读

所有任务：

1. `AGENTS.md` 指令；
2. `docs/frontend-rebuild/README.md`；
3. 本文件；
4. `09-task-board.md` 中当前任务；
5. 当前任务对应的架构、组件、API、迁移和测试章节。

只要任务涉及页面、基础组件、业务组件、主题、布局、品牌资源或外观设置，额外阅读：

- `10-custom-ui-visual-spec.md`；
- `11-theme-engine-and-ui-customization.md`。

涉及 Storage 时额外阅读：

- `docs/V1.0多链路设计文档.md`；
- `docs/storage-backend-refactor.md`；
- `verification.md` 最新相关章节。

涉及部署时额外阅读：

- `README.md` 部署部分；
- `docs/v1-hard-cutover-deploy.md`；
- `Dockerfile`；
- `docker-compose.yml`；
- `compose.dev.yaml`。

## 3. 每次任务的标准工作流

### 步骤 A：保护现场

执行：

```bash
git status --short --branch
git diff --stat
```

要求：

- 识别任务开始前已有改动；
- 不覆盖、不回退、不格式化无关文件；
- 当前工作区包含用户改动时，按文件和 Hunk 区分；
- 发现同一文件存在用户改动时先阅读差异，再进行最小补丁。

### 步骤 B：上下文扫描

必须回答：

- 当前 Feature 的旧入口在哪里？
- 使用哪些 API？
- 现有测试覆盖哪些行为？
- 新代码应该落在哪个目录？
- 是否需要后端最小改动？
- 如何验证 Loading/Empty/Error/Success？
- 如何回滚？

停止条件：已经能明确“做什么、改哪里、怎么验证、不能改什么”。

### 步骤 C：计划

复杂任务使用 `update_plan`，计划必须包含：

- 契约/类型；
- 组件或页面；
- API 接入；
- 测试；
- 构建；
- 文档/任务状态。

一次执行完当前任务再汇报，不在完成一个小组件后要求用户确认。

### 步骤 D：实现

顺序建议：

```text
类型和契约
  -> API Adapter
  -> Query/Mutation
  -> UI/Composable
  -> Page
  -> Tests
  -> Build/Integration
```

不要先写完整页面再补类型和请求层。

### 步骤 E：验证

根据任务执行最小充分组合：

```bash
cd frontend
npm run lint
npm run typecheck
npm run test:run -- <scope>
npm run build
```

涉及后端：

```bash
python -m pytest -q <related tests>
python -m ruff check <related files>
```

集成阶段还需 Playwright、Compose 和健康检查。

### 步骤 F：审查 Diff

执行：

```bash
git diff --check
git diff --stat
git status --short
```

检查：

- 无无关文件；
- 无大面积换行变化；
- 无 Secret；
- 无临时日志；
- 无跳过测试；
- 无旧全局依赖；
- 中文 UTF-8 正常。

### 步骤 G：交付

报告必须包含：

- 完成功能；
- 关键文件；
- API/后端改动；
- 测试与构建结果；
- 未执行真实外部验证；
- 风险和回滚；
- Task Board 状态。

## 4. 强制编码规则

### 4.1 文件和编码

- UTF-8 无 BOM；
- LF；
- 中文直接写入 TypeScript/Vue/Markdown UTF-8 常量；
- 不用 PowerShell 字符串拼接覆盖中文文件；
- 优先 `apply_patch`；
- 生成文件由对应生成命令产生。

### 4.2 Vue

- Vue 3 Composition API；
- `<script setup lang="ts">`；
- Props、Emits 类型化；
- 不使用 Options API 混写；
- 不在页面直接操作 DOM；
- 不使用 `window.*` 共享业务模块；
- 不创建全局 Event Bus；
- 不在一个 SFC 中实现多个独立业务流程；
- 不通过 `v-html` 渲染后端文本。

### 4.3 API

- 无直接 `fetch`；
- 无 endpoint 字符串散落；
- 通过 Feature API；
- Mutation 不自动重试；
- 所有请求可取消；
- 错误保留 Trace ID；
- Secret 不记录；
- 未配置 Provider 视为正常不可用态。

### 4.4 状态

- API 数据进入 Vue Query；
- 布局/偏好进入 Pinia；
- 表单和目录展开留在局部；
- 筛选/分页进入 Router；
- 不建立 `useAppStore()` 巨型 Store；
- 不复制 Query 数据到 Pinia。

### 4.5 组件

- 先使用 `components/ui`；
- Feature 组件留在 Feature；
- 不创建万能表格/万能表单；
- 第三方 UI 通过 Wrapper；
- 组件包含所有状态；
- 公共组件必须有测试和 Gallery 示例；
- Button、Input、Card、Table、Dialog、Navigation 等基础外形进入 Component Recipe，不在页面写主题特例；
- Provider Card、Job Timeline、Organizer Mapping、Resource Card/File Tree 等复杂呈现通过稳定 Renderer Contract 注入；
- Feature 不读取 Theme ID 或 UI Pack ID，也不复制 Light/Dark/自定义版页面；
- 完整 UI Pack 只替换呈现，不复制 API、Query、Mutation、权限或业务状态机。

## 5. 后端改动规则

AI 在前端任务中发现后端问题时，按以下顺序处理：

1. 确认是否可以由前端 Adapter 安全兼容；
2. 如果是后端 5xx、权限错误、信息缺失或规范缺失，做最小后端修复；
3. 增加后端回归测试；
4. 不修改数据库和核心状态机；
5. 在交付中明确说明后端改动原因。

未经用户明确要求，禁止：

- 新建 `/api/v2`；
- 改成 JWT；
- 替换 Flask；
- 修改 Provider 执行；
- 修改 v29 Schema；
- 修改 Job 状态含义；
- 删除旧 API 路径。

## 6. AI 不得采用的捷径

- 把旧 HTML 整段放进 Vue Template 后继续使用 `document.querySelector`；
- 在 `onMounted` 中加载旧脚本；
- 用 `window.legacyState` 临时共享并长期保留；
- 把旧 `admin.js` 拆成多个无边界 `utils`；
- 所有请求都放入 `api.ts` 一个文件；
- 所有页面都放入一个 Pinia Store；
- 为通过 TypeScript 使用大量 `as any`；
- 为通过测试删除失败断言；
- 为解决 CSRF 关闭安全检查；
- 为解决跨域开启通配 CORS；
- 为解决路由刷新把所有 404 返回 200，包括 `/api`；
- 未执行测试却写“已验证”。

## 7. 任务粒度

合适任务：

- 建立 API Client 与错误模型；
- 完成基础 Dialog 组件和测试；
- 完成 Storage FolderTree 和测试；
- 完成 Provider Accounts 页面；
- 完成 Jobs Detail 页面；
- 补齐一组 OpenAPI 路由；
- 集成 Docker Frontend Build。

不合适任务：

- “重构所有前端”；
- “把所有组件都做完”；
- “一次迁移所有后台页面”；
- “顺便优化后端架构”。

一个任务应能在一次较长执行中完整实现、验证和交付，并且不会依赖十个同时未完成的分支。

## 8. 分支与提交建议

若用户要求创建分支，默认使用：

```text
codex/frontend-foundation
codex/frontend-storage
codex/frontend-jobs
```

提交建议按可回归结果划分：

```text
fix(api): keep public config available without optional providers
feat(frontend): scaffold Vue application and API client
feat(frontend): add shared dialog and form components
feat(storage-ui): migrate provider accounts workspace
```

不要把后端 Bug、Vue 工程、Storage 页面和 Docker 切换压进一个提交。

## 9. 标准提示词模板

### 9.1 领取单个任务

```text
请执行 docs/frontend-rebuild/09-task-board.md 中的任务 <TASK-ID>。

要求：
1. 先阅读 docs/frontend-rebuild/README.md、08-ai-execution-playbook.md，以及任务引用的章节。
2. 检查并保护当前未提交改动。
3. 一次完成实现、测试、构建和文档更新，不在小步骤后询问确认。
4. 不扩大后端改动，不修改数据库或任务状态机。
5. 所有中文源文件保持 UTF-8 无 BOM。
6. 最终报告修改文件、验证命令、结果、风险和回滚方式。
```

### 9.2 执行一个阶段

```text
请完整执行 docs/frontend-rebuild/05-migration-roadmap.md 的阶段 <N>。

阶段内按 09-task-board.md 的依赖顺序逐项完成。每项必须通过测试后再继续，但不要中途向我请求低风险确认。保持当前业务语义和 API 路径，发现后端阻断问题时只做最小修复并补测试。
```

### 9.3 代码审查

```text
请根据 docs/frontend-rebuild/02-target-architecture.md、03-design-system-and-components.md、04-api-contract-and-backend-boundary.md、06-testing-and-quality-gates.md、10-custom-ui-visual-spec.md 和 11-theme-engine-and-ui-customization.md 审查当前前端改动。

重点检查：Feature 边界、直接 fetch、Pinia 滥用、组件重复、Theme/Recipe/Renderer/UI Pack 职责串层、业务代码判断具体主题、Mutation 重试、CSRF、错误态、移动端、可访问性、测试和旧前端依赖。只报告有证据的问题，并给出文件和行号。
```

## 10. 任务执行记录

每个 Task 完成后，在 `09-task-board.md` 对应任务下追加：

```text
完成日期：
提交/工作树：
主要文件：
测试：
未验证：
```

不要把 Task Board 变成流水账，只记录结果和剩余风险。

## 11. AI 自查问题

提交前必须自问：

1. 我是否创建了新的巨型文件？
2. 页面是否直接知道 API 路径？
3. 是否把 API 数据复制进 Pinia？
4. 是否遗漏 Loading/Empty/Error？
5. Mutation 是否可能重复？
6. 401/403/409/500 如何表现？
7. 手机和键盘是否可用？
8. 是否泄漏 Secret 或原始 Provider Payload？
9. 是否覆盖用户原有改动？
10. 是否执行了我声称通过的测试？
11. 是否存在 CRLF/乱码？
12. 是否能用上一镜像或 Git 回退？

任何答案不明确，都不能标记完成。

## 12. 最终交付示例

```text
完成 FE-201 Provider Accounts 页面：

- 新增 Provider Catalog、账号列表、检测、激活、禁用和删除 UI。
- 新增 provider-api.ts 和后端响应 Adapter；页面无直接 fetch。
- 复用 AppCard、StatusBadge、AsyncActionButton 和 ConfirmDialog。
- 后端未修改数据库；补充了缺失 OpenAPI Schema。

验证：
- npm run lint：通过
- npm run typecheck：通过
- npm run test:run -- storage：18 项通过
- npm run build：通过
- python -m pytest -q tests/test_accounts_v1.py：通过

未验证：
- 未使用真实 Provider 账号执行外部登录。

风险/回滚：
- 仅新增 Preview 前端入口，正式旧入口未切换；删除新增前端文件即可回滚。
```

## 13. 阻塞处理

只有以下情况才需要用户输入：

- 必须选择 React/Vue 等已锁定决策发生冲突；
- 需要破坏性数据库变更；
- 需要真实生产账号或生产数据操作；
- 需求存在互斥产品语义且无法从代码/文档判断；
- 需要 Git Push 或生产发布。

普通组件命名、目录拆分、测试 Fixture、低风险依赖安装和本地构建不应频繁询问用户。
