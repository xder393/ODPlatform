#!/usr/bin/env python
# -*- coding:utf-8 -*-
# @FileName  : performance_utils.py
# @Project   : ODPlatform
# @Function  : 性能测量工具——@time_it 装饰器

import logging
import time
from functools import wraps

# 业务模块标准写法
logger = logging.getLogger(__name__)


def time_it(
    iterations: int = 1,
    name: str = None,
    logger_instance: logging.Logger = None,
):
    """
    通用执行时间测量装饰器。

    Examples:
        @time_it()
        def my_func(): ...

        @time_it(iterations=10, name="批量推理")
        def infer_batch(): ...

        # 指定 logger:
        @time_it(logger_instance=logging.getLogger("my_module"))
        def custom(): ...
    """
    # 默认用本模块的 __name__ logger;
    # 调用方可以传一个特定 logger 进来(比如希望日志带着调用方的 logger 名)
    log = logger_instance if logger_instance is not None else logger

    def _format_time_auto_unit(seconds: float) -> str:
        """自动选择合适单位"""
        if seconds < 0.001:
            return f"{seconds * 1_000_000:.3f} 微秒"
        elif seconds < 1.0:
            return f"{seconds * 1000:.3f} 毫秒"
        elif seconds < 60:
            return f"{seconds:.2f} 秒"
        elif seconds < 3600:
            mins = seconds // 60
            secs = seconds % 60
            return f"{mins:.0f} 分钟 {secs:.2f} 秒"
        else:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            secs = (seconds % 3600) % 60
            return f"{hours:.0f} 小时 {mins:.0f} 分钟 {secs:.2f} 秒"

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            display_name = name if name is not None else func.__name__
            total = 0.0
            result = None
            for _ in range(iterations):
                start = time.perf_counter()
                result = func(*args, **kwargs)
                end = time.perf_counter()
                total += (end - start)

            avg = total / iterations
            avg_str = _format_time_auto_unit(avg)

            if iterations == 1:
                log.info(
                    f"性能报告: '{display_name}' 执行耗时: {avg_str}"
                )
            else:
                total_str = _format_time_auto_unit(total)
                log.info(
                    f"性能报告: '{display_name}' 执行 {iterations} 次 | "
                    f"总耗时: {total_str} | 平均耗时: {avg_str}"
                )

            return result
        return wrapper
    return decorator


if __name__ == "__main__":
    # 自测时单独配一个简单 handler——真实使用中, CLI 入口已经配好了, 这里不用
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s",
                        datefmt="%H:%M:%S")

    @time_it(name="测试睡眠 0.1 秒")
    def test_sleep():
        time.sleep(0.1)

    @time_it(iterations=5, name="测试快函数")
    def test_fast():
        sum(range(1000))

    test_sleep()
    test_fast()
