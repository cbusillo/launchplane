"""Retire only the original held, incomplete execute; keep production read-only."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_ROUTE,
    INCOMPLETE_REHEARSAL_TARGET_SUFFIX,
    OdooUpgradeRehearsalRecord,
    IncompleteRehearsalHostResult,
    IncompleteRehearsalRetirementCheckpoint,
    IncompleteRehearsalRetirementReceipt,
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


@dataclass
class IncompleteRehearsalRetirement:
    adapter: OdooRehearsalAdapter
    original_key: str

    def reservation_scope(self) -> str:
        return self.adapter.principal + "|incomplete-retirement"

    def request_fingerprint(self) -> str:
        # One retirement of one original execute. The checkpoint and host fence
        # own review admission, including proven no-effect replacement reviews.
        return canonical_json_sha256(
            {
                "product": self.adapter.product,
                "action": "retire-incomplete",
                "intent": self.adapter.request.intent(),
                "rehearsal_id": self.adapter.request.rehearsal_id,
                "original_key": self.original_key,
            }
        )

    def target_key(self) -> str:
        return self.adapter.target_key() + INCOMPLETE_REHEARSAL_TARGET_SUFFIX

    def reconciliation_key(self) -> str:
        return self.adapter.reconciliation_key()

    @staticmethod
    def review_was_refused(
        progress: IncompleteRehearsalRetirementCheckpoint, native: dict[str, object]
    ) -> bool:
        return (
            progress.reviewed_plan_sha256
            in IncompleteRehearsalHostResult.model_validate(native).refused_reviews
        )

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
        progress = record.incomplete_retirement
        if progress is not None and (
            progress.original_key_sha256
            != canonical_json_sha256({"operation_key": self.original_key})
        ):
            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        return record

    def host(self, mode: str, check: Callable[[], None]) -> dict[str, object]:
        record = self.record()
        script = self.adapter.host_script(record, mode, operation_key=self.original_key)
        try:
            self.adapter.validate_apply_script(record, script)
        except ProviderMutationRejectedError as refusal:
            if mode == "apply":
                raise
            raise refusal.cause from None
        evidence = self.adapter.run_script(record, script, check)
        try:
            result = IncompleteRehearsalHostResult.model_validate(
                json.loads(bytes.fromhex(evidence["rehearsal_result"]))
            )
        except (KeyError, ValueError):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED) from None
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

    @staticmethod
    def plan_digest(
        record: OdooUpgradeRehearsalRecord,
        original: LaunchplaneIdempotencyRecord,
        native: dict[str, object],
    ) -> str:
        return canonical_json_sha256(
            {
                "record": record.binding_sha256,
                "original": {
                    key: getattr(original, key)
                    for key in (
                        "record_id",
                        "scope",
                        "route_path",
                        "idempotency_key",
                        "request_fingerprint",
                        "reconciliation_key",
                        "provider_target_key",
                        "provider_effect_phase",
                        "provider_effect_started_at",
                    )
                },
                "host": native["binding_sha256"],
                "refused_reviews": native.get("refused_reviews", []),
            }
        )

    def qualification(
        self, record: OdooUpgradeRehearsalRecord, check: Callable[[], None]
    ) -> tuple[str, dict[str, object] | None]:
        self.placement()
        progress = record.incomplete_retirement
        if progress is not None and progress.host_disposal is not None:
            return progress.reviewed_plan_sha256, progress.host_disposal.model_dump(mode="json")
        native = self.host("dry-run", check)
        digest = self.plan_digest(record, self.original(record), native)
        if progress is not None and (
            progress.host_binding_sha256 != native["binding_sha256"]
            or progress.reviewed_plan_sha256 != digest
        ):
            if not self.review_was_refused(progress, native):
                if progress.host_binding_sha256 == native["binding_sha256"]:
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
                # Retry the admitted review to let the native fence prove a
                # no-effect refusal, including a failed dispatch before start.
                return progress.reviewed_plan_sha256, native
            if native.get("disposal_started") is not False:
                raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        return digest, native

    def plan(self) -> dict[str, object]:
        with self.adapter.store.testing_replacement_execution_lock(
            self.adapter.request.rehearsal_id
        ) as check:
            if check is None:
                raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
            record = self.record()
            digest, native = self.qualification(record, check)
            public = self.adapter.public(record)
            public.pop("resources_preserved", None)
            return {
                **public,
                "action": "retire-incomplete",
                "applied": False,
                "plan_sha256": digest,
                "original_key_required": True,
                "disposes_raw_capture": True,
                "recovery_tuples_produced": False,
                "host_disposal": native,
                "remaining_work": "placement-publication"
                if native and native["disposed"]
                else "host-disposal",
                "review_refusal_required": bool(
                    record.incomplete_retirement
                    and native
                    and record.incomplete_retirement.host_binding_sha256 != native["binding_sha256"]
                    and not self.review_was_refused(record.incomplete_retirement, native)
                ),
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
        receipt = IncompleteRehearsalRetirementReceipt.model_validate(result)
        progress = record.incomplete_retirement
        if (
            progress is None
            or progress.host_disposal != receipt.host_disposal
            or progress.original_key_sha256
            != canonical_json_sha256({"operation_key": self.original_key})
        ):
            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        return receipt.model_dump(mode="json")

    def run(self, lease: ProviderOperationLease) -> dict[str, object]:
        adapter = self.adapter
        record = adapter.store.read_odoo_upgrade_rehearsal(adapter.request.rehearsal_id)
        if record is not None and record.state == "retired":
            try:
                return self.readback()
            except (RehearsalRefused, ValueError) as error:
                raise ProviderMutationRejectedError(error) from None
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
                digest, native = self.qualification(record, check)
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
            progress = record.incomplete_retirement
            if progress is None or progress.reviewed_plan_sha256 != digest:
                assert native is not None
                if progress is not None and (
                    progress.host_disposal is not None
                    or not self.review_was_refused(progress, native)
                    or native.get("disposal_started") is not False
                ):
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
                progress = IncompleteRehearsalRetirementCheckpoint(
                    original_key_sha256=canonical_json_sha256({"operation_key": self.original_key}),
                    reviewed_plan_sha256=digest,
                    host_binding_sha256=str(native["binding_sha256"]),
                )
                updated = record.model_copy(update={"incomplete_retirement": progress})
                adapter.store.compare_and_write_odoo_upgrade_rehearsal(record, updated)
                record = updated
            if progress.host_disposal is None:
                lease.checkpoint_effect("dispose_incomplete_rehearsal")
                native = self.host("apply", checkpoint)
                disposal = IncompleteRehearsalHostResult.model_validate(native)
                if not disposal.disposed:
                    raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
                progress = IncompleteRehearsalRetirementCheckpoint.model_validate(
                    {
                        **progress.model_dump(mode="json"),
                        "host_disposal": disposal.model_dump(mode="json"),
                    }
                )
                updated = record.model_copy(update={"incomplete_retirement": progress})
                adapter.store.compare_and_write_odoo_upgrade_rehearsal(record, updated)
                record = updated
            assert progress.host_disposal is not None
            native = progress.host_disposal.model_dump(mode="json")
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
            result = IncompleteRehearsalRetirementReceipt.model_validate(result).model_dump(
                mode="json"
            )
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
            record = self.adapter.store.read_odoo_upgrade_rehearsal(
                self.adapter.request.rehearsal_id
            )
            if record is None:
                return ProviderObservation("unknown")
            if record.state == "retired":
                result = self.readback()
                return ProviderObservation(
                    "present",
                    response_payload=provider_operation_response_payload(
                        trace_id=self.adapter.trace,
                        records={"rehearsal": record.rehearsal_id},
                        result=result,
                    ),
                )
            with self.adapter.store.testing_replacement_execution_lock(
                record.rehearsal_id
            ) as check:
                if check is None:
                    return ProviderObservation("unknown")
                record = self.record()
                if provider_effect_phase and record.incomplete_retirement is None:
                    return ProviderObservation("unknown")
                digest, _native = self.qualification(record, check)
                if digest != self.adapter.request.reviewed_plan_sha256:
                    return ProviderObservation("unknown")
                check()
                # Only the reviewed disposal can continue. Native manifests bind
                # partial resources; persisted disposal skips an already-removed
                # compose. Preserve every original effect checkpoint on retry.
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
        except Exception:
            return ProviderObservation("unknown")
