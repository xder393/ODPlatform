"""config_log (log_effective_config / log_override_chains) 行为契约测试."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

from odp_platform.common.config_log import log_effective_config, log_override_chains


def test_log_effective_config_reads_each_field(caplog):
    """每个字段输出一行 + 来源."""
    caplog.set_level(logging.INFO)

    fake_config = MagicMock()
    fake_config.__class__.model_fields = {"epochs": ..., "batch": ..., "lr0": ...}
    fake_config.epochs = 100 ; fake_config.batch = 16 ; fake_config.lr0 = 0.01

    fake_merger = MagicMock()
    fake_merger.get_metadata = MagicMock(side_effect=lambda n: MagicMock(source_label="CLI"))

    log_effective_config(fake_config, fake_merger)
    assert "epochs" in caplog.text and "100" in caplog.text and "CLI" in caplog.text


def test_log_override_chains_reverse_order(caplog):
    """chain 显示按 DEFAULT → CLI 方向(reverse D5 chain)."""
    from datetime import datetime

    from odp_platform.runtime_config.merger import ConfigMetadata, ConfigSource

    caplog.set_level(logging.INFO)

    fake_config = MagicMock()
    fake_config.__class__.model_fields = {"lr0": ...}
    fake_config.lr0 = 0.001

    meta = ConfigMetadata(
        key="lr0", value=0.001, source=ConfigSource.CLI,
        timestamp=datetime.now(),
        overridden_from=ConfigMetadata(
            key="lr0", value=0.01, source=ConfigSource.YAML,
            timestamp=datetime.now(),
            overridden_from=ConfigMetadata(
                key="lr0", value=0.01, source=ConfigSource.DEFAULT,
                timestamp=datetime.now(),
            ),
        ),
    )
    fake_merger = MagicMock()
    fake_merger.get_metadata = MagicMock(return_value=meta)

    log_override_chains(fake_config, fake_merger)

    assert "DEFAULT" in caplog.text and "YAML" in caplog.text and "CLI" in caplog.text
    assert caplog.text.index("DEFAULT") < caplog.text.index("YAML") < caplog.text.index("CLI")


def test_safe_get_metadata_returns_none_for_mock_without_method(caplog):
    """merger 没 get_metadata — 不崩."""
    caplog.set_level(logging.INFO)

    fake_config = MagicMock()
    fake_config.__class__.model_fields = {"epochs": ...}
    fake_config.epochs = 100

    bad_merger = object()      # 完全没 get_metadata
    log_effective_config(fake_config, bad_merger)   # 不 raise 才算过
    assert "epochs" in caplog.text                   # 字段仍然打了, 只是来源 'unknown'
