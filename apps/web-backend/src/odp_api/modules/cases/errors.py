from odp_api.modules.inspection.models import CaseStatus


class InvalidCaseTransition(ValueError):
    """Raised when a requested defect-case state change is not permitted."""

    def __init__(self, from_status: CaseStatus, to_status: CaseStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Cannot transition a case from {from_status} to {to_status}.")
