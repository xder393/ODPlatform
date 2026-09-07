"""HTTP schemas for database-owned camera inspection sessions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class InspectionSessionCreate(BaseModel):
    camera_id: UUID
    line_id: UUID
    source_type: Literal["RECORDED", "RTSP", "LOCAL_CAMERA"]
    source_ref: str = Field(min_length=1, max_length=2048)
    secret_ref: str | None = Field(default=None, max_length=255)


class InspectionSessionResponse(BaseModel):
    session_id: UUID
    organization_id: UUID
    camera_id: UUID
    line_id: UUID
    source_type: str
    sanitized_uri: str
    secret_reference: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class InspectionSessionListResponse(BaseModel):
    items: list[InspectionSessionResponse]


__all__ = [
    "InspectionSessionCreate",
    "InspectionSessionListResponse",
    "InspectionSessionResponse",
]
