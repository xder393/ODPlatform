# apps/desktop — ODPlatform 桌面端 (PySide6)

给核心引擎 `apps/platform/` 套一层 GUI：拖拽导入数据集、格式转换、质量检查、
配置生成、任务执行(训练/评估/推理)、结果展示(表格 + 图表)。

拖拽支持：把数据集**文件夹或 zip** 拖到「数据集」框里，自动整理成
`data/raw/<名字>/{images, annotations}/`（兼容平铺 / images+annotations /
VOC 的 JPEGImages+Annotations 三种布局）。

## 结构

```
apps/desktop/
├── main.py                 开发入口 (python apps/desktop/main.py)
├── odp_desktop/
│   ├── app.py              主窗口 (左控制面板 + 日志/结果/图表三页)
│   ├── log_bridge.py       把 logging 流接到 Qt 信号 (日志面板实时滚动)
│   ├── workers.py          后台线程跑任务 (UI 不冻结, 状态栏进度条)
│   ├── charts.py           结果图表 (matplotlib, 验证/评估指标可视化)
│   └── tasks.py            服务层 → 普通 dict 的薄封装
└── packaging/
    ├── odp-desktop.spec    PyInstaller 打包配置
    └── build_windows.md    打包步骤
```

## 依赖

```bash
pip install PySide6
```

`odp_platform` 本体需先装好 (`pip install -e ./apps/platform`)，桌面端只 import
它、不重复实现业务。torch / ultralytics 是可选重依赖——没装时 GUI 照常启动，
训练/评估/推理会返回"未安装"的明确错误，数据转换/质检/配置生成照常能跑。

## 开发运行

```bash
cd ODPlatform
python apps/desktop/main.py
# 或者:
#   cd apps/desktop && python -m odp_desktop
```

## 打包成 Windows .exe

见 `packaging/build_windows.md`。要点：**必须在 Windows 机器上打**，
PyInstaller 不支持在 macOS/Linux 上交叉打包 Windows exe。
