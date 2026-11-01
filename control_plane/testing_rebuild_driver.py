from dataclasses import dataclass
from typing import Protocol

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.contracts.testing_application import TestingApplicationState


class TestingRebuildRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TestingRebuildProfileStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


@dataclass(frozen=True)
class TestingRebuildDriver:
    profile: LaunchplaneProductProfileRecord
    lane: ProductLaneProfile
    state: TestingApplicationState | None = None

    @property
    def execution_available(self) -> bool:
        return self.profile.driver_id == "odoo"


def testing_rebuild_driver(
    profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> TestingRebuildDriver:
    if not profile.is_active or lane.instance != "testing" or lane not in profile.lanes:
        raise TestingRebuildRefused(
            "testing_rebuild_inputs_unverified", "Rebuild requires an active testing lane."
        )
    if profile.driver_id == "odoo":
        return TestingRebuildDriver(profile, lane)
    if profile.driver_id != "generic-web":
        raise TestingRebuildRefused(
            "testing_rebuild_driver_unsupported", "This driver does not support testing rebuilds."
        )
    state = lane.generic_web_extensions.testing_state
    if state is not None and state.kind == "postgresql":
        raise TestingRebuildRefused(
            "testing_rebuild_postgresql_deferred",
            "Independent PostgreSQL testing rebuilds are deferred until a product needs them.",
        )
    if state is None or state.kind == "unknown":
        raise TestingRebuildRefused(
            "testing_rebuild_state_unverified",
            "Declare whether this testing lane has no persistent state or one local volume.",
        )
    return TestingRebuildDriver(
        profile, lane, TestingApplicationState.model_validate(state.model_dump())
    )


def resolve_testing_rebuild_driver(
    store: TestingRebuildProfileStore, product: str
) -> TestingRebuildDriver:
    profile = store.read_product_profile_record(product)
    lanes = [lane for lane in profile.lanes if lane.instance == "testing"]
    if len(lanes) != 1 or not is_exclusive_product_context(
        context=lanes[0].context,
        product=product,
        owners=product_context_owner_map(store.list_product_profile_records()),
    ):
        raise TestingRebuildRefused(
            "testing_rebuild_inputs_unverified", "Rebuild requires one exclusive testing context."
        )
    return testing_rebuild_driver(profile, lanes[0])
