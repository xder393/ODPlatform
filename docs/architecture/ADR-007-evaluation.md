# ADR-007: evaluation 子系统架构

**状态**: Accepted
**日期**: 2026-05-24
**作用范围**: `apps/platform/src/odp_platform/evaluation/`

## 上下文

D6 训练子系统产出权重后, 需要一个"评估"环节——在数据集上跑 `model.val()`
计算 mAP / precision / recall, 输出可审计的指标。D3/D6 的 TODO 里把这一块
记为"计划目录 evaluation/", 本 ADR 记录它的落地方式。

## 决策

### 1. 复用 TrainMetrics, 不另立 ValMetrics

`common/result.py` 的 `TrainMetrics` 结构 (task / save_dir / timestamp /
speed_ms / overall / class_map_50_95) 对 train 和 val 完全一致——ultralytics
两边的指标都是 `precision/recall/mAP50/mAP50-95`。再立一个 `ValMetrics`
只会复制一遍字段和序列化逻辑。

只做了两处**向后兼容**的小扩展:

- `TrainMetrics.from_yolo_results(..., task=None)`: `model.val()` 返回的
  `Metrics` 对象不带 `.task` 属性, 所以允许调用方显式传入 `config.task`,
  保证 `_METRIC_FIELDS_BY_TASK` 选对字段表 (否则会落到"打印 results_dict
  全量"的兜底分支)。
- `log_train_metrics(..., title="训练结果")`: 评估时传 `title="评估结果"`,
  避免日志头打印"训练结果"这种误导性措辞。

### 2. ValService 镜像 TrainService 的 8 阶段编排

`ValService.val()` 照搬 D6 `TrainService.train()` 的骨架:

1. `build_val_config` (D5)
2. 上下文日志 (设备快照 + 字段溯源)
3. 数据集预校验 (D4, 默认开, `--no-pre-validate` 关)
4. 加载模型 (ultralytics 软依赖守卫)
5. `model.val(**kwargs)` (输出根 `RUNS_DIR/<task>_val/`, 与 `_train`/`_infer` 并列)
6. 指标 (TrainMetrics + 日志)
7. 日志改名 (复用 `rename_log_to_save_dir`)
8. 审计快照 (`odp_audit.json`)

跟 D6 的唯一结构差异: **没有 archive 阶段**——评估不产出权重, 归档是训练
专属动作。其余全部复用, 不重写。

### 3. 永不抛 — 顶层 try/except 打包成 ValResult.error

跟 `TrainResult` 同款。`ValResult` 是 frozen dataclass (success / output_dir /
metrics / val_time / error / audit_path / log_path)。service 内部任何异常都被
兜住, 失败信息进 `error` 字段, 由 CLI 翻译成退出码。

### 4. CLI 薄壳 — odp-val

`cli/val_model.py` 照搬 `cli/train_model.py`:
argparse → 装日志 handler → `ValService.val()` → 退出码 0/1/130。

`half` / `plots` / `save_json` 这类布尔开关**不暴露成 CLI flag**——它们有
合理的 yaml 默认值, 用 `odp-gen-config val` 生成模板后编辑即可。CLI 只暴露
"有 None 默认值的取值参数"(不会产生 `store_true` 默认 False 污染 yaml 合并),
这跟 D6 train CLI 的纪律一致。

## 后果

**正面**:
- 评估链路跟训练链路长得一样, 新人学一次 D6 就懂 D7
- 指标结构单一起源 (`common/result.TrainMetrics`), 审计 JSON 结构稳定
- 预校验保证评估结果不是从坏数据集算出来的

**负面 / 已知边界**:
- 无 "对比多次评估" 的能力——`odp_audit.json` 是落点, 跨 run 对比留给
  未来的 experiment_db
- 无 per-class 的 PR 曲线 / 混淆矩阵自定义处理, 目前直接依赖 ultralytics
  `plots=True` 输出的图

## 拒绝的方案

- **另立 ValMetrics dataclass**: 字段重复, 序列化逻辑复制, 无收益
- **复用 TrainService 加 mode 参数**: 一个类靠 `mode="train"/"val"` 分支切换
  是 OOP 里的"参数切换反模式", train/val 归档行为不同, 硬塞一个类里会让
  `if mode == "train": archive()` 这种分支爬进来
- **评估不做 D4 预校验**: 评估虽比训练便宜, 但从坏数据算出的指标照样误导
  决策, fail-fast 原则同样适用
