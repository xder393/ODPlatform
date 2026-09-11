"""Observe a disposable frame-ingestor crash/recovery drill.

The probe is deliberately an observer at the process boundary.  It creates a
recorded session through the authenticated HTTP API, then reads durable
PostgreSQL facts after the independently running ingestor, relay, and worker
have acted.  It never imports or calls an ingestion/worker loop.

The script is guarded by ``ODP_ALLOW_COMPOSE_PROBE=disposable`` because it
creates demo data and leaves the durable rows available for inspection until
the disposable Compose project is removed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from odp_api.adapters.persistence.task_models import (
    FrameArtifactRow,
    InferenceTaskRow,
    InspectionSessionRow,
    PublishedInferenceResultRow,
)

OPT_IN_VALUE = "disposable"
DEFAULT_WORKSPACE = "/workspace"
DEFAULT_API_URL = "http://api:8000"
DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 1.0
STATE_KEYS = frozenset(
    {
        "organization_id",
        "session_id",
        "first_generation",
        "highest_committed_sequence",
    }
)


class ProbeError(RuntimeError):
    """A deterministic probe failure suitable for a nonzero CLI exit."""


@dataclass(frozen=True, slots=True)
class ProbeState:
    """The only facts carried across the container crash boundary."""

    organization_id: UUID
    session_id: UUID
    first_generation: int
    highest_committed_sequence: int

    def as_dict(self) -> dict[str, object]:
        return {
            "organization_id": str(self.organization_id),
            "session_id": str(self.session_id),
            "first_generation": self.first_generation,
            "highest_committed_sequence": self.highest_committed_sequence,
        }


@dataclass(frozen=True, slots=True)
class DurableObservation:
    """A fresh-database-pool view of one recovery session."""

    organization_id: UUID
    session_id: UUID
    status: str
    sanitized_uri: str
    generation: int
    artifact_generation: int | None
    highest_committed_sequence: int
    result_frame_sequence: int | None
    artifact_id: UUID | None
    task_id: UUID | None
    result_id: UUID | None
    task_status: str | None
    artifact_state: str | None
    artifact_lifecycle: str | None

    @property
    def is_committed(self) -> bool:
        """Whether the latest observed result has durable linked evidence."""

        return (
            self.result_id is not None
            and self.task_id is not None
            and self.artifact_id is not None
            and self.task_status == "SUCCEEDED"
            and self.artifact_state == "AVAILABLE"
            and self.artifact_lifecycle == "EVIDENCE"
            and self.result_frame_sequence is not None
        )

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in (
            "organization_id",
            "session_id",
            "artifact_id",
            "task_id",
            "result_id",
        ):
            if value[key] is not None:
                value[key] = str(value[key])
        return value


def require_disposable_opt_in(environ: Mapping[str, str] | None = None) -> None:
    """Fail before any state, API, or database mutation without explicit opt-in."""

    values = os.environ if environ is None else environ
    if values.get("ODP_ALLOW_COMPOSE_PROBE") != OPT_IN_VALUE:
        raise ProbeError(
            "refusing disposable Compose probe: set "
            "ODP_ALLOW_COMPOSE_PROBE=disposable"
        )


def _workspace_path() -> Path:
    workspace = Path(os.getenv("ODP_PROBE_WORKSPACE", DEFAULT_WORKSPACE))
    try:
        return workspace.expanduser().resolve(strict=True)
    except OSError as error:
        raise ProbeError(f"probe workspace is unavailable: {workspace}") from error


def validate_state_path(value: str | Path, *, must_exist: bool = False) -> Path:
    """Validate that state lives in the shared workspace and is a JSON file."""

    path = Path(value)
    if not path.is_absolute():
        raise ProbeError("state path must be absolute and inside the shared workspace")
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(_workspace_path())
    except (OSError, ValueError) as error:
        raise ProbeError("state path must be inside the shared probe workspace") from error
    if resolved.suffix.lower() != ".json":
        raise ProbeError("state path must use a .json suffix")
    if must_exist and not resolved.is_file():
        raise ProbeError(f"state file does not exist: {resolved}")
    if not must_exist and not resolved.parent.is_dir():
        raise ProbeError(f"state parent directory does not exist: {resolved.parent}")
    return resolved


def _as_uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str):
        raise ProbeError(f"state field {field} must be a UUID string")
    try:
        return UUID(value)
    except (ValueError, AttributeError) as error:
        raise ProbeError(f"state field {field} must be a UUID string") from error


def _as_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProbeError(f"state field {field} must be a positive integer")
    return value


def parse_state(value: object) -> ProbeState:
    """Validate the exact credential-free state schema."""

    if not isinstance(value, dict):
        raise ProbeError("state must be a JSON object")
    if frozenset(value) != STATE_KEYS:
        raise ProbeError("state must contain exactly the four recovery fields")
    return ProbeState(
        organization_id=_as_uuid(value["organization_id"], "organization_id"),
        session_id=_as_uuid(value["session_id"], "session_id"),
        first_generation=_as_positive_int(value["first_generation"], "first_generation"),
        highest_committed_sequence=_as_positive_int(
            value["highest_committed_sequence"], "highest_committed_sequence"
        ),
    )


def load_state(path: str | Path) -> ProbeState:
    """Load and validate a state file without opening the database."""

    state_path = validate_state_path(path, must_exist=True)
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProbeError(f"unable to read valid JSON state from {state_path}") from error
    return parse_state(raw)


def write_state(path: str | Path, state: ProbeState) -> None:
    """Write only the validated state fields, replacing the file atomically."""

    state_path = validate_state_path(path)
    if state_path.exists():
        raise ProbeError(f"refusing to overwrite existing probe state: {state_path}")
    payload = json.dumps(state.as_dict(), sort_keys=True, indent=2) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f".{state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_path)
        temporary = None
    except OSError as error:
        raise ProbeError(f"unable to write probe state: {state_path}") from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def build_session_factory() -> tuple[Any, sessionmaker[Session]]:
    """Create the observer's database pool from the container runtime URL."""

    database_url = os.getenv("ODP_DATABASE_URL")
    if not database_url:
        raise ProbeError("ODP_DATABASE_URL is required for the recovery probe")
    try:
        engine = create_engine(database_url)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
    except Exception as error:
        raise ProbeError("unable to open the disposable probe database") from error
    return engine, sessions


def observe_database(
    sessions: sessionmaker[Session], state: ProbeState
) -> DurableObservation:
    """Read a session and its result chain using a fresh SQLAlchemy session."""

    with sessions() as database:
        source = database.scalar(
            select(InspectionSessionRow).where(
                InspectionSessionRow.organization_id == state.organization_id,
                InspectionSessionRow.session_id == state.session_id,
            )
        )
        if source is None:
            raise ProbeError(f"session is not durable yet: {state.session_id}")

        latest: tuple[
            PublishedInferenceResultRow, InferenceTaskRow, FrameArtifactRow
        ] | None = None
        rows = database.execute(
            select(PublishedInferenceResultRow, InferenceTaskRow, FrameArtifactRow)
            .join(
                InferenceTaskRow,
                InferenceTaskRow.task_id == PublishedInferenceResultRow.task_id,
            )
            .join(
                FrameArtifactRow,
                FrameArtifactRow.artifact_id == PublishedInferenceResultRow.artifact_id,
            )
            .where(
                PublishedInferenceResultRow.organization_id == state.organization_id,
                InferenceTaskRow.organization_id == state.organization_id,
                FrameArtifactRow.organization_id == state.organization_id,
                FrameArtifactRow.stream_session_id == state.session_id,
                FrameArtifactRow.camera_id == source.camera_id,
            )
            .order_by(
                FrameArtifactRow.frame_sequence.desc(),
                PublishedInferenceResultRow.published_at.desc(),
            )
        ).all()
        committed_rows = []
        for result, task, artifact in rows:
            if (
                task.artifact_id == artifact.artifact_id
                and result.artifact_id == artifact.artifact_id
                and artifact.state == "AVAILABLE"
                and artifact.lifecycle == "EVIDENCE"
                and task.status == "SUCCEEDED"
            ):
                committed_rows.append((result, task, artifact))
        if committed_rows:
            latest = committed_rows[0]
        highest_sequence = max(
            (artifact.frame_sequence for _result, _task, artifact in committed_rows),
            default=0,
        )

        result, task, artifact = (latest or (None, None, None))
        return DurableObservation(
            organization_id=source.organization_id,
            session_id=source.session_id,
            status=source.status,
            sanitized_uri=source.sanitized_uri,
            generation=int(source.ingestion_generation or 0),
            artifact_generation=(
                int(artifact.ingestion_generation)
                if artifact is not None
                else None
            ),
            highest_committed_sequence=highest_sequence,
            result_frame_sequence=(artifact.frame_sequence if artifact is not None else None),
            artifact_id=(artifact.artifact_id if artifact is not None else None),
            task_id=(task.task_id if task is not None else None),
            result_id=(result.result_id if result is not None else None),
            task_status=(task.status if task is not None else None),
            artifact_state=(artifact.state if artifact is not None else None),
            artifact_lifecycle=(artifact.lifecycle if artifact is not None else None),
        )


def is_recovery_advanced(state: ProbeState, observation: DurableObservation) -> bool:
    """Return true only for a new fenced generation and a new committed frame."""

    return (
        observation.is_committed
        and observation.generation > state.first_generation
        and observation.artifact_generation == observation.generation
        and observation.highest_committed_sequence > state.highest_committed_sequence
    )


def assert_prepare_observation(observation: DurableObservation) -> None:
    """Require durable artifact/task/result linkage before writing state."""

    if not observation.is_committed:
        raise ProbeError(
            "first result is not durably linked to a succeeded task and evidence artifact: "
            f"{json.dumps(observation.as_dict(), sort_keys=True)}"
        )
    if observation.generation < 1 or observation.highest_committed_sequence < 1:
        raise ProbeError(
            "first durable result has invalid generation/sequence: "
            f"{json.dumps(observation.as_dict(), sort_keys=True)}"
        )
    if observation.artifact_generation != observation.generation:
        raise ProbeError(
            "first durable result artifact generation does not match session generation: "
            f"{json.dumps(observation.as_dict(), sort_keys=True)}"
        )


def assert_recovery_advanced(
    state: ProbeState, observation: DurableObservation
) -> None:
    """Raise when recovery did not publish both required monotonic facts."""

    if not observation.is_committed:
        raise ProbeError(
            "recovery has no durably linked result: "
            f"{json.dumps(observation.as_dict(), sort_keys=True)}"
        )
    if not is_recovery_advanced(state, observation):
        raise ProbeError(
            "recovery generation and sequence did not advance: "
            f"{json.dumps(observation.as_dict(), sort_keys=True)}"
        )


def _poll_seconds() -> float:
    raw = os.getenv("ODP_PROBE_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))
    try:
        value = float(raw)
    except ValueError as error:
        raise ProbeError("ODP_PROBE_POLL_SECONDS must be a positive number") from error
    if not math.isfinite(value) or value <= 0:
        raise ProbeError("ODP_PROBE_POLL_SECONDS must be a positive number")
    return value


def _timeout(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as error:
        raise ProbeError(f"{name} must be a positive number") from error
    if not math.isfinite(value) or value <= 0:
        raise ProbeError(f"{name} must be a positive number")
    return value


def wait_for_observation(
    sessions: sessionmaker[Session],
    state: ProbeState,
    predicate: Callable[[DurableObservation], bool],
    *,
    timeout_seconds: float,
    description: str,
) -> DurableObservation:
    """Poll durable facts with a bound and retain the last safe observation."""

    deadline = time.monotonic() + timeout_seconds
    poll = _poll_seconds()
    last: dict[str, object] | None = None
    while True:
        try:
            observation = observe_database(sessions, state)
            last = observation.as_dict()
        except ProbeError as error:
            last = {"error": str(error)}
        else:
            if predicate(observation):
                return observation
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll, remaining))
    raise ProbeError(
        f"{description} timed out after {timeout_seconds:.1f}s; "
        f"last_observed={json.dumps(last, sort_keys=True)}"
    )


def _api_url() -> str:
    return os.getenv("ODP_PROBE_API_URL", DEFAULT_API_URL).rstrip("/")


def _api_json(
    method: str,
    path: str,
    *,
    payload: Mapping[str, object] | None = None,
    token: str | None = None,
) -> dict[str, object]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{_api_url()}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ProbeError(f"probe API request {method} {path} failed with HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProbeError(f"probe API request {method} {path} failed") from error
    if not isinstance(value, dict):
        raise ProbeError(f"probe API request {method} {path} returned an invalid object")
    return value


def _login() -> tuple[str, UUID, UUID]:
    email = os.getenv("ODP_PROBE_EMAIL", "leader@example.test")
    password = os.getenv("ODP_PROBE_PASSWORD", "odp-leader-dev")
    login = _api_json(
        "POST", "/api/v1/auth/login", payload={"email": email, "password": password}
    )
    token = login.get("access_token")
    if not isinstance(token, str) or not token:
        raise ProbeError("probe API login did not return an access token")
    profile = _api_json("GET", "/api/v1/auth/me", token=token)
    try:
        organization_id = UUID(str(profile["organization_id"]))
        line_ids = profile["line_ids"]
        line_id = UUID(str(line_ids[0]))
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ProbeError("probe API profile did not return a usable organization and line") from error
    return token, organization_id, line_id


def _make_video(path: Path) -> None:
    try:
        import cv2
        import numpy as np
    except ImportError as error:  # pragma: no cover - runtime container dependency
        raise ProbeError("OpenCV and NumPy are required inside the ingestor container") from error
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 5, (64, 64))
    if not writer.isOpened():
        raise ProbeError(f"unable to create persistent recovery video: {path}")
    try:
        for offset in (0, 1):
            frame = np.zeros((64, 64, 3), dtype=np.uint8)
            cv2.line(frame, (5 + offset, 5), (55, 55 - offset), (255, 255, 255), 2)
            writer.write(frame)
    finally:
        writer.release()


def prepare(state_path: Path) -> ProbeState:
    """Create one API-authorized session and record its first durable result."""

    if state_path.exists():
        raise ProbeError(f"refusing to overwrite existing probe state: {state_path}")
    workspace = _workspace_path()
    video = workspace / f".ingestor-recovery-{uuid4().hex}.avi"
    _make_video(video)
    token, organization_id, line_id = _login()
    idempotency_key = f"ingestor-recovery-{uuid4().hex}"
    response = _create_session(
        token,
        idempotency_key=idempotency_key,
        camera_id=uuid4(),
        line_id=line_id,
        video=video,
    )
    try:
        session_id = UUID(str(response["session_id"]))
        response_org = UUID(str(response["organization_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ProbeError("probe API did not return a usable inspection session") from error
    if response_org != organization_id:
        raise ProbeError("probe API returned a session in a different organization")
    state = ProbeState(
        organization_id=organization_id,
        session_id=session_id,
        first_generation=0,
        highest_committed_sequence=0,
    )
    engine, sessions = build_session_factory()
    try:
        first = wait_for_observation(
            sessions,
            state,
            lambda observation: observation.is_committed,
            timeout_seconds=_timeout("ODP_PROBE_PREPARE_TIMEOUT_SECONDS", 120),
            description="first committed inference result",
        )
        assert_prepare_observation(first)
        state = ProbeState(
            organization_id=organization_id,
            session_id=session_id,
            first_generation=first.generation,
            highest_committed_sequence=first.highest_committed_sequence,
        )
        write_state(state_path, state)
    finally:
        engine.dispose()
    print(json.dumps({"phase": "prepare", **state.as_dict()}, sort_keys=True), flush=True)
    return state


def _create_session(
    token: str,
    *,
    idempotency_key: str,
    camera_id: UUID,
    line_id: UUID,
    video: Path,
) -> dict[str, object]:
    payload = {
        "camera_id": str(camera_id),
        "line_id": str(line_id),
        "source_type": "RECORDED",
        "source_ref": str(video),
        "product_category": os.getenv("ODP_PROBE_PRODUCT_CATEGORY", "外壳注塑件"),
    }
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{_api_url()}/api/v1/inspection-sessions",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ProbeError(f"probe API session creation failed with HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ProbeError("probe API session creation failed") from error
    if not isinstance(value, dict):
        raise ProbeError("probe API session creation returned an invalid object")
    return value


def _request_stop(token: str, state: ProbeState) -> None:
    _api_json(
        "POST",
        f"/api/v1/inspection-sessions/{state.session_id}:stop",
        token=token,
    )


def verify(state_path: Path) -> DurableObservation:
    """Observe takeover, then stop and hold the stopped state past one lease."""

    state = load_state(state_path)
    engine, sessions = build_session_factory()
    try:
        recovered = wait_for_observation(
            sessions,
            state,
            lambda observation: is_recovery_advanced(state, observation),
            timeout_seconds=_timeout("ODP_PROBE_RECOVERY_TIMEOUT_SECONDS", 120),
            description="expired-session takeover and new committed result",
        )
        assert_recovery_advanced(state, recovered)
        token, organization_id, _line_id = _login()
        if organization_id != state.organization_id:
            raise ProbeError("probe API stop actor is in a different organization")
        _request_stop(token, state)
        stopped = wait_for_observation(
            sessions,
            state,
            lambda observation: observation.status == "STOPPED",
            timeout_seconds=_timeout("ODP_PROBE_STOP_TIMEOUT_SECONDS", 90),
            description="session stop acknowledgement",
        )
        lease_seconds = _timeout("ODP_INGESTION_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
        deadline = time.monotonic() + lease_seconds
        last = stopped
        while time.monotonic() < deadline:
            last = observe_database(sessions, state)
            if last.status != "STOPPED":
                raise ProbeError(
                    "session reopened after stop request: "
                    f"{json.dumps(last.as_dict(), sort_keys=True)}"
                )
            time.sleep(min(_poll_seconds(), max(0.0, deadline - time.monotonic())))
        last = observe_database(sessions, state)
        if last.status != "STOPPED":
            raise ProbeError(
                "session was not STOPPED beyond one lease window: "
                f"{json.dumps(last.as_dict(), sort_keys=True)}"
            )
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "phase": "verify",
                "organization_id": str(state.organization_id),
                "session_id": str(state.session_id),
                "recovered_generation": recovered.generation,
                "recovered_sequence": recovered.highest_committed_sequence,
                "stopped": True,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return recovered


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "verify"))
    parser.add_argument("--state", required=True, help="absolute JSON state path in /workspace")
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    """Run one probe phase and return a shell-friendly status code."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        # This must precede state reads and all external calls.
        require_disposable_opt_in()
        state_path = validate_state_path(args.state, must_exist=args.phase == "verify")
        if args.phase == "prepare":
            prepare(state_path)
        else:
            verify(state_path)
    except ProbeError as error:
        print(f"ingestor recovery probe failed: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by Compose
    raise SystemExit(cli())


__all__ = [
    "DurableObservation",
    "ProbeError",
    "ProbeState",
    "assert_prepare_observation",
    "assert_recovery_advanced",
    "build_session_factory",
    "cli",
    "is_recovery_advanced",
    "load_state",
    "observe_database",
    "parse_state",
    "prepare",
    "require_disposable_opt_in",
    "validate_state_path",
    "verify",
    "wait_for_observation",
    "write_state",
]
