class InvalidCaseTransition(ValueError):
    """Raised when a requested defect-case state change is not permitted."""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Cannot transition a case from {from_status} to {to_status}.")


class InvalidCaseStatus(ValueError):
    """Raised when a defect case is constructed with an unsupported status."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"Unsupported defect case status: {status}.")
