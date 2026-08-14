# 打包 Windows .exe 步骤

> 硬约束：PyInstaller **不支持交叉打包**。必须在 **Windows** 机器上执行下面的
> 步骤，macOS/Linux 上打不出 Windows 的 exe。

## 1. 准备 Windows 环境

在一台 Windows 上装好 Python 3.10~3.12，然后：

```powershell
# 建议用干净虚拟环境
python -m venv venv
venv\Scripts\activate

# 装依赖 (torch/ultralytics 较大, 一起装才能打出能训练/推理的包)
pip install PySide6 pyinstaller
pip install -e .\apps\platform

# 要能真正训练/评估/推理, 再装:
pip install torch ultralytics
```

## 2. 打包

在仓库根目录：

```powershell
pyinstaller apps\desktop\packaging\odp-desktop.spec
```

产物在 `dist\ODPlatform\ODPlatform.exe`（onedir 模式，旁边是一堆依赖文件，
整个 `dist\ODPlatform\` 目录都要一起分发）。

## 3. 常见问题

- **体积大**：torch + ultralytics + PySide6 打包出来通常 2~4 GB，正常。
  不想打训练能力，可在 spec 里注释掉 `collect_all("ultralytics")`，只保留
  数据转换/质检/配置生成（但要确认 GUI 里 train/val/infer 按钮会优雅报错）。
- **启动报缺 DLL / Qt 插件**：先跑 `pyinstaller --clean` 重打；PySide6 的
  Qt 插件一般由 PyInstaller 自带 hook 自动收集。
- **onefile 还是 onedir**：当前 spec 是 onedir（启动快、好排错）。要单文件
  就把 spec 里的 `COLLECT` 去掉、`EXE` 里 `exclude_binaries=False`，但单文件
  启动会慢很多（每次解压）。
- **报 `ultralytics` 找不到**：确认第 1 步在同一个 venv 里 `pip install -e .`
  和 `pip install torch ultralytics` 都做了，PyInstaller 才能收集到。

## 4. 验证

打包后在 Windows 上双击 `ODPlatform.exe`，界面能起、`数据转换`/`质量检查`能跑，
就说明打包链路通了。训练/评估/推理取决于当时有没有把 torch 打进去。
