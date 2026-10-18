"""Caller actions for refused application storage custody operations."""


class StateVolumeRefusal(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
