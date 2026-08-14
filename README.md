# ODPlatform

一个目标检测开发平台,覆盖从数据准备、验证、配置、训练到推理的完整流程。写成了一套命令行工具链,基于 YOLO(ultralytics)。

## 目录结构

Monorepo,核心在 `apps/platform/`,其他端是占位:

```
apps/platform/         核心引擎(src 布局)
apps/web-backend/      Web 后端(占位, V1.1)
apps/web-frontend/     Web 前端(占位, V1.1)
apps/desktop/          桌面端(占位, V2.0)
packages/shared-schemas/ 共享 Pydantic 模型(占位, V1.1)
docs/architecture/     架构决策记录(ADR)
```

## 安装

```bash
conda create -n odplat python=3.12
conda activate odplat
pip install -e ./apps/platform
```

要实际跑训练/推理,还需要 `torch` 和 `ultralytics`(体量较大,可之后单独装):

```bash
pip install torch ultralytics
```

## 命令

| 命令 | 作用 |
|---|---|
| `odp-init` | 初始化项目目录结构 |
| `odp-reset` | 安全撤销 init 产生的运行时产物(默认 dry-run) |
| `odp-transform` | 把 raw 数据集转成 YOLO 格式 + 划分 + 生成训练 yaml |
| `odp-validate` | 数据集质检(图像标签成对 / 字段 / 格式 / 防泄露) |
| `odp-gen-config` | 生成训练/验证/推理的运行配置模板 |
| `odp-train` | 训练 |
| `odp-val` | 评估(在数据集上算 mAP/precision/recall) |
| `odp-infer` | 推理 |

## 典型流程

```bash
odp-init

# 放数据集到 data/raw/<数据集名>/{images, annotations}/

# 1. 转换 + 划分(VOC 示例)
odp-transform --dataset safety_helmet --format pascal_voc

# 2. 质检
odp-validate --dataset safety_helmet

# 3. 生成训练配置,按需改
odp-gen-config train

# 4. 训练
odp-train --data configs/datasets/safety_helmet.yaml ...

# 5. 评估
odp-val --model runs/detect_train/train/weights/best.pt --split val

# 6. 推理
odp-infer ...
```

设计上的关键决策都记在 `docs/architecture/` 的 ADR 里,想了解"为什么这么写"可以翻那里。
