# -*- coding:utf-8 -*-
"""支持 `python -m odp_desktop` (在 apps/desktop/ 目录下)."""
import sys
from pathlib import Path

# __main__.py 在 odp_desktop/ 里, 往上 4 层是仓库根
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PLATFORM_SRC = REPO_ROOT / "apps" / "platform" / "src"
if str(PLATFORM_SRC) not in sys.path:
    sys.path.insert(0, str(PLATFORM_SRC))

from odp_desktop.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
