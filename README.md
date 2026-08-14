# ODPlatform

通用目标检测开发平台(Monorepo workspace root)。

面向目标检测模型从数据准备、验证、训练到推理的完整开发流程，
提供一套企业级、可安装、可移植的命令行工具链。

## 子项目结构

| 目录 | 说明 |
|---|---|
| `apps/platform/` | 核心引擎（数据转换、验证、训练、推理） |
| `apps/web-backend/` | Web 后端（占位，V1.1 启动） |
| `apps/web-frontend/` | Web 前端（占位，V1.1 启动） |
| `apps/desktop/` | 桌面端（占位，V2.0 启动） |
| `packages/shared-schemas/` | 共享 Pydantic 数据模型（占位，V1.1 启动） |

## 快速开始

```bash
# 1. 创建并激活环境
conda create -n odplat python=3.12
conda activate odplat

# 2. 安装
pip install -e ./apps/platform

# 3. 初始化项目目录
odp-init

# 4. 放入数据集到 data/raw/<数据集名>/{images, annotations}/
# 5. 转换 + 划分 + 生成训练 yaml
odp-transform --dataset <数据集名> --format pascal_voc
```

参考 `docs/architecture/` 下的架构决策记录（ADR）。
