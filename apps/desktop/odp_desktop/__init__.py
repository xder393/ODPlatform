# -*- coding:utf-8 -*-
"""ODPlatform 桌面端 (PySide6).

薄 GUI 层 — 不实现业务, 只把 odp_platform 的服务层包装成界面:
  - log_bridge: 把 logging 流接到 Qt 信号
  - workers:     后台线程跑任务, 不卡 UI
  - tasks:       服务层 → 普通 dict 的薄封装
  - app:         主窗口
"""
