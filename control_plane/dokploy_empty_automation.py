from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import TYPE_CHECKING

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.dokploy.target_source_setup import require_empty_compose_source
from control_plane.dokploy_inventory import InventoryProject, read_dokploy_project_membership
from control_plane.generic_web_testing_exclusion import (
    _target_reservations,
    require_quiescent_testing_provider_work,
)
from control_plane.odoo_first_empty_inputs import (
    FirstEmptyAuthority,
    first_empty_storage_program,
    resolve_first_empty_authority,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore

if TYPE_CHECKING:
    from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

ROUTE = "/v1/dokploy-targets/setup"
OPERATION = "disable-empty-compose-auto-deploy"
REVIEW_SECONDS = 300


class EmptyAutomationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_auto_deploy: StrictBool
    observed_at: AwareDatetime
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _provider_configuration(payload: api.JsonObject) -> api.JsonObject:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"updatedAt", "environment", "server"}
    }


@dataclass(frozen=True)
class EmptyAutomationObservation:
    authority: FirstEmptyAuthority
    provider: api.JsonObject
    storage: str
    placement: InventoryProject


@dataclass
class EmptyAutomationAdapter:
    root: Path
    store: PostgresRecordStore
    request: DokployTargetSetupEnvelope
    product: str
    scope: str
    key: str
    trace: str

    def intent(self) -> dict[str, object]:
        return self.request.model_dump(mode="json", exclude={"mode", "confirmation"})

    def provider_target_origin(self) -> ProviderTargetOrigin:
        return ProviderTargetOrigin(
            product=self.product,
            context=self.request.context,
            instance="prod",
            provider_id="dokploy",
            target_type="compose",
            target_id=self.request.target_id,
        )

    def target_key(self) -> str:
        return self.provider_target_origin().target_key()

    def reconciliation_key(self) -> str:
        return nonvalidating_compact_sorted_json_sha256(self.intent())

    def require_no_predecessor(self) -> None:
        from control_plane.dokploy_target_setup_http import first_production_target_lock_name

        own_id = (
            build_launchplane_mutation_reservation_id(
                scope=self.scope, route_path=ROUTE, idempotency_key=self.key
            )
            if self.key and self.request.mode != "dry-run"
            else ""
        )
        held = _target_reservations(
            self.store,
            product=self.product,
            context=self.request.context,
            target_key=self.target_key(),
            target_id=self.request.target_id,
            require_complete=True,
        )
        if any(row.record_id != own_id for row in held):
            raise ValueError("An original provider operation still owns this target.")
        for row in self.store.list_held_provider_target_reservations():
            if row.record_id == own_id:
                continue
            origin = self.store.read_provider_target_origin(row)
            if row.provider_target_key == first_production_target_lock_name(
                self.request.context
            ) or (
                origin is not None
                and origin.origin.provider_id == "dokploy"
                and origin.origin.target_id == self.request.target_id
            ):
                raise ValueError("An original setup or physical-target operation is unresolved.")
        if self.store.list_odoo_stable_target_replacement_operation_records(
            context_name=self.request.context, instance_name="prod"
        ):
            raise ValueError("An existing production operation requires its original lifecycle.")

    @contextmanager
    def setup_lock(self) -> Iterator[None]:
        from control_plane.dokploy_target_setup_http import first_production_target_lock_name

        with self.store.testing_replacement_execution_lock(
            first_production_target_lock_name(self.request.context)
        ) as check:
            if check is None:
                raise ValueError("An original setup executor still owns this context.")
            check()
            with self.store.odoo_synchronous_lane_reservation(
                product=self.product, context=self.request.context, instance="prod"
            ) as owner:
                if owner is not None:
                    raise ValueError("An Odoo operation still owns the original lane.")
                yield
                check()

    def observation(self) -> EmptyAutomationObservation:
        authority = resolve_first_empty_authority(self.store, self.product, self.request.context)
        if (
            authority.target_id.target_id != self.request.target_id
            or authority.provider.to_deployed_target_reference()
            != self.request.expected_current_provider_target
        ):
            raise ValueError("The original target binding changed.")
        if authority.target.pending_public_host_effects:
            raise ValueError("Original target setup still has unresolved public-host effects.")
        self.require_no_predecessor()
        host, token = source.read_dokploy_config(
            control_plane_root=self.root, database_url=self.store.database_url
        )

        def read_provider() -> api.JsonObject:
            live = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=self.request.target_id
            )
            require_empty_compose_source(live, self.request.target_id)
            project = live.get("appName")
            if (
                not {"env", "composeFile", "mounts", "autoDeploy", "appName"} <= live.keys()
                or live["env"] not in (None, "")
                or live["composeFile"] not in (None, "")
                or live["mounts"] not in (None, [])
                or type(live["autoDeploy"]) is not bool
                or live.get("environmentId") != self.request.environment_id
                or live.get("serverId") != self.request.server_id
                or not isinstance(project, str)
                or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", project) is None
            ):
                raise ValueError(
                    "The original provider observation is occupied, changed or unknown."
                )
            native_copy_stream.require_registered_remote_server(live["serverId"])
            require_quiescent_testing_provider_work(
                host=host, token=token, target_id=self.request.target_id, never_deployed=True
            )
            return _provider_configuration(live)

        before = read_provider()
        placement = self.placement(host, token)
        spec = {
            "target_id": self.request.target_id,
            "project": before["appName"],
            "server": self.request.server_id,
            "volumes": authority.volumes,
        }
        frames: list[dict[str, object]] = []
        native_copy_stream.run_managed_server_program(
            host=host,
            token=token,
            server_id=self.request.server_id,
            program=first_empty_storage_program(),
            payload=spec,
            receive=frames.append,
            timeout_seconds=60,
        )
        if any(frame.get("type") == "denied" for frame in frames):
            raise native_copy_stream.NativeSourceAuthorizationDenied(
                "Original storage read refused."
            )
        storage = frames[0].get("storage") if len(frames) == 1 else None
        if (
            len(frames) != 1
            or set(frames[0]) != {"type", "binding", "storage"}
            or frames[0]["type"] != "complete"
            or frames[0]["binding"] != nonvalidating_compact_sorted_json_sha256(spec)
            or not isinstance(storage, str)
            or re.fullmatch(r"[a-f0-9]{64}", storage) is None
            or self.placement(host, token) != placement
            or read_provider() != before
            or resolve_first_empty_authority(self.store, self.product, self.request.context)
            != authority
        ):
            raise ValueError("Original storage, target or authority is unproved or changed.")
        self.require_no_predecessor()
        return EmptyAutomationObservation(authority, before, storage, placement)

    def placement(self, host: str, token: str) -> InventoryProject:
        matches = [
            (project, environment)
            for project in read_dokploy_project_membership(host=host, token=token)
            for environment in project.environments
            if environment.environment_id == self.request.environment_id
        ]
        if (
            len(matches) != 1
            or matches[0][0].project_id != self.request.project_id
            or [(target.category, target.target_id) for target in matches[0][1].targets]
            != [("compose", self.request.target_id)]
        ):
            raise ValueError("The exact original project and environment are not exclusive.")
        tracked = {
            row.target_id: row.context for row in self.store.list_dokploy_target_id_records()
        }
        if any(
            tracked.get(target.target_id) != self.request.context
            for environment in matches[0][0].environments
            for target in environment.targets
        ):
            raise ValueError("The original project contains foreign or unowned resources.")
        return matches[0][0]

    def review_digest(
        self, observation: EmptyAutomationObservation, observed_at: datetime, enabled: bool
    ) -> str:
        return nonvalidating_compact_sorted_json_sha256(
            {
                "request": self.request.model_dump(
                    mode="json", exclude={"mode", "confirmation", "automation_review"}
                ),
                "authority": observation.authority.fingerprint(),
                "provider": {**observation.provider, "autoDeploy": False},
                "placement": observation.placement.model_dump(mode="json"),
                "storage": observation.storage,
                "observed_at": observed_at.isoformat(),
                "observed_auto_deploy": enabled,
            }
        )

    def result(self, *, applied: bool, review: EmptyAutomationReview) -> dict[str, object]:
        return {
            "mode": "apply" if applied else "dry-run",
            "operation": OPERATION,
            "context": self.request.context,
            "instance": "prod",
            "applied": applied,
            "route_domain_ids": [],
            "setup": {
                "automatic_deployment_disabled": applied or not review.observed_auto_deploy,
                "automation_review": review.model_dump(mode="json"),
            },
        }

    def plan_result(self) -> dict[str, object]:
        with self.setup_lock():
            current = self.observation()
        now = datetime.now(UTC)
        enabled = current.provider["autoDeploy"]
        assert isinstance(enabled, bool)
        review = EmptyAutomationReview(
            observed_auto_deploy=enabled,
            observed_at=now,
            plan_sha256=self.review_digest(current, now, enabled),
        )
        return self.result(applied=False, review=review)

    def reviewed_observation(self, *, new_effect: bool) -> EmptyAutomationObservation:
        review = self.request.automation_review
        if review is None:
            raise ValueError("The original setup correction requires its reviewed observation.")
        if (
            new_effect
            and not 0 <= (datetime.now(UTC) - review.observed_at).total_seconds() < REVIEW_SECONDS
        ):
            raise ValueError("The setup correction review is stale; review current facts again.")
        current = self.observation()
        if (
            self.review_digest(current, review.observed_at, review.observed_auto_deploy)
            != review.plan_sha256
        ):
            raise ValueError("Original setup facts changed after review.")
        if new_effect and current.provider["autoDeploy"] is not review.observed_auto_deploy:
            raise ValueError("Automatic deployment changed after review.")
        return current

    def completed_payload(self) -> dict[str, object]:
        assert self.request.automation_review is not None
        return provider_operation_response_payload(
            trace_id=self.trace,
            records={},
            result=self.result(applied=True, review=self.request.automation_review),
        )

    def observe(
        self, _provider_operation_key: str, _provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        with self.setup_lock():
            current = self.reviewed_observation(new_effect=False)
        if current.provider["autoDeploy"] is not False:
            return ProviderObservation("unknown")
        return ProviderObservation("present", 202, self.completed_payload())

    def apply(
        self, _provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        with self.setup_lock():
            return self.apply_locked(lease)

    def apply_locked(self, lease: ProviderOperationLease) -> ProviderMutationOutcome:
        try:
            current = self.reviewed_observation(new_effect=True)
        except Exception as error:
            raise ProviderMutationRejectedError(error) from error
        changed = current.provider["autoDeploy"] is True

        assert isinstance(lease, ProviderEvidenceLease)
        lease.assert_current()
        if changed:
            host, token = source.read_dokploy_config(
                control_plane_root=self.root, database_url=self.store.database_url
            )

            def checkpoint() -> None:
                self.require_no_predecessor()
                live = api.fetch_dokploy_target_payload(
                    host=host, token=token, target_type="compose", target_id=self.request.target_id
                )
                if (
                    resolve_first_empty_authority(self.store, self.product, self.request.context)
                    != current.authority
                    or type(live.get("autoDeploy")) is not bool
                    or _provider_configuration(live) != current.provider
                ):
                    raise ValueError(
                        "Original setup authority or provider changed before dispatch."
                    )

            try:
                with self.store.provider_evidence_guard(
                    lease.evidence_reservation,
                    effect_admission_checkpoint=checkpoint,
                ):
                    lease.checkpoint_effect("disable-original-auto-deploy")
            except Exception as error:
                raise ProviderMutationRejectedError(error) from error
            try:
                api.dokploy_request(
                    host=host,
                    token=token,
                    path="/api/compose.update",
                    method="POST",
                    payload={"composeId": self.request.target_id, "autoDeploy": False},
                )
            except Exception as error:
                raise ProviderMutationUnknownError(
                    "Original setup update outcome is unknown."
                ) from error
        try:
            after = self.reviewed_observation(new_effect=False)
            if after.provider["autoDeploy"] is not False:
                raise ValueError("Independent original-target boolean readback did not converge.")
        except Exception as error:
            raise ProviderMutationUnknownError("Original setup readback is unproved.") from error
        return ProviderMutationOutcome(
            202, self.completed_payload(), provider_effect_performed=changed
        )
