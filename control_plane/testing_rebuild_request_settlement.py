"""Durable exclusion of one original request, without manufacturing an operation."""

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)


SETTLEMENT_ROUTE = "native-testing-rebuild-request-exclusion"


class TestingRebuildRequestConflict(ValueError):
    pass


class TestingRebuildRequestExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product: str = Field(min_length=1)
    original_scope: str = Field(min_length=1)
    original_key: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    excluded_at: str = Field(min_length=1)

    def receipt(self, trace_id: str) -> LaunchplaneIdempotencyRecord:
        return LaunchplaneIdempotencyRecord(
            record_id=build_launchplane_mutation_reservation_id(
                scope=self.original_scope,
                route_path=SETTLEMENT_ROUTE,
                idempotency_key=self.original_key,
            ),
            scope=self.original_scope,
            route_path=SETTLEMENT_ROUTE,
            idempotency_key=self.original_key,
            request_fingerprint=self.request_fingerprint,
            response_status_code=200,
            response_trace_id=trace_id,
            recorded_at=self.excluded_at,
            response_payload=self.model_dump(mode="json"),
        )
