from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, StrictBool, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.testing_publication import TestingServingSnapshot
from control_plane.dokploy.testing_route import TestingLaneRoute, amend_testing_route_address
from control_plane.testing_generation_retirement import RetainedTestingHostnameReservation


ADDRESS_SCOPE = "launchplane:testing-address:v1"
ADDRESS_ROUTE = "service:testing-address"


class TestingAddressPlan(FrozenRecord):
    product: str
    context: str
    retirement_operation_id: str
    reason: str = Field(min_length=1)
    publication_key: str
    publication_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile: LaunchplaneProductProfileRecord
    before: TestingServingSnapshot
    route_before: TestingLaneRoute
    route_after: TestingLaneRoute
    binding_after: EnvironmentRouteBindingRecord
    reservations: tuple[RetainedTestingHostnameReservation, ...] = Field(min_length=1)
    server_id: str
    provider_project: str
    retained_projects: tuple[str, ...] = Field(min_length=1)
    retained_automation: dict[str, StrictBool] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    recipient_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    domain_inventory_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_sha256: dict[str, str]
    retirement_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        before, after = self.route_before, self.route_after
        lane = next(
            (
                item
                for item in self.profile.lanes
                if (item.context, item.instance) == (self.context, "testing")
            ),
            None,
        )
        if (
            self.profile.product != self.product
            or not self.profile.is_active
            or self.profile.driver_id != "odoo"
            or lane is None
            or lane.base_url.rstrip("/") != "https://" + before.host
            or urlsplit(before.new_backend).port != self.profile.runtime_port
        ):
            raise ValueError(
                "Canonical address authority requires its original exclusive native testing profile."
            )
        expected_operations = {
            self.retirement_operation_id,
            *(item.operation_id for item in self.reservations),
        }
        if set(self.operation_sha256) != expected_operations or any(
            item.target_id == self.before.target_id.target_id for item in self.reservations
        ):
            raise ValueError(
                "Canonical address custody must retain every distinct original operation and recipient."
            )
        if self.retained_automation and set(self.retained_automation) != {
            item.target_id for item in self.reservations
        }:
            raise ValueError(
                "Automatic-deploy preparation requires every exact retained recipient."
            )
        if before.aliases or after.aliases != (before.host,) or after.host == before.host:
            raise ValueError("A canonical address change retains its single original hostname.")
        if (before.product, before.context) != (self.product, self.context):
            raise ValueError("The canonical address belongs to its original testing lane.")
        if after != amend_testing_route_address(before, after.host):
            raise ValueError("A canonical address change cannot alter the backend or ingress.")
        if any(
            (item.product, item.context, item.hostname) != (self.product, self.context, after.host)
            or not item.target_id
            for item in self.reservations
        ):
            raise ValueError("The address change needs exact original hostname custody.")
        binding = self.before.binding
        if (
            binding is None
            or binding.source.source_versions.get("testing_publication") != self.publication_key
            or self.binding_after != address_binding(binding, after)
        ):
            raise ValueError("The address change must preserve its testing binding authority.")
        return self


class TestingAddressRecord(FrozenRecord):
    plan: TestingAddressPlan
    state: Literal["planned", "applied"] = "planned"
    applied_at: datetime | None = None
    public_health: tuple[HealthcheckEvidence, ...] = ()


def address_binding(
    before: EnvironmentRouteBindingRecord, after: TestingLaneRoute
) -> EnvironmentRouteBindingRecord:
    from control_plane.contracts.route_binding_record import RouteBindingDomain

    return before.model_copy(
        update={
            "domains": (RouteBindingDomain(domain_name=after.host),),
            "source": before.source.model_copy(
                update={
                    "source_versions": {
                        **{
                            key: value
                            for key, value in before.source.source_versions.items()
                            if key != "testing_route_observation"
                        },
                        "testing_route": after.digest,
                        "testing_address": before.source.source_versions["testing_publication"],
                    }
                }
            ),
        }
    )
