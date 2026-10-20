"""Retire only the original held, incomplete execute; keep production read-only."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_ROUTE,
    OdooUpgradeRehearsalRecord,
    RehearsalPrerequisite,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.sandbox_placement import created_placement, REHEARSAL_PROJECT_DESCRIPTION
from control_plane.odoo_upgrade_rehearsal import (
    OdooRehearsalAdapter,
    RehearsalRefused,
    require_unaliased_rehearsal,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)


class RetirementHostResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qualified: bool
    disposed: bool
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    containers_absent: bool
    volumes_absent: bool
    network_absent: bool
    raw_source_absent: bool
    public_route: bool
    business_host_frozen: bool


@dataclass
class IncompleteRehearsalRetirement:
    adapter: OdooRehearsalAdapter
    original_key: str

    def target_key(self) -> str:
        return self.adapter.target_key() + ":incomplete-retirement"

    def reconciliation_key(self) -> str:
        return self.adapter.reconciliation_key()

    def original(self, record: OdooUpgradeRehearsalRecord) -> LaunchplaneIdempotencyRecord:
        execute = replace(
            self.adapter, request=self.adapter.request.model_copy(update={"action": "execute"})
        )
        execute.operation_readback(record, self.original_key)
        original = self.adapter.store.read_idempotency_record(
            scope=record.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=record.product),
            idempotency_key=self.original_key,
        )
        if (
            original is None
            or original.state != "reconcile_required"
            or original.provider_effect_phase != "execute"
            or not original.provider_effect_started_at
        ):
            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        return original

    def record(self) -> OdooUpgradeRehearsalRecord:
        adapter = self.adapter
        record = adapter.store.read_odoo_upgrade_rehearsal(adapter.request.rehearsal_id)
        if (
            record is None
            or record.product != adapter.product
            or record.principal != adapter.principal
            or record.intent_sha256 != canonical_json_sha256(adapter.request.intent())
            or record.state != "prepared"
            or record.copies
            or not all((record.compose_id, record.environment_id, record.project_id))
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        require_unaliased_rehearsal(adapter.store, record)
        self.original(record)
        return record

    def host(self, mode: str, check: Callable[[], None]) -> dict[str, object]:
        record = self.record()
        script = self.adapter.host_script(record, mode, operation_key=self.original_key)
        self.adapter.validate_apply_script(record, script)
        evidence = self.adapter.run_script(record, script, check)
        result = RetirementHostResult.model_validate(
            json.loads(bytes.fromhex(evidence["rehearsal_result"]))
        )
        if (
            not result.qualified
            or result.intent_sha256 != record.intent_sha256
            or result.attempt_sha256 != canonical_json_sha256({"operation_key": self.original_key})
            or result.public_route
            or result.business_host_frozen
            or result.disposed
            != all(
                (
                    result.containers_absent,
                    result.volumes_absent,
                    result.network_absent,
                    result.raw_source_absent,
                )
            )
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        return result.model_dump(mode="json")

    def placement(self, apply: bool = False, check: Callable[[], None] = lambda: None) -> bool:
        """Remove only the exact isolated placement, through existing provider retirement."""
        adapter = self.adapter
        record = adapter.store.read_odoo_upgrade_rehearsal(adapter.request.rehearsal_id)
        if record is None:
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        host, token = source.read_dokploy_config(control_plane_root=adapter.root)
        projects = api.fetch_dokploy_projects(host=host, token=token)
        matches = [
            project
            for project in projects
            if project.get("name") == record.rehearsal_id
            or project.get("projectId") == record.project_id
        ]
        if not matches:
            return True
        if len(matches) != 1:
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        project = matches[0]
        if (
            project.get("name") != record.rehearsal_id
            or project.get("projectId") != record.project_id
            or project.get("description") != REHEARSAL_PROJECT_DESCRIPTION
        ):
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        placement = created_placement(adapter.root, record.rehearsal_id)
        if placement not in {
            (record.project_id, record.environment_id, record.compose_id),
            (record.project_id, record.environment_id, ""),
        }:
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        environments = project.get("environments")
        if not isinstance(environments, list) or not 1 <= len(environments) <= 2:
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        for environment in environments:
            if (
                not isinstance(environment, dict)
                or not (
                    (
                        environment.get("name") == record.rehearsal_id
                        and environment.get("environmentId") == record.environment_id
                    )
                    or (
                        environment.get("name") == "production"
                        and environment.get("isDefault") is True
                    )
                )
                or any(
                    environment.get(key) != []
                    for key in (
                        "applications",
                        "postgres",
                        "mysql",
                        "mariadb",
                        "mongo",
                        "redis",
                        "libsql",
                    )
                )
            ):
                raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        if placement[2]:
            from control_plane.dokploy_target_setup_http import (
                fetch_dokploy_compose_domains_for_target_setup,
            )

            payload = api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=record.compose_id
            )
            if (
                payload.get("serverId") != record.server_id
                or payload.get("environmentId") != record.environment_id
                or not re.fullmatch(
                    re.escape(record.rehearsal_id) + r"(?:-[a-z0-9]{6})?",
                    str(payload.get("appName") or ""),
                )
                or payload.get("env") not in (None, "")
                or payload.get("composeFile") not in (None, "")
                or fetch_dokploy_compose_domains_for_target_setup(
                    host=host, token=token, compose_id=record.compose_id
                )
            ):
                raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        if not apply:
            return False
        if placement[2]:
            from control_plane.workflows.odoo_preview_runtime import _delete_compose

            check()
            _delete_compose(
                host=host, token=token, compose_id=record.compose_id, delete_volumes=False
            )
            if created_placement(adapter.root, record.rehearsal_id) != (
                record.project_id,
                record.environment_id,
                "",
            ):
                raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        check()
        self.placement(False, check)
        # The provider owns environment/project cascade removal, only after the
        # rehearsal compose and all its separately guarded storage are gone.
        api.dokploy_request(
            host=host,
            token=token,
            path="/api/project.remove",
            method="POST",
            payload={"projectId": record.project_id},
        )
        if not self.placement(False, check):
            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        return True

    def plan(self) -> dict[str, object]:
        with self.adapter.store.testing_replacement_execution_lock(
            self.adapter.request.rehearsal_id
        ) as check:
            if check is None:
                raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
            record = self.record()
            original = self.original(record)
            self.placement()
            native = self.host("dry-run", check)
            return {
                **self.adapter.public(record),
                "action": "retire-incomplete",
                "applied": False,
                "plan_sha256": canonical_json_sha256(
                    {
                        "record": record.binding_sha256,
                        "original": original.model_dump(mode="json"),
                        "host": native["binding_sha256"],
                    }
                ),
                "original_key_required": True,
                "disposes_raw_capture": True,
                "recovery_tuples_produced": False,
            }

    def readback(self) -> dict[str, object]:
        adapter = self.adapter
        record = adapter.store.read_odoo_upgrade_rehearsal(adapter.request.rehearsal_id)
        if (
            record is None
            or record.product != adapter.product
            or record.principal != adapter.principal
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        original = adapter.store.read_idempotency_record(
            scope=record.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=record.product),
            idempotency_key=self.original_key,
        )
        result = original.response_payload.get("result") if original else None
        if (
            record.state != "retired"
            or original is None
            or original.state != "completed"
            or record.intent_sha256 != canonical_json_sha256(adapter.request.intent())
            or original.provider_target_key != record.rehearsal_id
            or not isinstance(result, dict)
            or result.get("action") != "retire-incomplete"
            or result.get("rehearsal_id") != record.rehearsal_id
            or result.get("disposed") is not True
            or not self.placement()
        ):
            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        return result

    def run(self, lease: ProviderOperationLease) -> dict[str, object]:
        adapter = self.adapter
        record = adapter.store.read_odoo_upgrade_rehearsal(adapter.request.rehearsal_id)
        if record is not None and record.state == "retired":
            return self.readback()
        with adapter.store.testing_replacement_execution_lock(
            adapter.request.rehearsal_id
        ) as check:
            if check is None:
                raise ProviderMutationRejectedError(
                    RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
                )
            try:
                record = self.record()
                original = self.original(record)
                self.placement()
                native = self.host("dry-run", check)
                digest = canonical_json_sha256(
                    {
                        "record": record.binding_sha256,
                        "original": original.model_dump(mode="json"),
                        "host": native["binding_sha256"],
                    }
                )
                if digest != adapter.request.reviewed_plan_sha256:
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
            except RehearsalRefused as error:
                raise ProviderMutationRejectedError(error) from None

            def checkpoint() -> None:
                lease.assert_current()
                check()
                if self.record() != record or self.original(record) != original:
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)

            checkpoint()
            lease.checkpoint_effect("dispose_incomplete_rehearsal")
            native = self.host("apply", checkpoint)
            if native.get("disposed") is not True:
                raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
            lease.checkpoint_effect("retire_incomplete_placement")
            if not self.placement(True, checkpoint):
                raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
            checkpoint()
            updated = record.model_copy(
                update={"state": "retired", "updated_at": datetime.now(timezone.utc)}
            )
            result = {
                **adapter.public(updated),
                "action": "retire-incomplete",
                "completed": True,
                "disposed": True,
                "resources_preserved": False,
                "raw_source_retained": False,
                "original_operation_released": True,
                "recovery_tuples_produced": False,
                "host_disposal": native,
            }
            adapter.store.complete_incomplete_rehearsal_retirement(
                expected=record,
                replacement=updated,
                original=original,
                trace_id=adapter.trace,
                result=result,
            )
            return result

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        result = self.run(lease)
        return ProviderMutationOutcome(
            202,
            provider_operation_response_payload(
                trace_id=self.adapter.trace,
                records={"rehearsal": self.adapter.request.rehearsal_id},
                result=result,
            ),
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            result = self.readback()
        except (RehearsalRefused, ValueError, OSError):
            return ProviderObservation("unknown")
        return ProviderObservation(
            "present",
            response_payload=provider_operation_response_payload(
                trace_id=self.adapter.trace,
                records={"rehearsal": self.adapter.request.rehearsal_id},
                result=result,
            ),
        )
