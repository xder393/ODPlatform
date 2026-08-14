# ADR-002: data_pipeline 子系统架构

**Date**: 2026-03-15 (D3)
**Status**: Accepted

## Context

ODPlatform 需要把多种格式的原始标注数据集 (VOC / COCO / YOLO)
转换成 ultralytics 训练所需的 YOLO 格式 + 划分 + yaml, 暴露成
一条 CLI 命令。

## Decision

采用**单顶层目录 + 双内部子包**结构:

```
data_pipeline/
├── registry.py       (框架: 注册表 + ConvertOptions)
├── service.py        (框架: 调度层)
├── orchestrator.py   (端到端编排)
├── core/             (实现: 各格式 converter)
└── split/            (划分/落盘/yaml)
```

九条核心原则:

1. **注册表 + 自动发现**: 加新格式 = 加新文件, 不改老代码 (开闭原则)。`@register` 装饰器登记到 `_REGISTRY`, `_lazy_init` 用 `pkgutil.iter_modules` 扫 `core/` 包自动 import——新增 `core/<format>.py` 后注册表代码一行都不用动
2. **接口等价性**: yolo 直通也"写"到 output_labels_dir, 调用方看到所有 converter 行为一致
3. **统一参数包**: ConvertOptions 统一签名, 各 converter 按需取用
4. **能力声明**: @register(supported_tasks=...), 不匹配在 service 层 fail-fast
5. **纯函数 + IO 分离**: split_pairs 纯函数 (sub-second 测试), materializer 才碰盘
6. **依赖注入**: materializer 不 import paths, 通过 SplitOutputDirs 接收目标目录
7. **元数据持久化**: yaml 的 odp_meta 块记录 random_state / 划分比例 / 时间, 支持复现
8. **入参与状态分离**: `_user_classes` (契约, 不变) vs `_final_classes` (运行时确定); 避免"同一变量两种含义"的 bug
9. **会话级临时状态用 tempfile**: 中转目录不放项目内 (`data/raw/` 洁癖铁律), 用系统临时目录, 自动清理

## Alternatives Considered

### A. 顶层平铺 (data_converter/ + data_splitter/ + pipelines/ 三个顶层目录)

**否决**: 顶层目录数量会随业务阶段(D6 验证 / D7 训练 / D8 推理)
线性膨胀。每个新阶段都加 2-3 个顶层目录, 学员看顶层就看不清"业务地图"。

我们坚持**按业务阶段拆顶层目录**:
```
data_pipeline/    (D3, 本节)
data_validation/  (D6, 下节)
training/         (D7)
inference/        (D8)
```

而**技术职能拆分(converter / splitter / orchestrator)留在子目录内部**——
顶层回答"项目有哪几件大事", 子目录回答"这件大事内部分几步"。

### B. 单文件 data_pipeline.py

**否决**: 加格式时整个文件越来越胖, 多人协作 merge 冲突频繁,
违反单一职责。一个 2000 行的 data_pipeline.py 没有任何工程师想 review。

### C. 让 ultralytics.convert_coco 直接写到 output_labels_dir

**否决**: ultralytics 的输出结构跟我们需要的不一致 (它会建子目录),
直接写会污染 output_labels_dir 的结构。改用 tempfile.mkdtemp()
中转 — 不污染 data/raw/, 不污染仓库, 进程结束自然清理。

### D. 让 yolo "converter" 啥也不做, 由 orchestrator 处理位置

**否决**: 这种方案让 orchestrator 处处要写 `if format == "yolo"` 特殊处理。
违反开闭原则的"分发漂移"问题会重演——每加一种"原生格式"都要在 orchestrator 加分支。
改用**接口等价性**: yolo 也通过硬链接/复制"写"到 output_labels_dir,
orchestrator 一行 if 都不需要。

### E. 用工厂模式而不是注册表

**否决**: 工厂内部还是 `if/elif`, 加新格式还是要改工厂。注册表+@register 才真正
做到"加新文件 = 加新格式", 老代码一行不动。

### F. materializer 直接 import paths.py 而不用 DI

**否决**: 单元测试时无法把输出导向临时目录, 要么污染真实 `data/train/`, 要么
monkey-patch paths 模块——两个都是 hack。多输出目标(挂载点/网络盘)时也要改
materializer 本身。改用 SplitOutputDirs 注入, 调用方决定路径, 测试和复用都简单。

## Known Boundaries

- **task=segment 端到端**: 目前只有 coco converter 支持 segment 输出,
  但 orchestrator 的 yaml schema 未适配 polygon 标签 — 推迟到 D3.2。
- **K-fold 划分**: 当前只支持 train/val/test 三分。K-fold 是
  split_pairs 的扩展, 不是 orchestrator 的 — 推迟到 D3.x。
- **数据验证**: 当前只在 orchestrator 入口做覆盖率前置, 更深入的
  yolo txt 越界 / 类别分布检查推迟到 D6 data_validation/。

## References

- D3 讲义 (本文档配套)
- ultralytics docs: https://docs.ultralytics.com/datasets/
- sklearn train_test_split: 注意 train_size 必须在开区间 (0.0, 1.0)
