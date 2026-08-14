# ADR-001: 采用 Monorepo + apps/ 布局

| 项目 | 内容 |
|---|---|
| 状态 | ✅ Accepted |
| 决策日期 | 2026-05-06 |

## 背景
ODPlatform V1.0 只有一个端,但 V1.1+ 要扩展为 3-4 个端。

## 备选方案
- 方案 A: 平铺布局(直觉做法)
- 方案 B: Monorepo + apps/ (采纳) ← 多打一层目录,未来扩展零成本
- 方案 C: Multi-repo

## 决定
方案 B: Monorepo + apps/

## 理由
1. 杠杆效应(今天免费,未来省 100 倍工作)
2. 共享代码方便
3. 跨端原子变更
4. 业界先例(Google / Meta / Uber)
5. 我们的规模合适

## 后果
- 正面: 多端启动零摩擦、跨端测试自然位置
- 负面: CI 稍慢、首次安装路径稍长
- 中性: 需要 marker file + 双层 pyproject

## 撤销条件
仓库 >100 万行 / 团队 >100 人 / 跨端发布周期严重冲突 → 重新评估

## 参考资料
- Monorepo vs Multi-repo
- Why Google uses a Monorepo
- src/ layout in Python projects (PyPA)
- PEP 517 / 621 / 660 — pyproject.toml 标准
