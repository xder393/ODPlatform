from pathlib import Path

import pytest

from odp_platform.data_pipeline.split.splitter import split_pairs


def _fake_pairs(n: int):
    """造 n 对假 Path——splitter 不读硬盘, 假的就够。"""
    return [(Path(f"img_{i}.jpg"), Path(f"img_{i}.txt")) for i in range(n)]


class TestNormalCases:
    def test_default_8_1_1(self):
        m = split_pairs(_fake_pairs(100), 0.8, 0.1)
        assert len(m.train) == 80
        assert len(m.val)   == 10
        assert len(m.test)  == 10

    def test_seven_three_zero(self):
        """边界 4: test_rate 浮点 ≈ 0, 必须 RATE_EPSILON 兜底。"""
        m = split_pairs(_fake_pairs(100), 0.7, 0.3)
        assert len(m.train) == 70
        assert len(m.val)   == 30
        assert len(m.test)  == 0

    def test_seven_zero_three(self):
        """边界 5: val_rate ≈ 0 (对称 case)。"""
        m = split_pairs(_fake_pairs(100), 0.7, 0.0)
        assert len(m.train) == 70
        assert len(m.val)   == 0
        assert len(m.test)  == 30


class TestEdgeCases:
    def test_n_lt_3_all_train(self):
        m = split_pairs(_fake_pairs(2), 0.8, 0.1)
        assert len(m.train) == 2
        assert len(m.val)   == 0
        assert len(m.test)  == 0

    def test_train_rate_one(self):
        m = split_pairs(_fake_pairs(10), 1.0, 0.0)
        assert len(m.train) == 10

    def test_zero_pairs(self):
        m = split_pairs(_fake_pairs(0), 0.8, 0.1)
        assert m.summary()["total"] == 0


class TestReproducibility:
    def test_same_seed_same_split(self):
        pairs = _fake_pairs(50)
        m1 = split_pairs(pairs, 0.8, 0.1, random_state=42)
        m2 = split_pairs(pairs, 0.8, 0.1, random_state=42)
        assert m1.train == m2.train


class TestErrors:
    def test_invalid_rates_raise(self):
        with pytest.raises(ValueError):
            split_pairs(_fake_pairs(10), 0.6, 0.6)  # 总和 > 1
