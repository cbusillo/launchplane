"""Read-only completion evidence for a retained profile mutation key.

Missing reservations are not evidence of non-dispatch: the original request
may still reach the service. Only an atomically committed response settles it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord

ProfileMutationField = Literal["owner", "image-repository", "production-use"]


class ProductProfileMutationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    product: str
    field: ProfileMutationField
    idempotency_key: str
    state: Literal["completed", "unresolved"] = "unresolved"
    original_trace_id: str = ""


def profile_mutation_receipt(
    *,
    record: LaunchplaneIdempotencyRecord | None,
    trace_id: str,
    product: str,
    field: ProfileMutationField,
    idempotency_key: str,
) -> ProductProfileMutationReceipt:
    receipt = ProductProfileMutationReceipt(
        trace_id=trace_id, product=product, field=field, idempotency_key=idempotency_key
    )
    if record is None or record.state != "completed" or record.response_status_code != 202:
        return receipt
    payload = record.response_payload
    records = payload.get("records")
    result = payload.get("result")
    if (
        not isinstance(records, dict)
        or records.get("product_profile") != product
        or not isinstance(result, dict)
        or result.get("applied") is not True
        or payload.get("status") != "accepted"
    ):
        return receipt
    return receipt.model_copy(
        update={"state": "completed", "original_trace_id": record.response_trace_id}
    )
