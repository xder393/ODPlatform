# -*- mode: python ; coding: utf-8 -*-
"""ODPlatform 桌面端 PyInstaller 打包配置 (onedir).

用法 (在 Windows 上, 从仓库根目录):
    pyinstaller apps/desktop/packaging/odp-desktop.spec
产物:
    dist/ODPlatform/ODPlatform.exe
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


def _find_repo_root(start: Path) -> Path:
    """向上找 .odp-workspace 定位仓库根 (不依赖目录层级)."""
    for p in [start, *start.parents]:
        if (p / ".odp-workspace").exists():
            return p
    raise RuntimeError("找不到 .odp-workspace, 请确认从 ODPlatform 仓库内打包")


REPO_ROOT = _find_repo_root(Path(SPECPATH).resolve())
PLATFORM_SRC = REPO_ROOT / "apps" / "platform" / "src"
DESKTOP_DIR = REPO_ROOT / "apps" / "desktop"

# ultralytics / torch 有动态 import, PyInstaller 钩子可能漏, collect_all 兜底
datas, binaries, hiddenimports = [], [], []
for pkg in ("ultralytics",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(DESKTOP_DIR / "main.py")],
    pathex=[str(PLATFORM_SRC), str(DESKTOP_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "matplotlib.tests", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ODPlatform",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # GUI 应用, 不弹黑框
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ODPlatform",
)
