# ADR-006: training 子系统设计

- **状态**: Accepted
- **日期**: 2026-05-24
- **决策者**: ODPlatform team
- **关联**: ADR-001 (路径 SSoT), ADR-002 (词汇 SSoT), ADR-005 (runtime_config 子系统)

## 1. 背景

D5 完成 runtime_config 子系统后, ODPlatform 已经能:
- 通过 `build_train_config(yaml_path, cli_args)` 得到合并好的 Pydantic 配置 + 溯源 merger
- 通过 `odp-gen-config train` 生成自解释的 YAML 模板
- 通过 D4 `validate_dataset` 对数据集做 fail-fast 校验
- 通过 D2 `get_logger("odp_platform", "train")` 拿到一份"业务模块统一发声"的根 logger

但这些子系统**互相之间没有衔接**——用户拿到配置, 想跑训练, 必须自己写胶水代码:

```python
config, merger = build_train_config(...)
report = validate_dataset(...)         # 自己接 D4
if report.exit_code >= 2: ...           # 自己处理失败
get_logger(...)                         # 自己接 D2
model = YOLO(config.model)              # 自己接 ultralytics
model.train(**config.to_ultralytics_kwargs())
# 然后自己 rename log / archive 权重 / 写 audit JSON / 处理异常 / ...
```

胶水代码量大约 200 行——**且每个新用户都要写一遍**, 错误率非常高(典型错误: 不挂 D2 logging / 跳过 D4 校验 / 不归档权重 / 让 ultralytics 异常直接穿透 CLI).

## 2. 决策

立一个 `training/` 子系统作为编排器, 把上述 4 个邻居子系统 + ultralytics 串起来. 子系统的内部结构遵循"跨任务通用 → `common/`, 训练专属 → `training/`"原则.

### 2.1 核心设计选择

| 决策点 | 选项 | 选择 | 理由 |
|---|---|---|---|
| **service 模式** | 包装器 / 薄壳函数 / 编排器 | **编排器** | 8 阶段流水线, 每阶段调一个邻居子系统, 看 service 就是看一次训练做了哪些事 |
| **service 抛不抛异常** | 抛 / 不抛 | **不抛, 装进 `TrainResult.error`** | jupyter / 服务化 / 自动化脚本统一收益 |
| **6 个跨任务工具放哪** | training / common / 新立 yolo_common | **`common/`** | D7 ValService / D8 InferService 都要复用, 放 training 等于让验证 / 推理子系统依赖训练子系统 |
| **TrainMetrics 物理位置** | training/result.py / common/result.py | **`common/result.py`** | D7 ValMetrics 跟 TrainMetrics 几乎同构, 复用一个 dataclass |
| **TrainMetrics 公开路径** | 只 common / common + training 转再导出 | **两条路径都暴露** | `from odp_platform.training import TrainMetrics` 跟 `TrainResult` 一起取符合直觉 |
| **logging handler 装在哪** | 业务模块装 / 每个 service 装 / 只 CLI 入口装 | **只 CLI 入口装一次** | 走 D2 `get_logger("odp_platform", "train")`, 业务模块只发声 |
| **log_rename 操作哪个 root** | unnamed root / `"odp_platform"` named root | **named root** | D2 设计了 named root + `propagate=False`, 操作 unnamed root 等于操作了一个空 logger |
| **best/last.pt 归档** | 不归档 / 移动 / 复制 | **复制** | 原文件留给 ultralytics resume / val 直接读, 归档一份给 D7/D8 引用 |
| **archive 失败影响 result.success** | 是 / 否 | **否(best-effort)** | 训练已经成功, 归档失败只是用户要手动复制, 不让 archive 拖累训练成败判断 |
| **audit JSON 落点** | runs/<task>_train/<train_dir>/odp_audit.json / 独立目录 | **跟 ultralytics save_dir 同目录** | 跟 args.yaml / results.csv 一起, 自然形成"实验快照"概念 |

### 2.2 公开 API (`training/__init__.py` 的 `__all__`)

```python
__all__ = [
    "TrainService",     # class — 训练编排
    "TrainResult",      # dataclass — 训练结果(成败 + 路径 + 指标)
    "TrainMetrics",     # dataclass — 完整指标(转再导出自 common.result)
    "train_yolo",       # function — 便捷一行调用
]
```

下游(D7 / D8 / experiment_db / jupyter)只从这 4 个符号取依赖.

### 2.3 3 条工程规矩 (CI 守门)

| 规矩 | grep 自检 |
|---|---|
| service 内部不重新发明 D5 | `grep "YAMLLoader\|CLILoader\|ConfigMerger" service.py` → 0 |
| 业务模块不挂 handler | `grep -rn "addHandler\|setLevel(" training/ common/` → 只能在 logging_utils / log_rename 出现 |
| 验证/推理子系统不依赖训练 | `grep -rn "from odp_platform.training" evaluation/ inference/` → 0 |

## 3. 不选择的方案

### 方案 A: 把 6 个 common 工具放 `training/`

跟选定方案的差别: 工具的物理位置.

**为什么不选**:
- D7 ValService 需要 `resolve_model_path / resolve_dataset_path / rename_log_to_save_dir`, 必须 `from odp_platform.training import resolve_model_path`
- "验证模块从训练模块 import 工具"——名字跟语义打架, 一眼读不懂
- D8 InferService 同样问题, 一锅端
- 跨任务通用工具不应该挂在任何一个任务的子系统下, 应该挂在 `common/`(项目共享底层)

### 方案 B: 立一层 `yolo_common/` 隔离 YOLO 工具

把 6 个 YOLO 工具放在 `apps/platform/src/odp_platform/yolo_common/`, 跟 `common/` 区分开.

**为什么不选**:
- `odp_platform` 这个端的定位就是"目标检测平台"——`common/` 必然被 YOLO 概念污染, 那是合理的
- 再加一层 `yolo_common/` 等于在 YOLO 平台里加一个"yolo 子标签", 跟项目名打架
- `common/system_utils.log_device_info` (D2 已立) 也是 ML-only, 同样"污染"了 common——但接受度高, 没人提议把它搬走

### 方案 C: 把整个 D6 揉成一个 `train.py` 朴素脚本(不立子系统)

跟绝大多数 yolo 教程同款.

**为什么不选**:
- D5 立的配置溯源、D4 立的数据校验、D2 立的 logging 通道全部要在用户那一侧手工拼接
- 每个用户写 200 行胶水代码, 错误率高
- 没有"训练结果"的实体概念, audit / experiment_db / D7 接 best 都无处接入

### 方案 D: 不立 log_rename, 只靠 `audit JSON` 记录 log_path

D6 只写 audit JSON 一种方式让用户找日志, 不动 named root 的 FileHandler.

**为什么不选**:
- `ls logging/train/` 跟 `ls runs/detect_train/` 对不上是真实高频痛点(每次 debug 都要查映射)
- 文件名直接编码 save_dir 比 audit JSON 查映射的体验好得多
- log_rename 的风险用"操作 named root + best-effort 永不抛 + 失败回滚"治住了, 风险可控

## 4. 后果

### 4.1 好处

- **一行 `odp-train` 跑通完整训练**, 自动接 D2/D4/D5/ultralytics, 用户不写胶水
- **`TrainResult` 永不抛**——jupyter / 服务化 / 自动化脚本调用方式统一
- **`common/` 6 个工具直接被 D7/D8 复用**——D7 写出来的 service.py 跟 D6 高度对称, 维护成本低
- **`odp_audit.json` 给未来 experiment_db 留好落点**——任何一次训练的产物 + 配置 + 指标 + 链路日志都能 1 行 import 进 DB
- **日志文件名跟 save_dir 对得上**——日常 debug 不再查映射表

### 4.2 坏处 / 风险

- **`training/__init__.py` 转再导出 `TrainMetrics`**——一个符号两个路径, 必须靠 ADR + docstring 解释清楚, 否则会让新人疑惑"我到底从哪里 import"
- **log_rename 操作 named root**——一旦 D2 改了 `ROOT_LOGGER_NAME` 常量, log_rename 这个硬编码"odp_platform"要跟着改(已在源码注释里标注)
- **archive 失败不影响 `success`**——用户可能错过 warning, 训练完看不到归档文件以为是 service bug. 缓解: 日志里 warning 级输出 + audit JSON 里 `best_archive` 字段会是 null, 容易排查

### 4.3 性能影响

- **service 内部的额外 logging**(2 段配置溯源 + 1 段指标 + 1 段类别 mAP) → 多写大约 30 行日志, 增量可忽略
- **archive 复制 best+last** → 一次性 IO, 通常 < 200ms(模型 < 100MB 量级)
- **audit JSON 写盘** → 单文件几 KB, 一次性 IO 可忽略

## 5. 关键文件位置

```
apps/platform/src/odp_platform/

common/                              ← 跨任务通用 (6 个新增)
├── model_path.py                    resolve_model_path (含 search_dirs)
├── dataset_path.py                  resolve_dataset_path
├── log_rename.py                    rename_log_to_save_dir (操作 named root)
├── config_log.py                    log_effective_config + log_override_chains
├── result.py                        TrainMetrics + log_train_metrics
└── plot_style.py                    apply_academic_style

training/                            ← 训练专属 (3 个新增)
├── __init__.py                      公开 4 个符号
├── service.py                       TrainService + TrainResult + train_yolo
└── archive.py                       archive_checkpoints

cli/                                 ← CLI 入口 (1 个新增)
└── train_model.py                   odp-train

tests/                               ← 单元测试 (7 个新增 + 2 个 conftest)
├── common/
│   ├── conftest.py                  mock_det_results / mock_segment_results
│   ├── test_model_path.py
│   ├── test_dataset_path.py
│   ├── test_log_rename.py
│   ├── test_config_log.py
│   └── test_result.py
└── training/
    ├── conftest.py                  fake_train_dir
    ├── test_archive.py
    └── test_service.py

docs/adr/
└── 006-training-subsystem.md        本文档
```

## 6. 跟其他 ADR 的关系

- **ADR-001 (路径 SSoT)**: D6 完全靠 `common/paths.py` 拿路径, 不动它. `CHECKPOINTS_DIR` 在 D2 就立好了, D6 只是第一个真正写入它的子系统
- **ADR-002 (词汇 SSoT)**: D6 完全靠 `common/constants.py` 的 `Task.DETECT / Task.SEGMENT`, 不立第二份
- **ADR-005 (runtime_config)**: D6 的 service.py 通过 `build_train_config(yaml_path, cli_args)` 一行获取配置和 merger, 不重新发明任何 D5 已有的合并 / 验证 / 溯源逻辑

## 7. 后续工作

- **D7: evaluation 子系统** — 立 ValService, 复用 D6 的 6 个 common 工具
  - `resolve_model_path("train3-best.pt", search_dirs=[CHECKPOINTS_DIR, PRETRAINED_MODELS_DIR])` 优先查 D6 归档
  - `resolve_dataset_path` 直接复用
  - `rename_log_to_save_dir` 直接复用(把 `train3` 换成 `val3` 即可)
  - `TrainMetrics` 以别名 `ValMetrics = TrainMetrics` 复用(物理同一个类), 复用 `_METRIC_FIELDS_BY_TASK`
- **D8: inference 子系统** — 立 InferService, 同样复用 6 个 common 工具
- **experiment_db 子系统(尚未编号)** — 接管 `odp_audit.json` 的消费侧, 把所有训练 / 验证产物入库
- **`task='unknown'` 优化(P3)** — `TrainMetrics.from_yolo_results` 当前对 task='unknown' 走 fallback 分支(打 results_dict 全量). 可优化为从 config.task 传 task 进来作 fallback. 优先级低, 不影响功能, 列在 ADR-006 后续工作里跟踪
- **`search_dirs` 测试增量** — 加 D7 时同时加"传 2 个目录, 第 1 个命中"和"第 1 个没命中走第 2 个"这两条覆盖

## 8. 修订记录

- **2026-05-24**: 初版 (Accepted)
