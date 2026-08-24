# ODPlatform

企业 AI 质检平台：面向制造业一线质检员的实时质检工作台（摄像头实时告警 → 工单处置 → AI 处置建议 → 审计链闭环），加上一套目标检测开发工具链（「数据准备 → 质检 → 配置 → 训练 → 评估 → 推理」9 个命令行）。视觉核心基于 YOLO（ultralytics）。

## 使用教程

### 方式一：本机开发运行（无需 Docker）

**前提**：Python 3.12、Node.js ≥ 20。

**终端 1 — 启动后端**（仓库根目录；`ODP_SEED_DEMO=true` 启用演示数据，否则无法登录）：

```bash
# 首次运行或升级代码后：显式迁移持久化 SQLite 数据库。
ODP_DATABASE_URL=sqlite:////tmp/odp-quality-inspection.sqlite3 \
  bash -c 'cd apps/web-backend && ../../.venv-runtime/bin/alembic upgrade head'

ODP_AUTH_JWT_SECRET=dev-demo-secret ODP_SEED_DEMO=true \
  .venv-runtime/bin/uvicorn odp_api.main:create_app --factory --host 127.0.0.1 --port 8000
```

> 没有现成虚拟环境时：`python -m venv .venv-runtime && .venv-runtime/bin/pip install -e ./apps/web-backend -e ./packages/shared-schemas`。默认 SQLite 文件是 `/tmp/odp-quality-inspection.sqlite3`；应用启动也会执行 Alembic 升级并以稳定业务键补齐演示种子，因此重启不会复制账户、工单、检测事件或审计记录。

**终端 2 — 启动前端**（`apps/web-frontend/` 目录）：

```bash
npm install
npm run dev
```

**浏览器打开 http://localhost:5173**，用质检员账户登录（见下方演示账户表）。

> 前端经 Vite 代理访问后端（`/api`、`/ws` 转发到 `127.0.0.1:8000`），与 nginx 生产形态同源。
> 直接访问 `http://127.0.0.1:8000/` 返回 404 是正常的——API 没有根页面，界面由前端提供。

### 方式二：Docker 一键运行（完整演示栈）

```bash
docker compose -f deploy/compose.yaml up -d --build
# 等待健康检查通过（最长约 2 分钟，镜像构建 + 迁移 + 种子数据）
curl -fsS http://localhost:8080/healthz
```

浏览器打开 **http://localhost:8080**。服务一览：

| 服务 | 地址 | 说明 |
|---|---|---|
| 质检平台（nginx 统一入口） | :8080 | 前端 + API + WebSocket |
| API 直连 | :8000 | 含 `/metrics` |
| Prometheus | :9090 | 指标 + 告警规则 |
| Grafana | :3000 | `admin`/`admin`，预置质检仪表盘 |
| Alertmanager | :9093 | 告警路由 |

### 演示账户

| 邮箱 | 密码 | 角色 | 用途 |
|---|---|---|---|
| `inspector@example.test` | `odp-inspector-dev` | 质检员 | 查看告警、处置工单（**演示主流程用这个**） |
| `leader@example.test` | `odp-leader-dev` | 班组长 | 复核与权限边界演示 |
| `admin@example.test` | `odp-admin-dev` | 管理员 | 管理演示 |

账户仅用于本地演示（开发密码明文内置，禁止用于生产）。

### 演示脚本（登录 → 看告警 → 处置 → 看审计/指标）

1. 启动服务（方式一或方式二）
2. 浏览器打开工作台，用质检员账户登录
3. 实时告警区出现「疑似表面划痕」卡片（模拟相机事件，含置信度与相机编号）
4. 工单列表选中「待确认」工单 → 右侧生成 **AI 处置建议**：「可信度高」徽标 + 引用来源（规程名称 · 文档版本 · 页码/段落）
5. （可选，高危操作演示）点击「模拟暂停产线」→ 提示 5 分钟再次认证 → 输入密码确认
6. 点击「确认复检」→ 工单时间线出现「待确认 → 复核中」
7. 点击「完成处置」→ 工单状态变为「已处置」，人工流转写入审计哈希链
8. （Docker 模式）打开 Grafana 查看 Quality Inspection 仪表盘：告警速率、推理延迟（p50/p95）、工单解决时长、RAG 建议命中率；Prometheus 查询 `inspection_alert_total` 等指标
9. 结束后 `docker compose -f deploy/compose.yaml down`

### 持久化、迁移与恢复

- 本机模式把业务状态（账户密码哈希、工单、状态历史、审计链和告警事实）保存在 `ODP_DATABASE_URL`，默认是 `/tmp/odp-quality-inspection.sqlite3`。迁移命令为 `cd apps/web-backend && alembic upgrade head`（需要时设置同一 `ODP_DATABASE_URL`）。
- Docker 模式使用 `postgres-data` 卷中的 PostgreSQL 保存同一业务状态；Redis 仅用于一次性票据、二次认证 TTL 和跨实例告警唤醒，不是告警事实来源。Compose 中既有卷/迁移 owner 是 `odp`，API 运行角色是受限的 `odp_app`；后者不能更新、删除或截断审计日志。文件中的数据库密码仅供本地演示，部署时必须由秘密管理系统注入独立的运行时与迁移 URL。`docker compose down` 保留数据卷，`docker compose down -v` 会删除演示数据。
- Docker 种子只由一次性 `migrate` 服务执行，顺序为 business → durable alerts → knowledge；API replica 不会在启动时重播演示告警，也不应在 Docker 中设置 `ODP_SEED_DEMO`。服务异常恢复时先恢复 PostgreSQL 卷/备份，再以 owner URL（`postgresql+psycopg://odp:…`）运行 `database_roles --bootstrap-only`、`odp_api.migrations`（RAG/pgvector）和 `alembic upgrade head`；随后以 owner URL 重放 `database_roles` 授权，并以 runtime URL（`postgresql+psycopg://odp_app:…`）依次运行 `odp_api.seed`、`odp_api.seed_alerts` 和 `ODP_POSTGRES_URL=postgresql://odp_app:… python -m odp_api.seed_knowledge`，然后重启 `api`。任一迁移或种子失败都不会启动 Uvicorn。
- Alembic URL 优先级是调用方显式 `Config.set_main_option("sqlalchemy.url", ...)`、然后 `ODP_DATABASE_URL`、最后 `alembic.ini` 的本地 SQLite 默认值。这样 CLI/Compose 会使用环境数据库，而程序化测试可隔离到自己的数据库。
- 旧 `postgres-data` 卷仍以 owner `odp` 运行；不要重建卷或重命名 owner。升级前先备份：`docker compose -f deploy/compose.yaml exec -T postgres pg_dump -U odp -d odp > odp-before-upgrade.sql`。随后运行 `deploy/postgres/upgrade-existing-volume.sh`（或 `docker compose -f deploy/compose.yaml up migrate`）；该一次性服务会幂等修正 `odp_app`、迁移、授权并播种，而不会删除既有数据。若升级失败，以备份恢复：`cat odp-before-upgrade.sql | docker compose -f deploy/compose.yaml exec -T postgres psql -U odp -d odp`；代码回滚时也应回滚对应镜像后再启动 `migrate`，避免在不兼容的 schema 上直接启动 API。

### WebSocket 票据与游标

浏览器绝不把长期 JWT 放在 WebSocket URL。每次连接先用 Bearer JWT 调用 `POST /api/v1/auth/websocket-ticket`，得到随机、单次使用、60 秒有效的票据；随后连接 `/ws/inspection-events?ticket=…&cursor=…`。后端的 REST 补偿与 WebSocket 帧均使用 `{ cursor, alert }` 包络。前端只在接受并按 `event_id` 去重后，才把游标保存为当前认证 actor 的 `localStorage["odp_alert_cursor"]`；登出或切换 actor 会清除/隔离该状态，并按 1/2/4/8/16/30 秒退避重连。

### 开发专用未来告警触发器

只有显式设置 `ODP_ENABLE_DEV_EVENT_TRIGGER=true` 的运行时才提供经过 Bearer 鉴权、组织和产线授权检查的 `POST /api/v1/dev/inspection-events`，请求体为 `{"event_id":"UUID","line_id":"UUID"}`。它用于 Compose/Playwright 在已经建立 WebSocket 后发布确定性测试告警；环境名称不会隐式开启此路由，不能作为生产写入接口。

账号启用状态由持久化身份仓储在每次认证和票据消费时强制执行；本 P0 不提供管理员启用/停用账号的管理接口。

### 常见问题

- **`apps/web-frontend` 下没有 `package.json`**：企业版代码在 `codex/enterprise-ai-quality-inspection` 分支上；主分支（main）的这些目录还是旧占位符，切到该分支（或合并后）即可。
- **`/` 返回 404**：正常现象，API 只有 `/healthz`、`/api/v1/*`、`/ws/*`、`/metrics` 等路由，界面由前端提供。
- **登录报 401**：本机开发确认已使用 `ODP_SEED_DEMO=true`，或手工运行 business seed；Docker 则检查一次性 `migrate` 服务是否成功完成，勿向 API 设置 `ODP_SEED_DEMO`。
- **「AI 建议不可用」**：确认后端使用默认 `inmemory` 检索后端且启用了种子数据（`ODP_RETRIEVAL_BACKEND=pgvector` 需要 PostgreSQL 已迁移）。

## 命令行工具（目标检测开发流程）

### 9 个命令

| 命令 | 作用 |
|---|---|
| `odp-init` | 初始化项目目录结构 |
| `odp-reset` | 安全撤销运行时产物（默认 dry-run，双重防护） |
| `odp-transform` | VOC/COCO/YOLO 标注 → YOLO 格式 + train/val/test 划分 + 训练 yaml |
| `odp-validate` | 数据集质检：图像-标注成对 / 标签格式 / 数据泄露 / yaml 字段 |
| `odp-gen-config` | 反射式生成训练/评估/推理配置模板 |
| `odp-train` | 训练 |
| `odp-val` | 评估（mAP / precision / recall） |
| `odp-infer` | 推理（图片/视频/文件夹/摄像头，多线程流水线，支持实时显示） |
| `odp-detect` | 轻量检测（出框保存，输出统计） |

### 快速开始

```bash
# 1. 环境
python -m venv .venv && source .venv/bin/activate
pip install -e ./apps/platform
pip install torch ultralytics   # 训练/推理需要

# 2. 初始化
odp-init

# 3. 数据集放到 data/raw/<数据集名>/{images, annotations}/
```

### 典型流程

```bash
# 转换 + 划分（VOC 示例；COCO/YOLO 同理换 --format）
odp-transform --dataset safety_helmet --format pascal_voc

# 质检（4 项检查，PASS/ERROR 退出码接 CI）
odp-validate --dataset safety_helmet

# 生成训练配置，按需编辑
odp-gen-config train

# 训练
odp-train --data safety_helmet.yaml --model yolo11n.pt --epochs 50 --imgsz 320

# 评估
odp-val --data safety_helmet.yaml --model <best.pt>

# 推理
odp-infer --model <best.pt> --source 图片/视频/文件夹 --save
```

训练/评估/推理的产物都自动落盘：权重归档到 `models/checkpoints/`、审计快照（`odp_audit.json`，含配置 + 指标 + 溯源）到 `runs/`、日志到 `apps/platform/logging/`。

## 企业质检平台架构

- **Web 后端**（`apps/web-backend/`）：FastAPI + Pydantic，端口/适配器分层（六边形架构）；模块化实现检查工单（cases）、身份与 RBAC（identity）、AI 编排（ai_orchestration）、审计哈希链（audit）、知识库（knowledge）与可观测性（observability）；JWT 登录、一次性 WebSocket 票据、持久化告警游标流与 Prometheus 指标（`/metrics`）
- **Web 前端**（`apps/web-frontend/`）：React 19 + TypeScript + Vite；登录鉴权门（token 存 `localStorage["odp_token"]`）→ 票据 WebSocket + 游标 REST 补偿 → 工单时间线 / AI 处置建议面板（可信度徽标 + 文档引用）/ 暂停产线二次认证；vitest 单测 + Playwright E2E
- **共享契约**（`packages/shared-schemas/`）：前后端共用的 Pydantic 事件模型
- **部署**（`deploy/`）：Docker Compose（api / nginx 前端 / postgres+pgvector / redis / minio）+ Prometheus / Alertmanager / Grafana 可观测栈

角色与权限：质检员、班组长、管理员，按组织 + 产线做租户隔离；高危操作（暂停产线）要求 5 分钟内的再次认证；审计日志为追加式哈希链，启动抽检 + 每日全量校验。

### 视觉核心引擎（`apps/platform/`）

```
odp_platform/
├── common/            工具层: 路径/日志/字符串/系统/性能/指标
├── data_pipeline/     数据准备: 注册表 + 转换器 + 划分
├── data_validation/   数据质检: 检查器注册表 + 一次扫描快照 + 报告
├── runtime_config/    运行配置: Pydantic + 三源合并(CLI>YAML>默认) + 溯源
├── training/          训练编排
├── evaluation/        评估编排
├── inference/         推理: 多线程流水线 + 帧源 + 美化可视化
├── frame_source/      帧源: 图片/视频/文件夹/摄像头/流
├── visualization/     检测框美化绘制
└── cli/               9 个命令行入口
```

几个核心设计（细节见 `docs/architecture/` 的 ADR）：

- **注册表模式**：加一种标注格式 / 一个质检项 = 加一个文件，框架代码零改动（`@register`/`@check` 装饰器 + `pkgutil` 自动发现）
- **依赖注入**：落盘模块不 import 全局路径，目标目录由调用方注入
- **纯函数与 IO 分离**：划分逻辑纯函数可秒测，碰盘逻辑单独一层
- **数据与展示分离**：验证报告是纯数据，渲染是纯展示
- **配置溯源**：每个配置值能追到来源（CLI/YAML/默认）
- **fail-fast**：覆盖率低于阈值、质检有 ERROR，训练前直接拦下
- **软依赖**：torch/ultralytics 未装时 import 不崩

## 测试

三段测试（CI 同款命令）：

```bash
# 1. 平台核心引擎（202 个测试：注册表机制、划分边界、配置合并溯源、训练/评估编排、端到端冒烟）
cd apps/platform && pytest tests/ -q

# 2. Web 后端（工单状态机、RBAC/租户隔离、审计哈希链、持久化票据/告警、任务状态机、知识库、AI 编排、登录）
pytest apps/web-backend/tests -q

# 3. Web 前端（登录、鉴权门、票据/游标告警对账、工单处置、建议面板）
cd apps/web-frontend && npm test && npm run build && npm run typecheck:e2e

# E2E（需 Docker 起全套服务 + Playwright 浏览器）
cd apps/web-frontend && E2E_BASE_URL=http://localhost:8080 npm run test:e2e
```

## 目录结构

```
apps/platform/          视觉核心引擎（src 布局，hatchling 打包）
apps/web-backend/       Web 后端：FastAPI 质检 API（cases/identity/ai_orchestration/audit…）
apps/web-frontend/      Web 前端：React 19 质检工作台（登录 + 实时告警 + 工单处置）
packages/shared-schemas/ 共享 Pydantic 模型（前后端契约）
deploy/                 Docker Compose + nginx + Prometheus/Alertmanager/Grafana 可观测栈
docs/architecture/      架构决策记录（ADR）
scripts/                开发脚本
```

## 技术栈

Python 3.12 · ultralytics(YOLO) · PyTorch · Pydantic v2 · scikit-learn · matplotlib · hatchling · FastAPI · React 19 · TypeScript · Vite · vitest · Playwright · PostgreSQL(pgvector) · Redis · Prometheus/Grafana

## 许可证

[MIT](LICENSE)
