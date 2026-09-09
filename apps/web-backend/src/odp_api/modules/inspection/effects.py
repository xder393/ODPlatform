from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PublishedEffect:
    result_id: UUID
    event_id: UUID | None = None
    case_id: UUID | None = None
    alert_outbox_id: UUID | None = None


class PublishConflict(RuntimeError):
    pass


class InspectionEffectService:
    def __init__(self, adapter):
        self._adapter = adapter

    def publish(self, command):
        return self._adapter.publish(command)
