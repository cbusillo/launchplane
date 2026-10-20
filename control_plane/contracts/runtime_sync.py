"""Value-free browser review for a lane's managed runtime environment."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class BrowserRuntimeSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_digest(self) -> "BrowserRuntimeSyncRequest":
        if self.reviewed_plan_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", self.reviewed_plan_sha256
        ):
            raise ValueError("Runtime sync requires its reviewed plan digest.")
        return self


class RuntimeSyncReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["runtime-sync"] = "runtime-sync"
    product: str
    context: str
    instance: str
    plan_sha256: str
    target_fingerprint: str
    target_id: str
    target_type: Literal["application", "compose"]
    target_name: str
    missing_keys: tuple[str, ...]
    different_keys: tuple[str, ...]
    retired_keys_present: tuple[str, ...]
    changed_keys: tuple[str, ...]
    desired_key_count: int
    live_key_count: int
    unchanged_key_count: int
    applied: bool = False


class RuntimeSyncModeAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = False
    reason: str = "A signed-in administrator is required."


class RuntimeSyncAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: RuntimeSyncModeAvailability
    apply: RuntimeSyncModeAvailability
