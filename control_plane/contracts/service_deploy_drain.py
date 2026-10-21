"""Public-safe references correlating a deploy alert with private runtime detail."""

import hashlib
import re


OPERATION_REFERENCE_PATTERN = re.compile(r"[a-f0-9]{64}")


def operation_reference(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
