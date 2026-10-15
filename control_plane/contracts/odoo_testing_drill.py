"""Value-free lane evidence for the service's disposable rebuild drill."""

from typing import Literal
from pydantic import BaseModel, ConfigDict


class OdooTestingDrillEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["queued", "pass", "fail", "skipped"]
    observed_at: str
    operation_id: str = ""
    deployment_record_id: str = ""
    step: str = ""
    trace: str = ""
    alert_delivered: bool = False
