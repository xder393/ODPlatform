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

## 测试

```bash
cd apps/platform && pytest tests/ -q
# 202 个测试：注册表机制、划分边界、配置合并溯源、训练/评估编排、端到端冒烟
```

## 目录结构

```
apps/platform/         核心引擎（src 布局，hatchling 打包）
apps/desktop/          桌面端（PySide6 + PyInstaller 打包方案）
apps/web-backend/      Web 后端（占位，V1.1）
apps/web-frontend/     Web 前端（占位，V1.1）
packages/shared-schemas/ 共享 Pydantic 模型（占位，V1.1）
docs/architecture/     架构决策记录（ADR）
scripts/               开发脚本
```

## 技术栈

Python 3.12 · PySide6 · ultralytics(YOLO) · PyTorch · Pydantic v2 · scikit-learn · matplotlib · hatchling

## 许可证

[MIT](LICENSE)
