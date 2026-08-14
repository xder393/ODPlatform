#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : plot_style.py
# @Author    : ODPlatform team
# @Project   : ODPlatform
# @Function  : matplotlib 学术发表风格(显式调用版, 不污染全局 import)
"""学术发表风格的 matplotlib 设置."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


_ACADEMIC_RCPARAMS: dict[str, object] = {
    "font.family":        ["Times New Roman", "SimSun"],
    "font.size":          14,
    "axes.titlesize":     18,
    "axes.labelsize":     16,
    "xtick.labelsize":    14,
    "ytick.labelsize":    14,
    "legend.fontsize":    14,
    "figure.titlesize":   20,
    "savefig.dpi":        600,
    "savefig.format":     "png",
    "savefig.bbox":       "tight",
    "savefig.pad_inches": 0.1,
    "figure.constrained_layout.use": True,
}


def apply_academic_style(
    *,
    use_matplotx: bool = True,
    matplotx_style: str = "pitaya_smoothie_light",
) -> bool:
    """对当前 Python 进程的 matplotlib 全局应用学术发表风格.

    ⚠️ 调用此函数后, 当前 Python 进程内所有 matplotlib 出图都会受影响.
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib 未安装, 跳过学术风格应用")
        return False

    # rcParams (基础配置, 永远生效)
    plt.rcParams.update(_ACADEMIC_RCPARAMS)
    logger.info(f"已应用学术 rcParams ({len(_ACADEMIC_RCPARAMS)} 项)")

    # matplotx 配色 (可选, 没装也不报错)
    if use_matplotx:
        try:
            import matplotx
            if "_" in matplotx_style and matplotx_style.endswith(("_light", "_dark")):
                style_name, _, variant = matplotx_style.rpartition("_")
                style_dict = getattr(matplotx.styles, style_name, None)
                if isinstance(style_dict, dict) and variant in style_dict:
                    plt.style.use(style_dict[variant])
                    logger.info(f"已应用 matplotx 配色: {matplotx_style}")
                else:
                    logger.warning(f"matplotx 找不到 style: {matplotx_style}")
            else:
                plt.style.use(matplotx_style)
                logger.info(f"已应用 matplotx 配色: {matplotx_style}")
        except ImportError:
            logger.info("matplotx 未安装, 跳过配色(rcParams 仍生效)")
        except (KeyError, AttributeError, ValueError) as e:
            logger.warning(f"matplotx 配色应用失败: {e}(rcParams 仍生效)")

    return True
