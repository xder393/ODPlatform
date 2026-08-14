#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""ODPlatform 项目重置工具 —— 命令行入口(薄壳)。

实际逻辑在 odp_platform.cli.reset_project.main 里。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "platform" / "src"))

from odp_platform.cli.reset_project import main

if __name__ == "__main__":
    sys.exit(main())
