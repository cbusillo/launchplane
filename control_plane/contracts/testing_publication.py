"""Exact serving preimages and successor metadata for one testing rebuild."""

from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.dokploy.testing_route import TestingLaneRoute
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration, generation_binding
from control_plane.storage.product_authority_bundle import (
    RuntimeEnvironmentSetExpectation,
    SecretDeliverySetExpectation,
)


class TestingServingSnapshot(FrozenRecord):
    target: DokployTargetRecord
    target_id: DokployTargetIdRecord
    provider: ProviderTargetRecord
    runtime: RuntimeEnvironmentRecord | None = Field(default=None, repr=False)
    overrides: OdooInstanceOverrideRecord | None = None
    inventory: EnvironmentInventory | None = None
    generation: FreshOdooTestingGeneration | None = None
    binding: EnvironmentRouteBindingRecord | None = None


class TestingPublicationPlan(FrozenRecord):
    mutation_record_id: str = Field(min_length=1)
    provider_target_key: str = Field(min_length=1)
    profile: LaunchplaneProductProfileRecord
    candidate: TestingCandidateAllocation
    before: TestingServingSnapshot
    after: TestingServingSnapshot
    deployment: DeploymentRecord
    route: TestingLaneRoute
    runtime_read_set: RuntimeEnvironmentSetExpectation
    secret_read_set: SecretDeliverySetExpectation
    created_at: datetime

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))

    @property
    def key(self) -> str:
        return canonical_json_sha256(
            {
                "product": self.candidate.product,
                "context": self.candidate.context,
                "operation": self.candidate.operation_id,
            }
        )

    @model_validator(mode="after")
    def validate_serving(self) -> Self:
        candidate = self.candidate
        app = candidate.application
        if app is None or app.phase != "prepared" or app.prepared is None:
            raise ValueError("Serving publication requires the original private readiness receipt.")
        plan = app.plan
        expected_witness = (
            generation_binding(plan.generation) if plan.generation else "empty:" + plan.digest
        )
        if app.prepared.source_witness != expected_witness:
            raise ValueError(
                "Publication requires the original sanitized copy or empty bootstrap witness."
            )
        lane = next(
            (
                lane
                for lane in self.profile.lanes
                if (lane.context, lane.instance) == (candidate.context, "testing")
            ),
            None,
        )
        if (
            self.profile.product != candidate.product
            or self.profile.driver_id != "odoo"
            or self.profile.lifecycle_state != "active"
            or lane is None
            or lane.base_url.rstrip("/") != plan.base_url
            or (self.route.product, self.route.context, self.route.host)
            != (candidate.product, candidate.context, urlsplit(plan.base_url).hostname)
            or self.route.new_backend != app.prepared.private_endpoint
        ):
            raise ValueError("Publication must preserve its owned testing address and backend.")
        for snapshot in (self.before, self.after):
            for item in (
                snapshot.target,
                snapshot.target_id,
                snapshot.provider,
                snapshot.runtime,
                snapshot.overrides,
                snapshot.inventory,
                snapshot.generation,
                snapshot.binding,
            ):
                if item is not None and (item.context, item.instance) != (
                    candidate.context,
                    "testing",
                ):
                    raise ValueError("Publication metadata cannot affect another lane.")
            if snapshot.target.target_type != "compose" or (
                snapshot.provider.provider_id,
                snapshot.provider.target_category,
                snapshot.provider.target_id,
            ) != ("dokploy", "compose", snapshot.target_id.target_id):
                raise ValueError("Serving target representations must agree.")
            if snapshot.runtime is not None and snapshot.runtime.scope != "instance":
                raise ValueError("Publication can only write its testing instance layer.")
        after = self.after
        if self.before.target_id.target_id != candidate.serving_target_id or (
            after.target_id.target_id,
            after.target.target_name,
            after.target.project_name,
        ) != (candidate.compose_id, candidate.namespace, candidate.namespace):
            raise ValueError("Publication must adopt its independent prepared target.")
        if (
            after.target.env
            or after.target.domains
            or after.target.public_hosts
            or after.target.pending_public_host_effects
            or after.target.policies.staff_testing_hold
        ):
            raise ValueError(
                "The candidate cannot acquire generation-owned routing or inline secrets."
            )
        if after.binding is None or (
            after.binding.product != candidate.product
            or after.binding.provider_target.target_name != candidate.namespace
            or after.binding.source.source_kind != "service"
            or after.binding.source.source_versions.get("testing_publication") != self.key
            or after.binding.source.source_versions.get("testing_route") != self.route.digest
            or tuple(d.domain_name for d in after.binding.domains) != (self.route.host,)
        ):
            raise ValueError("Serving routing needs the exact lane-owned publication provenance.")
        if self.before.binding is not None and (
            self.before.binding.domains != after.binding.domains
            or self.before.binding.ingress != after.binding.ingress
            or self.before.binding.tls != after.binding.tls
        ):
            raise ValueError(
                "Publication cannot change the existing address, ingress or TLS owner."
            )
        if (
            self.deployment.record_id != plan.runtime_identity.deployment_record_id
            or self.deployment.runtime_identity != plan.runtime_identity
            or after.inventory is None
            or after.inventory.runtime_identity != plan.runtime_identity
            or after.inventory.deployment_record_id != self.deployment.record_id
            or self.deployment.resolved_target is None
            or self.deployment.resolved_target.target_id != candidate.compose_id
        ):
            raise ValueError("Serving deployment and inventory must bind the prepared build.")
        if (plan.generation is None) != (after.generation is None):
            raise ValueError("An empty rebuild cannot invent a restore generation.")
        if (
            plan.generation is not None
            and after.generation is not None
            and (
                after.generation.model_copy(update={"completed_at": ""}) != plan.generation
                or not after.generation.completed_at
            )
        ):
            raise ValueError("Serving restore generation must retain its exact copy witness.")
        if (
            not self.runtime_read_set.include_global
            or self.runtime_read_set.contexts != (candidate.context,)
            or self.runtime_read_set.instances != ((candidate.context, "testing"),)
            or (
                self.secret_read_set.integration,
                self.secret_read_set.context,
                self.secret_read_set.instance,
            )
            != ("runtime_environment", candidate.context, "testing")
        ):
            raise ValueError("Publication requires all applicable runtime and credential metadata.")
        if self.created_at.utcoffset() is None:
            raise ValueError("Publication requires an aware timestamp.")
        return self


class TestingPublicationRecord(FrozenRecord):
    plan: TestingPublicationPlan
    state: Literal["planned", "applied", "reversed"] = "planned"
    updated_at: datetime
    public_health: HealthcheckEvidence | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if self.updated_at.utcoffset() is None or self.updated_at < self.plan.created_at:
            raise ValueError("Publication receipt time must follow its original plan.")
        if self.state == "applied" and self.public_health is None:
            raise ValueError("Applied publication must retain its public proof.")
        return self
