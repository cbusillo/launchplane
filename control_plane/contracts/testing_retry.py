"""Recovery metadata lives outside the queue format that older workers consume."""

from pydantic import BaseModel, ConfigDict, Field

TESTING_RETRY_REARM_SCOPE = "launchplane:testing-retry-rearm:v1"
TESTING_RETRY_REARM_ROUTE = "service:testing-retry-rearm"
TESTING_RETRY_ROUTE = "/v1/products/{product}/testing-retry"


def testing_retry_rearm_key(product: str, generation_binding_sha256: str = "") -> str:
    # Older workers keep reading the original product key and never consume a
    # fresh-generation boundary they cannot enforce. The record shape stays readable.
    return (
        f"{product}:generation:{generation_binding_sha256}"
        if generation_binding_sha256
        else product
    )


class TestingRetryRearm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    context: str = Field(min_length=1)
    after_operation_id: str = Field(min_length=1)
    lane_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recorded_at: str = Field(min_length=1)
    recorded_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
