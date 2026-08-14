#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : scripts/init_project.py
# @Function  : 项目初始化的【开发阶段入口】(无需安装 package)
#
# 用法:
#   python scripts/init_project.py
"""ODPlatform 项目初始化入口(开发阶段)"""

import sys
from pathlib import Path

# 从仓库根定位 platform 的 src 目录
REPO_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_SRC = REPO_ROOT / "apps" / "platform" / "src"

# 把 src 加到 sys.path 最前面(优先于已安装版本)
sys.path.insert(0, str(PLATFORM_SRC))

# 导入并运行真正的初始化函数
from odp_platform.cli.init_project import initialize_project

if __name__ == "__main__":
    initialize_project()
