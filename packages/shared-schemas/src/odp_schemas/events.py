from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class InspectionAlert(BaseModel):
    """A defect alert emitted by an inspection camera."""

    event_id: UUID
    organization_id: UUID
    camera_id: UUID
    occurred_at: datetime
    defect_class: str
    confidence: float = Field(ge=0, le=1)
