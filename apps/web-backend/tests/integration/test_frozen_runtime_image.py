"""Opt-in acceptance of the actual Compose runtime image, without host code/network.

Run after `docker compose -f deploy/compose.yaml build api` with
ODP_DOCKER_RUNTIME_TEST=1. Missing dependencies, migration assets or a runtime
installer regression must fail here, not be hidden by the developer's venv.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).parents[4]
BACKEND_SERVICES = (
    "api", "migrate", "storage-init", "frame-ingestor", "outbox-relay",
    "inference-worker-1", "inference-worker-2", "recovery-scheduler",
    "artifact-reconciler", "stream-retention",
)
pytestmark = pytest.mark.skipif(
    os.getenv("ODP_DOCKER_RUNTIME_TEST") != "1",
    reason="requires explicitly enabled, prebuilt Docker runtime image",
)


@pytest.fixture(scope="module")
def runtime_config():
    result = subprocess.run(
        ["docker", "compose", "-f", "deploy/compose.yaml", "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def run_offline_image(image, command):
    # A host-side timeout must not leave the test container running in Docker.
    name = f"odp-frozen-test-{uuid4().hex}"
    try:
        return subprocess.run(
            ["docker", "run", "--name", name, "--rm", "--pull=never",
             "--network=none", image, *command],
            capture_output=True, text=True, check=False, timeout=90,
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, check=False, timeout=15,
        )


def test_all_backend_processes_use_the_same_built_runtime(runtime_config):
    image_ids = set()
    for name in BACKEND_SERVICES:
        image = runtime_config["services"][name]["image"]
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True, check=True, timeout=15,
        )
        image_ids.add(result.stdout.strip())
    assert len(image_ids) == 1, "backend processes must not drift between environments"


def test_image_imports_and_migrates_without_network_or_host_source(runtime_config):
    # A bare Python image, missing COPY, wrong PYTHONPATH or runtime dependency
    # drift all break these observable imports/migrations in an isolated container.
    script = """
import importlib
import importlib.metadata as metadata
import json
import os
import subprocess
from pathlib import Path
import cv2
import numpy as np
import onnxruntime as ort
import odp_schemas
from odp_api.database_roles import bootstrap_sql, grant_sql
from odp_api.migrations import MIGRATIONS_DIR
from odp_api.adapters.vision.onnx import OnnxVisionAdapter

for name in ('frame_ingestor', 'outbox_relay', 'inference_worker',
             'recovery_scheduler', 'artifact_reconciler', 'stream_retention'):
    importlib.import_module('odp_api.processes.' + name)
assert bootstrap_sql() and grant_sql()
assert len(list(MIGRATIONS_DIR.glob('*.sql'))) >= 4
model = Path('/workspace/apps/web-backend/tests/fixtures/models/tiny-detector.onnx')
import hashlib
OnnxVisionAdapter(model, model_sha256=hashlib.sha256(model.read_bytes()).hexdigest())
assert cv2.imencode('.jpg', np.zeros((64, 64, 3), dtype=np.uint8))[0]
subprocess.run(['alembic', 'upgrade', 'head'],
               cwd='/workspace/apps/web-backend', check=True,
               env={**os.environ, 'ODP_DATABASE_URL': 'sqlite:////tmp/offline-image.db'})
print(json.dumps({name: metadata.version(name) for name in
    ('fastapi', 'numpy', 'onnxruntime', 'opencv-python-headless', 'alembic')}))
"""
    result = run_offline_image(
        runtime_config["services"]["api"]["image"], ["python", "-c", script],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # Python 3.12 versions from the reviewed lock, not the installed host venv.
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "fastapi": "0.141.1", "numpy": "2.5.3", "onnxruntime": "1.29.0",
        "opencv-python-headless": "4.14.0.94", "alembic": "1.19.1",
    }


def test_compose_api_command_starts_offline(runtime_config):
    # Exercise Compose's real command, not a string search for pip/uv. Local
    # SQLite is intentional here; the full-stack E2E gate covers external stores.
    script = """
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

with tempfile.TemporaryFile(mode='w+t') as output:
    process = subprocess.Popen(sys.argv[1:], stdout=output, stderr=output, env={
        **os.environ, 'ODP_ENVIRONMENT': 'test',
        'ODP_DATABASE_URL': 'sqlite:////tmp/offline-api.db',
        'PIP_NO_INDEX': '1', 'UV_OFFLINE': '1',
    })
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=1) as response:
                    assert json.load(response) == {'status': 'ok'}
                    print('offline-api-healthy', flush=True)
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        else:
            output.seek(0)
            raise RuntimeError(output.read())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
"""
    api = runtime_config["services"]["api"]
    result = run_offline_image(api["image"], ["python", "-c", script, *api["command"]])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offline-api-healthy" in result.stdout


@pytest.mark.parametrize(("additional_requirement", "expected_error"), [
    ("", None),
    ("odp-schema-missing-dependency==1.0", "missing shared-schema dependency"),
    ("pydantic<2", "incompatible shared-schema dependency"),
])
def test_shared_schema_dependency_contract(runtime_config, additional_requirement, expected_error):
    # Source is copied rather than packaged. Check its declared requirements
    # against the actual image so a future independent schema change fails CI.
    # TOML is parsed inside the pinned Python 3.12 image, not by the host Python.
    script = """
import importlib.metadata as metadata
import sys
import tomllib
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

project = tomllib.loads(sys.argv[1])['project']
assert '.'.join(map(str, sys.version_info[:3])) in SpecifierSet(project['requires-python'])
requirements = project['dependencies'] + ([sys.argv[2]] if sys.argv[2] else [])
for value in requirements:
    requirement = Requirement(value)
    if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
        continue
    # These need explicit lock integration, not an accidental partial check.
    assert not requirement.extras and requirement.url is None, 'schema extras/URLs need lock integration'
    try:
        version = metadata.version(requirement.name)
    except metadata.PackageNotFoundError as error:
        raise AssertionError('missing shared-schema dependency: ' + requirement.name) from error
    assert version in requirement.specifier, 'incompatible shared-schema dependency: ' + value
print('shared-schema-dependencies-satisfied')
"""
    project = (ROOT / "packages/shared-schemas/pyproject.toml").read_text()
    result = run_offline_image(
        runtime_config["services"]["api"]["image"],
        ["python", "-c", script, project, additional_requirement],
    )
    if expected_error is None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "shared-schema-dependencies-satisfied" in result.stdout
    else:
        assert result.returncode != 0, "invalid schema dependency was accepted"
        assert expected_error in result.stderr, result.stderr


def test_dependency_build_rejects_a_stale_lockfile(tmp_path):
    # Keep the locked versions valid but change dependency metadata. --frozen
    # alone would silently accept this; the offline --check must reject it.
    backend = tmp_path / "apps/web-backend"
    backend.mkdir(parents=True)
    project = (ROOT / "apps/web-backend/pyproject.toml").read_text()
    assert '"fastapi>=0.110.0"' in project
    (backend / "pyproject.toml").write_text(
        project.replace('"fastapi>=0.110.0"', '"fastapi>=0.120.0"')
    )
    shutil.copyfile(ROOT / "apps/web-backend/uv.lock", backend / "uv.lock")
    result = subprocess.run(
        ["docker", "build", "--pull=false", "--network=none", "--progress=plain",
         "--target", "dependencies", "-f", str(ROOT / "deploy/Dockerfile.backend"),
         str(tmp_path)],
        capture_output=True, text=True, check=False, timeout=90,
    )
    assert result.returncode != 0, "stale lockfile unexpectedly built"
    # Match the executed diagnostic, not BuildKit echoing the RUN instruction.
    assert re.search(
        r"(?m)^#\d+ [\d.]+ ERROR: frozen runtime lock validation failed;",
        result.stderr,
    ), result.stderr
