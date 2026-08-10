# 06｜测试与质量门禁

## 1. 原则

- 测试必须覆盖主要风险，不追求无意义的百分比。
- 前端重构不能降低现有后端回归质量。
- 页面测试以用户行为和 API 契约为主，不断言大量内部实现。
- Provider、目录树、选集、Mutation 和轮询必须覆盖失败路径。
- 每阶段必须通过对应门禁后才能进入下一阶段。

## 2. 测试分层

```text
工具/适配器单元测试
  -> UI 组件测试
  -> Feature 集成测试（MSW）
  -> Flask API 契约测试
  -> Playwright E2E
  -> 类生产真实链路冒烟
```

## 3. 单元测试

适合测试：

- API 错误解析；
- Query 参数编码；
- 后端响应到 ViewModel 的 Adapter；
- 状态 Label/Tone 映射；
- Provider Manifest 解析；
- 目录节点转换；
- 资源选集转换；
- 日期格式；
- 路由 Query 解析；
- Zod Schema；
- Pinia Store 的有限本地状态；
- 轮询间隔策略。

必须覆盖：

- `code` 与 `error_code`；
- 非 JSON 错误；
- 204；
- 401/403/409/429/500；
- `field_errors`；
- `trace_id`；
- 未知 Provider；
- 缺失可选字段；
- 非法日期和分页参数。

## 4. UI 组件测试

### 基础组件

每个公共组件至少测试：

- 默认渲染；
- Disabled；
- Loading；
- 键盘；
- ARIA；
- 错误状态；
- Emit/回调；
- Slot；
- `standard`、`compact` 等 Component Recipe 下 Props/Events/Slots 与键盘合同一致；
- Light、Dark、High Contrast 下可读且 Focus 状态存在。

### Theme、Recipe 与 Renderer Contract

必须测试：

- Theme Schema、缺失 Token、非法值和对比度门禁；
- System Theme 监听与解析优先级；
- Theme/UI Pack 切换不清表单、筛选、Drawer、路由和 Query Cache；
- 未安装 Theme/UI Pack 的 fallback；
- Component Recipe 不改变业务事件、表单值和 Focus Trap；
- Provider Card、Job Timeline、Organizer Mapping、Resource Card/File Tree、Request Status Track 运行同一 Renderer Contract Test；
- UI Contract Version 不兼容时阻止加载或安全回退。

### FolderTree

必须测试：

- 根节点加载；
- 展开子节点；
- 加载中防重复；
- 请求失败；
- 节点级重试；
- 选择 ID；
- 选择 Path；
- 键盘展开/选择；
- Abort；
- 空目录；
- 已选路径回显。

### Dialog

必须测试：

- 打开后焦点；
- ESC；
- Confirm Loading；
- Confirm Error；
- 关闭后恢复焦点；
- 危险确认文案；
- 移动端模式可通过类/结构断言。

## 5. Feature 集成测试

使用 MSW 模拟真实接口，测试完整 Feature 行为。

### Storage

- Catalog -> 创建账号 -> Auth Session -> 成功；
- Captcha 错误 -> 新验证码 -> 成功；
- Account Test 失败；
- 目录树加载；
- 创建保存位置；
- 409 被引用对象不可删除；
- Preflight 部分失败；
- Provider 空间不足；
- Session 失效。

### Jobs

- 筛选、分页和 URL 同步；
- 查看详情；
- Running 轮询；
- Retry；
- Cancel Confirm；
- Mutation 失败；
- Trace ID 展示。

### Public Submit

- 搜索；
- 资源详情；
- 文件选择；
- Manual Detect；
- Captcha；
- Submit；
- Slow Confirm；
- Request Token 保存；
- 后端禁止字段不进入 Payload。

## 6. API 契约测试

后端继续使用 pytest。新增：

### 6.1 路由/OpenAPI 对齐

```text
真实 app.url_map
  -> 规范化路径参数
  -> 排除 static/internal 白名单
  -> 与 OpenAPI paths+methods 比较
  -> 缺失即失败
```

### 6.2 标准错误字段

关键接口失败响应至少验证：

- HTTP 状态；
- `success=false` 或明确错误结构；
- `code`/兼容 `error_code`；
- `message`；
- 5xx `trace_id`；
- 表单错误 `field_errors`。

### 6.3 Session/CSRF

- 未登录管理接口 401；
- 写接口缺 CSRF 403；
- 登录/退出豁免符合当前后端设计；
- Session 接口返回 Token；
- 前端静态路由不绕过 API 权限。

### 6.4 公共配置

至少组合测试：

- 四 Provider 都未配置；
- 只有 Quark；
- 只有 Mobile/Tianyi；
- SixPan 未激活；
- importer resolve 失败；
- 接口仍返回 200 和不可用状态。

## 7. Playwright E2E

### 7.1 测试模式

两类：

1. **Mock E2E**：稳定覆盖页面流程；
2. **Local Integration E2E**：运行真实 Flask 临时数据库，不访问真实 Provider。

真实 Provider 流程属于发布冒烟，不放入普通 CI。

### 7.2 管理端核心场景

#### AUTH-01 登录

```text
访问 /admin/overview
  -> 跳转 /admin/login
  -> 输入账号密码
  -> 登录成功
  -> 回到目标页
  -> Session 刷新仍登录
  -> 退出
```

#### STORAGE-01 配置主流程

```text
登录
  -> Storage
  -> 添加 Provider 账号
  -> 完成 Mock Auth
  -> 检测并激活
  -> 添加 OpenList
  -> 浏览目录
  -> 创建保存位置
  -> 创建规则
  -> 完整预检通过
```

#### JOB-01 任务管理

```text
打开 Jobs
  -> 筛选 Running
  -> 打开详情
  -> 查看 Timeline
  -> 取消
  -> 确认
  -> 状态刷新
```

#### ORGANIZER-01 人工整理

```text
打开 Organizer Task
  -> 修改 Mapping
  -> 批量修改
  -> Approve
  -> Apply
  -> 查看 Run
```

#### SETTINGS-01 Secret 设置

```text
打开通知/高级设置
  -> Secret 显示 configured 而非原值
  -> 更新
  -> 测试
  -> 保存成功
```

### 7.3 访客端核心场景

#### PUBLIC-01 搜索并提交

```text
访问 /submit/search
  -> 输入关键词
  -> 选择结果
  -> 展开文件
  -> 勾选
  -> 确认分类
  -> 提交
  -> 获得 Request Token
  -> 跳转状态页
```

#### PUBLIC-02 手动链接与验证码

```text
访问 /submit/link
  -> 输入链接
  -> 预览
  -> Captcha 错误
  -> 刷新 Captcha
  -> 提交成功
```

#### PUBLIC-03 最近提交

```text
提交成功
  -> 本地记录公开 Token
  -> 打开查询页
  -> 点击最近提交
  -> 正确显示状态
```

#### PUBLIC-04 可选 Provider 未配置

```text
SixPan 未激活
  -> /api/public/config 200
  -> 页面正常打开
  -> SixPan 显示不可用
  -> 其他来源仍可使用
```

## 8. 可访问性测试

### 自动化

Playwright + axe 扫描：

- Login；
- Admin Layout；
- Storage；
- Jobs List/Detail；
- Dialog；
- Public Search；
- Public Submit Confirm；
- Request Status。

阻断等级：

- critical：必须修复；
- serious：必须修复或记录明确例外；
- moderate/minor：按阶段评估。

### 手工检查

- Tab 顺序；
- Dialog 焦点；
- Sidebar 键盘；
- FolderTree 键盘；
- 屏幕阅读器可读 Label；
- 200% 缩放；
- Reduced Motion；
- 不依赖 Hover 才能操作。

## 9. 视觉回归

重点页面截图：

- Admin Login；
- Admin Overview；
- Storage Desktop/Mobile；
- Jobs List/Detail；
- Organizer Detail；
- Public Search Desktop/Mobile；
- Public Confirm；
- Error/Empty/Loading Gallery。

视觉覆盖矩阵：

- Reference Light/Dark 覆盖全部核心页面；
- High Contrast 覆盖核心基础组件和关键业务流程；
- 每个新增 UI Pack 覆盖其声明的主 Theme、Component Gallery 和核心页面；
- 390px/320px 覆盖 Admin/Public Shell、Storage、Job Detail、Public Search 和 Request Status；
- 不运行所有 Theme × UI Pack 的无意义全笛卡尔积。

视觉回归不要求每个动态时间文本完全一致；使用固定 Fixture 和时间 Mock。Theme、Recipe 或 Renderer 变更必须明确更新对应基线，不能通过扩大截图阈值掩盖差异。

## 10. 性能预算

### 构建目标

- 所有页面路由懒加载；
- Public 初始入口不加载 Admin Feature；
- Admin 初始入口不加载 Organizer/Updates 等大 Feature；
- 组件库按需 Tree Shake；
- 图片有合理尺寸；
- 不把全部旧图标和背景无条件打包。

建议预算：

| 指标 | 目标 |
|---|---:|
| Public 首屏 JS gzip | <= 250 KB，超出需说明 |
| Admin Shell JS gzip | <= 350 KB，超出需说明 |
| 单个异步 Feature Chunk gzip | <= 250 KB |
| LAN 环境 LCP | <= 2.5 秒 |
| Route 切换可操作 | <= 1 秒（缓存后） |

复杂 UI 库可能影响首期预算，AI 不得通过删除必要能力来过预算，应优先做路由分包和依赖分析。

## 11. 静态质量规则

### TypeScript

- `strict`；
- 无隐式 `any`；
- Catch 使用 `unknown`；
- 不使用 `@ts-ignore`，确需使用 `@ts-expect-error` 并说明；
- 不使用非空断言掩盖真实空值；
- API 数据先适配再进入组件。

### Vue

- `<script setup lang="ts">`；
- Props/Emits 类型化；
- 不直接操作 DOM，焦点/尺寸等确有需要时封装 Composable；
- 不使用全局 mutable singleton；
- 不在 Template 写复杂业务表达式；
- 列表使用稳定 Key；
- 不滥用 Deep Watch；
- Watcher 必须有明确清理。

### CSS

- 使用 Token；
- Feature 样式限定作用域；
- 不新增大面积 `!important`；
- 不依赖随机深层选择器覆盖组件库；
- 不用硬编码 z-index 竞赛；
- 移动端不产生嵌套横向滚动。

### API

- 无页面直接 `fetch`；
- 无 endpoint 字符串散落；
- Mutation 不自动重试；
- 请求可取消；
- Secret 不进入日志。

## 12. CI 门禁

建议工作流：

```bash
python -m pip install -r requirements.txt pytest ruff
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
```

Playwright 可分为：

- PR：关键 Mock E2E；
- main/release：完整 E2E；
- 发布前：类生产冒烟。

## 13. 每阶段 Definition of Done

- [ ] 需求和范围完成；
- [ ] 类型检查通过；
- [ ] Lint 通过；
- [ ] 单元/组件测试通过；
- [ ] 该 Feature E2E 通过；
- [ ] 后端相关 pytest 通过；
- [ ] Production Build 通过；
- [ ] Desktop/Mobile 检查；
- [ ] Theme/Component Recipe/UI Pack 关键组合检查；
- [ ] Renderer Contract 与未安装 Pack fallback 检查；
- [ ] Loading/Empty/Error/Success 完整；
- [ ] 401/403/409/500 检查；
- [ ] 无障碍检查；
- [ ] 无 Secret 泄漏；
- [ ] 文档和 Task Board 更新；
- [ ] 回滚方式明确；
- [ ] Git Diff 不包含无关文件和大面积 CRLF 变化。

## 14. 失败处理

连续三次同类失败后必须停止重复尝试并重新评估：

- 是否接口契约判断错误；
- 是否 Mock 与真实响应不一致；
- 是否组件边界错误；
- 是否测试依赖实现细节；
- 是否需要最小后端修复；
- 是否应缩小本阶段范围。

不得通过跳过测试、扩大 timeout、删除断言或关闭 TypeScript 规则掩盖问题。
