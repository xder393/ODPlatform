# ODPlatform

目标检测开发平台：覆盖「数据准备 → 质检 → 配置 → 训练 → 评估 → 推理」完整链路，提供一套命令行工具和一个 PySide6 桌面端。基于 YOLO（ultralytics）。

## 功能

### 命令行（9 个命令）

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

### 桌面端（PySide6，macOS 风格）

`python apps/desktop/main.py`，五个页面：

- **数据集**：选择/拖拽导入 + 格式自动检测 + 带标注框的样本预览 + 目录树
- **质量检查**：进度条 + 通过/警告/错误卡片 + 问题详情表 + 导出报告
- **格式转换**：源格式/目标格式 + 划分选项 + 转换结果
- **训练**：启动训练/评估（可停止）+ 实验列表 + 指标卡片 + 训练曲线 + 混淆矩阵
- **检测**：选图/选文件夹 + 置信度阈值 + 出框结果

### 企业 AI 质检平台（Web，V1.1）

浏览器端生产线质检工作台，覆盖「实时告警 → 工单处置 → AI 处置建议（RAG + 引用溯源）→ 审计链」闭环：

- **Web 后端**（`apps/web-backend/`）：FastAPI + Pydantic，端口/适配器分层（六边形架构）；模块化实现检查工单（cases）、身份与 RBAC（identity）、AI 编排（ai_orchestration）、审计哈希链（audit）、知识库（knowledge）与可观测性（observability）；JWT 登录（`POST /api/v1/auth/login`）、WebSocket 告警流（`/ws/inspection-events`，query param 鉴权）
- **Web 前端**（`apps/web-frontend/`）：React 19 + TypeScript + Vite；登录鉴权门（token 存 `localStorage["odp_token"]`）→ 实时告警流（WS + REST 补偿对账）→ 工单时间线 / AI 处置建议面板（可信度徽标 + 文档引用）/ 暂停产线二次认证；vitest 单测 + Playwright E2E
- **共享契约**（`packages/shared-schemas/`）：前后端共用的 Pydantic 事件模型
- **部署**（`deploy/`）：Docker Compose（api / nginx 前端 / postgres / redis / minio）+ Prometheus / Alertmanager / Grafana 可观测栈

角色与权限：质检员（inspector）、线长/主管（supervisor）、管理员（administrator），按组织 + 产线做租户隔离；高危操作（暂停产线）要求 5 分钟内的再次认证。

## 架构

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

## 快速开始

```bash
# 1. 环境
conda create -n odplat python=3.12
conda activate odplat
pip install -e ./apps/platform
pip install torch ultralytics PySide6   # 训练/推理/桌面端

# 2. 初始化
odp-init

# 3. 数据集放到 data/raw/<数据集名>/{images, annotations}/
#    或在桌面端直接拖拽导入
```

## 典型流程

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

## 企业质检演示

Docker 一键启动（需 Docker；前端经 nginx 统一入口，端口 8080）：

```bash
docker compose -f deploy/compose.yaml up -d --build
```

### 演示账户

| 邮箱 | 密码 | 角色 | 用途 |
|---|---|---|---|
| `inspector@example.test` | `odp-inspector-dev` | 质检员 | 查看告警、处置工单（演示主流程） |
| `leader@example.test` | `odp-leader-dev` | 线长 | 复核与权限边界演示 |
| `admin@example.test` | `odp-admin-dev` | 管理员 | 审计与全局配置演示 |

账户仅用于本地演示（开发密码明文内置，禁止用于生产）。

### 演示脚本（登录 → 看告警 → 处置 → 看审计/指标）

1. 启动服务：`docker compose -f deploy/compose.yaml up -d --build`
2. 等待健康检查通过：`curl -fsS http://localhost:8080/healthz` → `{"status":"ok"}`（失败则隔几秒重试）
3. 浏览器打开 http://localhost:8080，用质检员账户（上表第一行）登录
4. 实时告警区出现「疑似表面划痕」卡片（模拟相机事件，含置信度与相机编号）
5. 工单列表选中「待确认」工单 → 右侧生成 AI 处置建议：「可信度高」徽标 + 引用来源（规程名称 · 文档版本 · 页码/段落）
6. （可选，高危操作演示）点击「模拟暂停产线」→ 提示 5 分钟再次认证 → 输入密码确认
7. 点击「确认复检」→ 工单时间线出现「待确认 → 复核中」
8. 点击「完成处置」→ 工单状态变为「已处置」，人工流转写入审计哈希链
9. 打开 Grafana（http://localhost:3000，`admin`/`admin`）查看 Quality Inspection 仪表盘：告警速率、推理延迟（p50/p95）、工单解决时长、RAG 建议命中率
10. 打开 Prometheus（http://localhost:9090）查询 `odp_inspection_alerts_total`、`odp_audit_verification_failures_total` 等指标；结束后 `docker compose -f deploy/compose.yaml down`

### 性能基线

首版性能基线：3 路模拟摄像头各 5 FPS、20 个并发工作台用户（来自设计 spec）。生产容量以压测数据为准——压测待做；接口已保留批处理与动态批大小扩展点。当前为单进程内存/模拟适配器架构，扩容前需先完成持久化与水平扩展设计。

## 测试

三段测试，均在仓库根目录执行（CI 同款命令）：

```bash
# 1. 平台核心引擎（202 个测试：注册表机制、划分边界、配置合并溯源、训练/评估编排、端到端冒烟）
cd apps/platform && pytest tests/ -q

# 2. Web 后端（当前 77 个测试：工单/身份 RBAC/审计哈希链/AI 编排/租户隔离等）
pytest apps/web-backend/tests -q

# 3. Web 前端（26 个单测：登录、鉴权门、告警流对账、工单处置、建议面板）
cd apps/web-frontend && npm test && npm run build && npm run typecheck:e2e

# E2E（需 docker 起全套服务 + Playwright 浏览器）
cd apps/web-frontend && E2E_BASE_URL=http://localhost:8080 npm run test:e2e
```

## 目录结构

```
apps/platform/         核心引擎（src 布局，hatchling 打包）
apps/desktop/          桌面端（PySide6 + PyInstaller 打包方案）
apps/web-backend/      Web 后端：FastAPI 质检 API（cases/identity/ai_orchestration/audit…）
apps/web-frontend/     Web 前端：React 19 质检工作台（登录 + 实时告警 + 工单处置）
packages/shared-schemas/ 共享 Pydantic 模型（前后端契约）
deploy/                Docker Compose + Prometheus/Alertmanager/Grafana 可观测栈
docs/architecture/     架构决策记录（ADR）
scripts/               开发脚本
```

## 技术栈

Python 3.12 · PySide6 · ultralytics(YOLO) · PyTorch · Pydantic v2 · scikit-learn · matplotlib · hatchling · FastAPI · React 19 · TypeScript · Vite · vitest · Playwright · PostgreSQL(pgvector) · Redis · Prometheus/Grafana

## 许可证

[MIT](LICENSE)
