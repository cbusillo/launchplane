from typing import Annotated, Any

from pydantic import Field

SNAPSHOT_PROTOCOL_KEY = "x-launchplane-snapshot-protocol"
LANE_KINDS = ("testing", "prod")


def snapshot_protocol(*values: str) -> dict[str, Any]:
    return {SNAPSHOT_PROTOCOL_KEY: list(values)}


def snapshot_protocol_default(value: str) -> Any:
    return Field(default=value, json_schema_extra=snapshot_protocol(value))


LaneInstance = Annotated[str, Field(json_schema_extra=snapshot_protocol(*LANE_KINDS))]
