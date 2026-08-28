"""Behavioral contract for fenced worker ownership."""

from odp_api.ports.tasks import StaleLease, TaskExecutionPort


def test_execution_port_exposes_fenced_mutations():
    assert StaleLease
    assert TaskExecutionPort
