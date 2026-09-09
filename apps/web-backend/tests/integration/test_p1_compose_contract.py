"""Compose must expose independent P1 process entrypoints."""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).parents[4] / "deploy" / "compose.yaml"


def test_compose_declares_independent_realtime_processes():
    document = yaml.safe_load(COMPOSE.read_text())
    services = document["services"]
    expected = {
        "frame-ingestor",
        "outbox-relay",
        "inference-worker-1",
        "inference-worker-2",
        "recovery-scheduler",
        "artifact-reconciler",
        "stream-retention",
    }
    assert expected <= services.keys()
    for name in expected:
        command = services[name]["command"]
        assert "odp_api.processes." in " ".join(command if isinstance(command, list) else [command])


def test_deployed_processes_do_not_use_sqlite_task_state():
    document = yaml.safe_load(COMPOSE.read_text())
    process_roles = {
        "frame-ingestor": "odp_worker",
        "outbox-relay": "odp_relay",
        "inference-worker-1": "odp_worker",
        "inference-worker-2": "odp_worker",
        "recovery-scheduler": "odp_scheduler",
        "artifact-reconciler": "odp_scheduler",
        "stream-retention": "odp_scheduler",
    }
    for name, service in document["services"].items():
        if name in {"postgres", "redis", "minio", "web", "prometheus", "grafana", "alertmanager", "migrate"}:
            continue
        environment = service.get("environment", {})
        assert not any("TASK_DATABASE_PATH" in str(key) for key in environment)
        assert not any("task-state" in str(volume) for volume in service.get("volumes", []))
        if name in process_roles:
            assert process_roles[name] in environment["ODP_DATABASE_URL"]
