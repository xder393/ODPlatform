# data — 数据集目录

```
data/
├── raw/     <- 原始数据集, 每个数据集一个文件夹: raw/<数据集名>/{images, annotations}/
├── train/   <- 划分后的训练集(images/ + labels/, 由 odp-transform 产出)
├── val/     <- 验证集
└── test/    <- 测试集
```

`raw/` 下的数据不进 git(见 .gitignore)。
