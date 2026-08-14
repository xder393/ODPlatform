"""BaseConfig 行为契约测试."""
from __future__ import annotations

from typing import ClassVar, Optional

import pytest
from pydantic import Field, ValidationError

from odp_platform.runtime_config.base import BaseConfig


# ============================================================
# 不能直接实例化 BaseConfig (抽象基类语义)
# ============================================================

class _ConcreteTestConfig(BaseConfig):
    """测试用最小子类."""
    value: int = Field(
        default=0,
        description="测试字段(供 audit_snapshot 往返测试用)",
        json_schema_extra={"group": "核心参数"},
    )


def test_base_config_default_values():
    """BaseConfig 子类默认值应正确."""
    c = _ConcreteTestConfig()
    assert c.batch     == 16
    assert c.imgsz     == 640
    assert c.workers   == 8
    assert c.cache     is False
    assert c.device    is None
    assert c.amp       is True
    assert c.verbose   is True


def test_base_config_extra_forbid():
    """extra='forbid' 拒绝拼错的字段名."""
    with pytest.raises(ValidationError) as exc_info:
        _ConcreteTestConfig(epchs=300)         # epochs 拼错
    assert "epchs" in str(exc_info.value)


def test_base_config_framework_only_fields_default():
    """BaseConfig 默认 FRAMEWORK_ONLY_FIELDS 只有 verbose."""
    assert BaseConfig.FRAMEWORK_ONLY_FIELDS == {"verbose"}


def test_to_ultralytics_kwargs_drops_framework_fields():
    """to_ultralytics_kwargs 过滤 FRAMEWORK_ONLY_FIELDS."""
    c = _ConcreteTestConfig(verbose=False)
    kw = c.to_ultralytics_kwargs()
    assert "verbose" not in kw, "verbose 不应该传给 ultralytics"


def test_to_ultralytics_kwargs_drops_none():
    """to_ultralytics_kwargs 过滤 None 值."""
    c = _ConcreteTestConfig()
    kw = c.to_ultralytics_kwargs()
    assert "device" not in kw, "默认 None 的字段不应该出现"


def test_to_ultralytics_kwargs_keeps_false_and_zero():
    """to_ultralytics_kwargs 必须保留 False / 0 / '' 等显式值."""
    c = _ConcreteTestConfig(cache=False, amp=False)
    kw = c.to_ultralytics_kwargs()
    assert kw["cache"] is False
    assert kw["amp"]   is False


def test_get_field_groups():
    """get_field_groups 返回有序的分组字典."""
    c = _ConcreteTestConfig()
    groups = c.get_field_groups()
    # 至少 BaseConfig 自己的 4 个组在
    expected = {"核心参数", "输入配置", "设备配置", "输出配置", "基础设置"}
    assert expected <= set(groups.keys())


def test_get_field_metadata_complete():
    """每个字段都有完整的 group/examples/tips/yaml_comment 4 个槽位."""
    c = _ConcreteTestConfig()
    for field_name in _ConcreteTestConfig.model_fields:
        metadata = c.get_field_metadata(field_name)
        # description 必填
        assert metadata.get("description"), f"{field_name} 缺 description"
        # 至少有一个用户向元数据(说明 SSoT 思路落到位)
        has_user_meta = any([
            metadata.get("group"),
            metadata.get("examples"),
            metadata.get("yaml_comment"),
        ])
        assert has_user_meta, f"{field_name} 缺用户向元数据"


# ============================================================
# 敏感字段 / 复现接口 (在内部 fixture 类上跑)
# ============================================================

class _SensitiveTestConfig(BaseConfig):
    """用于测试 sensitive 字段处理的最小内部 fixture."""
    api_key: Optional[str] = Field(
        default=None,
        description="API 密钥示例 (测试用)",
        json_schema_extra={"group": "核心参数", "sensitive": True},
    )
    public_param: int = Field(
        default=10,
        description="普通参数 (测试用)",
        json_schema_extra={"group": "核心参数", "sensitive": False},
    )


# ============================================================
# 实验复现 audit_snapshot
# ============================================================

def test_audit_snapshot_roundtrip():
    """to_audit_snapshot → from_audit_snapshot 应完全恢复."""
    config = _ConcreteTestConfig(value=42, name="hello")
    snapshot = config.to_audit_snapshot()

    assert snapshot["config_class"]  == "_ConcreteTestConfig"
    assert snapshot["config_module"] == _ConcreteTestConfig.__module__
    assert "frozen_at" in snapshot
    assert snapshot["values"]["value"] == 42

    restored = _ConcreteTestConfig.from_audit_snapshot(snapshot)
    assert restored.value == 42
    assert restored.name == "hello"


def test_audit_snapshot_wrong_class_raises():
    """用错类恢复快照应报错."""
    config = _ConcreteTestConfig(value=1)
    snapshot = config.to_audit_snapshot()

    with pytest.raises(ValueError, match="不能恢复"):
        _SensitiveTestConfig.from_audit_snapshot(snapshot)


def test_audit_snapshot_json_serializable():
    """快照必须可 json.dumps (实验数据库通常 JSON 列存)."""
    import json
    config = _ConcreteTestConfig(value=42)
    snapshot = config.to_audit_snapshot()
    text = json.dumps(snapshot, ensure_ascii=False)
    restored = json.loads(text)
    assert restored["values"]["value"] == 42


# ============================================================
# 敏感字段 mask
# ============================================================

def test_mask_sensitive_dump_masks_marked_fields():
    c = _SensitiveTestConfig(api_key="super_secret_123", public_param=42)
    masked = c.mask_sensitive_dump()
    assert masked["api_key"] == "***"
    assert masked["public_param"] == 42         # 没标 sensitive 不动


def test_mask_sensitive_dump_keeps_none():
    """None 值的 sensitive 字段不应 mask 成 '***'."""
    c = _SensitiveTestConfig(api_key=None)
    masked = c.mask_sensitive_dump()
    assert masked["api_key"] is None


def test_sensitive_field_names_reflects_correctly():
    assert _SensitiveTestConfig.sensitive_field_names() == {"api_key"}


def test_custom_sensitive_mask_via_classvar():
    """子类改 SENSITIVE_MASK 应生效."""
    class _RedactedConfig(_SensitiveTestConfig):
        SENSITIVE_MASK: ClassVar[str] = "<REDACTED>"

    c = _RedactedConfig(api_key="x")
    assert c.mask_sensitive_dump()["api_key"] == "<REDACTED>"
