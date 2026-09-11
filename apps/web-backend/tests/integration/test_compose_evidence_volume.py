"""Exercise the shipped MinIO mount across down/up, in a disposable project."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).parents[4]
pytestmark = pytest.mark.skipif(
    os.getenv("ODP_DOCKER_RUNTIME_TEST") != "1",
    reason="requires Docker and the prebuilt backend image",
)


def test_evidence_bytes_survive_compose_down_and_up(tmp_path):
    # Removing the /data declaration must break this test: Docker creates a
    # fresh anonymous volume on the second up, leaving the evidence unreachable.
    result = subprocess.run(
        ["docker", "compose", "-f", "deploy/compose.yaml", "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=30,
    )
    config = json.loads(result.stdout)
    project = f"odp-evidence-test-{uuid4().hex}"
    service = config["services"]["minio"]
    service.pop("ports", None)
    volumes = {
        mount["source"]: {}
        for mount in service.get("volumes", []) if mount["type"] == "volume"
    }
    compose_file = tmp_path / "compose.json"
    compose_file.write_text(json.dumps({"services": {"minio": service}, "volumes": volumes}))
    compose = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    created_volumes = set()
    client = f"{project}-client"
    payload = b"original-inspection-evidence:" + os.urandom(32)
    expected = {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
    script = """
import hashlib
import io
import json
import sys
from minio import Minio
storage = Minio('minio:9000', access_key='minioadmin', secret_key='minioadmin', secure=False)
if sys.argv[1] == 'put':
    storage.make_bucket('persistence-test')
    payload = bytes.fromhex(sys.argv[2])
    storage.put_object('persistence-test', 'frame.jpg', io.BytesIO(payload), len(payload))
response = storage.get_object('persistence-test', 'frame.jpg')
try:
    content = response.read()
    print(json.dumps({'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}))
finally:
    response.close()
    response.release_conn()
"""
    try:
        for phase in ("put", "get"):
            subprocess.run(
                [*compose, "up", "-d", "--wait", "--wait-timeout", "60", "--pull", "never"],
                capture_output=True, text=True, check=True, timeout=90,
            )
            container = subprocess.check_output([*compose, "ps", "-q", "minio"], text=True).strip()
            mounts = json.loads(subprocess.check_output(
                ["docker", "inspect", container, "--format", "{{json .Mounts}}"], text=True,
            ))
            created_volumes.update(
                mount["Name"] for mount in mounts
                if mount["Type"] == "volume" and mount["Destination"] == "/data"
            )
            result = subprocess.run(
                ["docker", "run", "--rm", "--name", client, "--pull=never",
                 "--network", f"{project}_default", config["services"]["api"]["image"],
                 "python", "-c", script, phase, *([payload.hex()] if phase == "put" else [])],
                capture_output=True, text=True, check=False, timeout=30,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == expected, "original per-run evidence did not survive"
            subprocess.run([*compose, "down"], capture_output=True, check=True, timeout=30)
        assert len(created_volumes) == 1, "down/up did not reattach the original evidence volume"
    finally:
        subprocess.run(["docker", "rm", "-f", client], capture_output=True, timeout=15, check=False)
        subprocess.run([*compose, "down", "--volumes"], capture_output=True, timeout=30, check=False)
        # Only IDs observed on our unique test containers. If the mount regresses
        # to anonymous storage, also remove the first volume orphaned by down/up.
        for volume in created_volumes:
            subprocess.run(["docker", "volume", "rm", volume],
                           capture_output=True, timeout=15, check=False)
