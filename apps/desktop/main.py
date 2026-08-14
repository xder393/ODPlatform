#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : main.py
# @Project   : ODPlatform
# @Function  : 桌面端启动入口 (开发阶段, 无需安装 package)
#
# 用法:
#   python apps/desktop/main.py
"""ODPlatform 桌面端入口."""
import sys
from pathlib import Path

# 把 platform 的 src 加到 sys.path, 让 odp_platform 可 import
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLATFORM_SRC = REPO_ROOT / "apps" / "platform" / "src"
sys.path.insert(0, str(PLATFORM_SRC))

from odp_desktop.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
