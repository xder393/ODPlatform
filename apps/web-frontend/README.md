# apps/web-frontend — 企业 AI 质检工作台（Web 前端）

React 19 + TypeScript + Vite 8 实现的质检工作台：登录鉴权 → 实时告警流 → 工单处置（AI 处置建议 + 时间线 + 高危操作二次认证）。对接 `apps/web-backend/` 的 REST API 与 WebSocket 告警流，共享契约见 `packages/shared-schemas/`。

## 登录流程

- 打开页面时 `App` 作为鉴权门：`localStorage["odp_token"]` 无值 → 渲染 `LoginForm`；有值 → 渲染 `RealtimeWorkbench`（顶部带「退出登录」，清 token 回登录页）。
- `LoginForm` 提交邮箱 + 密码到 `POST /api/v1/auth/login`，成功后把 `access_token` 写入 `localStorage["odp_token"]` 并回调 `onAuthenticated`；401 显示「邮箱或密码错误」。
- 业务请求统一走 `src/api/client.ts` 的 `apiFetch`：自动附加 `Authorization: Bearer <token>`；`login()` 本身不自动存 token。
- 每次 WebSocket 连接先以 Bearer JWT 换取 60 秒、一次性 `/api/v1/auth/websocket-ticket`，再连接 `/ws/inspection-events?ticket=<opaque>&cursor=<opaque>`；长期 JWT 不出现在 URL。REST 与 WebSocket 都返回 `{ cursor, alert }`，游标只在接受并按 `event_id` 去重后保存，并按 actor/session 隔离。
- 演示账户见仓库根 README「企业质检演示」。

## 组件结构

```
src/
├── main.tsx                         入口：StrictMode + <App since=…/>
├── App.tsx                          鉴权门：无 token → LoginForm；有 token → 工作台 + 登出头
├── api/
│   ├── client.ts                    apiFetch（Bearer 注入）+ login/listCases/transitionCase/
│   │                                requestAdvice/simulatePause/reauthenticate
│   └── types.ts                     与后端契约一一对应的类型
└── features/
    ├── auth/LoginForm.tsx           邮箱/密码登录表单（中文 UI、label 关联、401 错误文案）
    ├── workbench/RealtimeWorkbench.tsx  告警流 + 工单列表/详情/时间线/暂停二次认证
    ├── workbench/useInspectionFeed.ts   票据 WS 告警流（游标 REST 补偿 + 1/2/4/8/16/30 秒退避）
    ├── cases/CaseTimeline.tsx       工单时间线（检测事件/AI 建议/人工流转/当前状态）
    └── advice/AdvicePanel.tsx       AI 处置建议面板（可信度徽标 + 引用来源 + 模拟暂停产线）
```

## 常用命令

```bash
npm test                # vitest 单测（jsdom，26 个）
npm run build           # tsc --noEmit + vite build
npm run typecheck:e2e   # 仅类型检查 Playwright 规格与配置（无需浏览器）
npm run test:e2e        # Playwright E2E（需服务已就绪 + 浏览器已安装）
```

本地起 dev server：`npm run dev`（开发时由 vite 代理或 nginx 转发 `/api` 与 `/ws` 到后端）。

## E2E

`e2e/quality-workflow.spec.ts` 覆盖确定性闭环：登录质检员账户 → 看到「疑似表面划痕」告警 → 选中工单出现 AI 处置建议（可信度高徽标 + 引用来源含文档版本/页码）→ 确认复检（时间线「待确认 → 复核中」）→ 完成处置（状态已处置）。

运行方式（CI 同款）：

```bash
# 1. 起全套服务（api/nginx/postgres/redis，含 seed 数据；本机无 docker 则在 CI 跑）
docker compose -f ../../deploy/compose.yaml up -d --build

# 2. 安装浏览器（一次）
npx playwright install --with-deps chromium

# 3. 跑测试
E2E_BASE_URL=http://localhost:8080 npm run test:e2e
```

`playwright.config.ts` 的 `baseURL` 取环境变量 `E2E_BASE_URL`（默认 `http://localhost:8080`）；CI 由 compose 统一起服务，配置中不自动启动 dev server（如需本地自动起服务，解除配置文件中 `webServer` 段的注释）。E2E 类型检查独立于 src 主 tsconfig（见 `tsconfig.e2e.json`），不引入额外类型依赖。
