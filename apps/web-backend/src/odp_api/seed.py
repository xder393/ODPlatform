"""Deterministic demo seed for the quality inspection platform.

``python -m odp_api.seed`` prints the demo accounts and a summary of the
seeded artifacts. ``build_demo_seed()`` is a pure function: fixed UUIDs and
fixed content make repeated runs idempotent, which keeps the Compose demo and
the offline E2E suite reproducible.

The knowledge documents use English PDF content on purpose: the retrieval
query is ``"{defect_class} inspection guidance"`` and both scoring paths
(hash embedding + BM25) tokenize on word boundaries, so matching terms must
share the Latin token space. Source names and UI labels remain Chinese.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from io import BytesIO
from typing import Literal
from uuid import UUID, uuid5

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from odp_api.adapters.vision.mock import MockVisionAdapter
from odp_api.modules.identity.models import Actor, Role
from odp_api.modules.inspection.models import DefectCase
from odp_api.modules.inspection.service import InspectionService
from odp_api.ports.vision import FrameInput

# Fixed namespace so every derived UUID is stable across runs.
SEED_NAMESPACE = UUID("0d750000-0000-4000-8000-000000000001")

DEMO_ORG_ID = UUID("10000000-0000-4000-8000-000000000001")
DEMO_LINE_ID = UUID("20000000-0000-4000-8000-000000000001")
DEMO_CAMERA_IDS = (
    UUID("30000000-0000-4000-8000-000000000001"),
    UUID("30000000-0000-4000-8000-000000000002"),
    UUID("30000000-0000-4000-8000-000000000003"),
)
PRODUCT_CATEGORY = "外壳注塑件"
MODEL_RELEASE = "mock-yolo-1.0"

# Development-only accounts; passwords are documented for the local demo and
# must never be reused outside it.
DEMO_ACCOUNTS = (
    ("inspector@example.test", "odp-inspector-dev", Role.INSPECTOR),
    ("leader@example.test", "odp-leader-dev", Role.SUPERVISOR),
    ("admin@example.test", "odp-admin-dev", Role.ADMINISTRATOR),
)

SEED_CASE_COUNT = 10


@dataclass(frozen=True, slots=True)
class SeedDocument:
    """One knowledge document to ingest into the configured retrieval backend."""

    source_name: str
    filename: str
    content: bytes
    evidence_kind: Literal["CURRENT_SPECIFICATION", "HISTORICAL_CASE"]


@dataclass(frozen=True, slots=True)
class DemoSeed:
    """All deterministic demo artifacts, ready for app composition."""

    actors: tuple[Actor, ...]
    passwords: dict[UUID, str]
    cases: tuple[DefectCase, ...]
    documents: tuple[SeedDocument, ...]
    model_release: str


def build_demo_seed() -> DemoSeed:
    """Build the complete deterministic demo dataset (no IO side effects)."""
    actors = tuple(
        Actor(
            actor_id=uuid5(SEED_NAMESPACE, f"demo-actor-{email}"),
            organization_id=DEMO_ORG_ID,
            role=role,
            line_ids=frozenset({DEMO_LINE_ID}),
            email=email,
        )
        for email, _password, role in DEMO_ACCOUNTS
    )
    passwords = {
        actor.actor_id: password
        for actor, (_email, password, _role) in zip(actors, DEMO_ACCOUNTS)
    }

    inspection_service = InspectionService(MockVisionAdapter())
    cases = []
    for index in range(SEED_CASE_COUNT):
        camera_id = DEMO_CAMERA_IDS[index % len(DEMO_CAMERA_IDS)]
        frame = FrameInput(fixture_name=f"seed-frame-{index + 1:02d}", content=f"seed-frame-{index + 1:02d}".encode())
        base_case = inspection_service.inspect_fixture(
            frame,
            organization_id=DEMO_ORG_ID,
            camera_id=camera_id,
            event_id=uuid5(SEED_NAMESPACE, f"demo-event-{index + 1:02d}"),
        )
        # ``inspect_fixture`` leaves case/event scope unset; the seeded cases
        # must carry it, otherwise evidence tiering degrades to UNAVAILABLE.
        scoped_events = tuple(
            replace(event, line_id=DEMO_LINE_ID) for event in base_case.inspection_events
        )
        cases.append(
            replace(
                base_case,
                case_id=uuid5(SEED_NAMESPACE, f"demo-case-{index + 1:02d}"),
                line_id=DEMO_LINE_ID,
                product_category=PRODUCT_CATEGORY,
                inspection_events=scoped_events,
            )
        )

    documents = (
        SeedDocument(
            source_name="划痕检验规程",
            filename="scratch-inspection-specification.pdf",
            content=_pdf_bytes(
                (
                    "scratch inspection specification: stop the line and re-inspect "
                    "immediately when scratch defect confidence exceeds 0.9.",
                    "inspection follows the shell injection part standard; record the "
                    "model release and threshold used for the decision.",
                    "simulated line pause requires reauthentication completed within "
                    "five minutes.",
                )
            ),
            evidence_kind="CURRENT_SPECIFICATION",
        ),
        SeedDocument(
            source_name="划痕历史工单汇编",
            filename="scratch-historical-cases.pdf",
            content=_pdf_bytes(
                (
                    "scratch historical case 2026-03: a shell injection part surface "
                    "scratch at confidence 0.95 was confirmed as a false positive.",
                    "scratch historical case 2026-05: a scratch on the same line was "
                    "confirmed as a real defect; the threshold was raised afterwards.",
                )
            ),
            evidence_kind="HISTORICAL_CASE",
        ),
    )

    return DemoSeed(
        actors=actors,
        passwords=passwords,
        cases=tuple(cases),
        documents=documents,
        model_release=MODEL_RELEASE,
    )


def _pdf_bytes(paragraphs: tuple[str, ...]) -> bytes:
    """Build a minimal one-paragraph-per-page PDF with extractable text."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for paragraph in paragraphs:
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({paragraph}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _seed_summary() -> str:
    seed = build_demo_seed()
    lines = [
        "ODPlatform demo seed (development-only)",
        "=" * 46,
        f"organization:      {DEMO_ORG_ID}",
        f"production line:   {DEMO_LINE_ID}",
        f"product category:  {PRODUCT_CATEGORY}",
        f"cameras:           3",
        f"defect cases:      {len(seed.cases)} (scratch, {MODEL_RELEASE})",
        f"knowledge docs:    {len(seed.documents)}",
        "accounts:",
    ]
    for email, password, role in DEMO_ACCOUNTS:
        lines.append(f"  {email:24} {password:18} {role.value}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(_seed_summary())
