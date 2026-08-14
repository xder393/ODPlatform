"""data_pipeline.core —— 各标注格式的 converter 实现。

每个 <format>.py 模块通过 @register 装饰器把自己登记进注册表。
新增格式 = 新增一个文件, 无需改动 registry / service / 其他 converter。
"""
