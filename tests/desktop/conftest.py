"""把 apps/desktop 和 apps/platform/src 加到 sys.path.

桌面端任务层 (odp_desktop.tasks) 是纯函数、不依赖 PySide6/torch,
所以它的测试可以跟平台测试一起在 CI 里跑.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "desktop"))
sys.path.insert(0, str(REPO_ROOT / "apps" / "platform" / "src"))
