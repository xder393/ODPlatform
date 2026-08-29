import sys
from pathlib import Path

WEB_BACKEND_SRC = Path(__file__).parents[3] / "src"
sys.path[:0] = [str(WEB_BACKEND_SRC)]

from odp_api.modules.tasks.admission import AdmissionPolicy, FrameSampler


def test_sampler_selects_two_frames_per_second_from_ten_fps():
    sampler = FrameSampler(target_fps=2)

    selected = [index for index in range(10) if sampler.accept(index / 10)]

    assert selected == [0, 5]


def test_admission_rejects_before_upload_when_worker_is_unhealthy():
    decision = AdmissionPolicy(frame_ttl_seconds=2).evaluate(
        ready_count=0,
        oldest_ready_age_seconds=0,
        worker_healthy=False,
        redis_available=True,
        frame_age_seconds=0.1,
    )

    assert decision.reason == "WORKER_UNHEALTHY"


def test_admission_rejects_when_frame_ttl_is_exhausted():
    decision = AdmissionPolicy(frame_ttl_seconds=2).evaluate(
        ready_count=0,
        oldest_ready_age_seconds=0,
        worker_healthy=True,
        redis_available=True,
        frame_age_seconds=2,
    )
    assert decision.reason == "FRAME_TTL_EXHAUSTED"


def test_admission_rejects_when_redis_is_unavailable():
    decision = AdmissionPolicy(frame_ttl_seconds=2).evaluate(
        ready_count=0,
        oldest_ready_age_seconds=0,
        worker_healthy=True,
        redis_available=False,
        frame_age_seconds=0.1,
    )
    assert decision.reason == "REDIS_UNAVAILABLE"


def test_admission_rejects_when_oldest_ready_is_stale():
    decision = AdmissionPolicy(frame_ttl_seconds=2).evaluate(
        ready_count=1,
        oldest_ready_age_seconds=2,
        worker_healthy=True,
        redis_available=True,
        frame_age_seconds=0.1,
    )
    assert decision.reason == "READY_STALE"
