from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
import json

import click
from pathlib import Path
from typing import Any, Literal, cast

from control_plane.retained_volume_sweep_program import RETAINED_VOLUME_SWEEP_HOST_PROGRAM
from control_plane.contracts.product_retirement import ProductRetirementRecord
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.contracts.retained_volume_sweep import (
    RetainedVolumeSweepItem,
    RetainedVolumeSweepPlan,
    RetainedVolumeSweepRequest,
    RetainedVolumeSweepResponse,
)
from control_plane.dokploy import api, compose, native_copy_stream, source
from control_plane.dokploy_inventory import TARGET_COLLECTIONS
from control_plane.production_recipient import read_provider_compose
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
)
from control_plane.storage.postgres import PostgresRecordStore

SweepStatus = Literal["eligible", "held", "removed", "absent", "unknown"]


def _strings(value: object, *, parse_environments: bool = False) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        result = set().union(
            *(_strings(item, parse_environments=parse_environments) for item in value.values())
        )
        if parse_environments and isinstance(value.get("env"), str):
            result.update(api.parse_dokploy_env_text(value["env"]).values())
        return result
    if isinstance(value, (list, tuple)):
        collection: list[object] | tuple[object, ...] = value
        return set().union(
            *(_strings(item, parse_environments=parse_environments) for item in collection)
        )
    return set()


def _volume_referenced(name: str, payload: object, *, exact: bool = False) -> bool:
    return any(
        value == name
        or not exact
        and name.startswith((value + "-", value + "_"))
        or name in value.replace(":", "/").split("/")
        for value in _strings(payload, parse_environments=exact)
        if value
    )


def _custody_clear(
    store: PostgresRecordStore, record: ProductRetirementRecord, binding: RetainedVolumeBinding
) -> bool:
    profile = store.read_product_profile_record(record.product)
    if profile.lifecycle_state != "retired" or profile.production_use != "prelaunch":
        return False
    contexts = {record.context} | {preview.preview.context for preview in record.preview_plans}
    if any(store.list_backup_gate_records(context_name=context) for context in contexts):
        return False
    for operation in (
        *store.list_odoo_prod_retained_volume_backup_import_operation_records(),
        *store.list_odoo_prod_backup_restore_operation_records(),
    ):
        if _volume_referenced(binding.volume_name, operation.model_dump(mode="json")):
            return False
    with closing(store.retained_volume_recovery_payloads()) as recovery_payloads:
        if any(_volume_referenced(binding.volume_name, payload) for payload in recovery_payloads):
            return False
    return not any(
        row.provider_target_key != "retained-volume-sweep:" + record.record_id
        for row in store.list_held_provider_target_reservations()
    )


def compose_volume_names(
    document: dict[str, Any], payload: dict[str, Any], allowed_keys: set[str] | None = None
) -> tuple[str, ...]:
    import re
    from control_plane.dokploy.environment import require_unique_compose_inputs
    from control_plane.testing_activation_compose import interpolate

    volumes = document.get("volumes", {})
    if not isinstance(volumes, dict):
        raise ValueError("Compose volume inventory is unavailable.")
    names = []
    for alias, definition in volumes.items():
        if not isinstance(alias, str) or not isinstance(definition, dict | type(None)):
            raise ValueError("Compose volume definition is unavailable.")
        declared = definition or {}
        actual = declared.get("name")
        if actual is None:
            project = document.get("name") or payload.get("appName")
            actual = (
                alias
                if declared.get("external")
                else str(project) + "_" + alias
                if isinstance(project, str) and project
                else None
            )
        if not isinstance(actual, str):
            raise ValueError("Compose volume identity is unavailable.")
        if "$" in actual:
            match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", actual)
            if match is None or allowed_keys is not None and match[1] not in allowed_keys:
                raise ValueError("Compose volume identity uses an unreviewed input.")
            environment = require_unique_compose_inputs(str(payload.get("env") or ""), (match[1],))
            actual = interpolate(actual, environment)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", actual) is None:
            raise ValueError("Compose volume identity is unsupported.")
        names.append(actual)
    return tuple(names)


def _environment_referenced(name: str, owner: Mapping[str, object]) -> bool:
    raw = owner.get("env")
    if raw is not None and not isinstance(raw, str):
        return True
    return _volume_referenced(name, {"env": raw}, exact=True)


def _inventory_clear(
    root: Path, binding: RetainedVolumeBinding, *, ignored_compose_id: str = "", exact: bool = False
) -> bool:
    host, token = source.read_dokploy_config(control_plane_root=root)
    for project in api.fetch_dokploy_projects(host=host, token=token):
        if exact and _environment_referenced(binding.volume_name, project):
            return False
        environments = project.get("environments")
        if not isinstance(environments, list):
            return False
        for environment in environments:
            if not isinstance(environment, dict):
                return False
            if exact and _environment_referenced(binding.volume_name, environment):
                return False
            for kind, (target_type, identifier) in TARGET_COLLECTIONS.items():
                targets = environment.get(kind)
                if not isinstance(targets, list):
                    return False
                for target in targets:
                    if not isinstance(target, dict) or not isinstance(target.get(identifier), str):
                        return False
                    target_id = target[identifier]
                    assert isinstance(target_id, str)
                    if target_type == "compose" and target_id == ignored_compose_id:
                        continue
                    if target_type in {"application", "compose"}:
                        payload = api.fetch_dokploy_target_payload(
                            host=host, token=token, target_type=target_type, target_id=target_id
                        )
                    else:
                        database_payload = api.as_json_object(
                            api.dokploy_request(
                                host=host,
                                token=token,
                                path=f"/api/{target_type}.one",
                                query={identifier: target_id},
                            )
                        )
                        if (
                            database_payload is None
                            or database_payload.get(identifier) != target_id
                        ):
                            return False
                        payload = database_payload
                    if exact and _environment_referenced(binding.volume_name, payload):
                        return False
                    mounts = payload.get("mounts")
                    if not isinstance(mounts, list) or _volume_referenced(
                        binding.volume_name, payload, exact=exact
                    ):
                        return False
                    if target_type == "compose":
                        document = read_provider_compose(
                            compose.fetch_dokploy_converted_compose_file(
                                host=host, token=token, compose_id=target_id
                            )
                        )
                        try:
                            names = compose_volume_names(
                                document, payload if exact else {**payload, "env": ""}
                            )
                        except ValueError:
                            return False
                        if binding.volume_name in names or _volume_referenced(
                            binding.volume_name, document, exact=exact
                        ):
                            return False

    return True


def _host_result(
    root: Path,
    binding: RetainedVolumeBinding,
    mode: str,
    operation_key: str,
    lease: ProviderOperationLease | None = None,
    *,
    retirement: dict[str, object] | None = None,
) -> tuple[SweepStatus, str]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    frames: list[dict[str, object]] = []

    def dispatch(send: Callable[[], None]) -> None:
        if lease is None or not callable(send):
            raise ValueError("A volume removal requires its current operation lease.")
        lease.checkpoint_effect("remove-volume:" + binding.volume_identity_sha256)
        send()

    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=binding.server_id,
        program=RETAINED_VOLUME_SWEEP_HOST_PROGRAM,
        payload={
            "mode": mode,
            "volume": binding.volume_name,
            "identity": binding.volume_identity_sha256,
            "engine_identity": binding.engine_identity_sha256,
            "operation": operation_key,
            **({"retirement": retirement} if retirement else {}),
        },
        receive=frames.append,
        timeout_seconds=120,
        dispatch_guard=dispatch if lease is not None else None,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ProviderMutationUnknownError("Retained volume host result is incomplete.")
    result = frames[0].get("result")
    if not isinstance(result, dict) or result.get("status") not in {
        "eligible",
        "held",
        "removed",
        "absent",
        "unknown",
    }:
        raise ProviderMutationUnknownError("Retained volume host result is invalid.")
    return cast(SweepStatus, result["status"]), str(
        result.get("reason_code", "host_evidence_unavailable")
    )


def build_retained_volume_sweep(
    store: PostgresRecordStore,
    root: Path,
    request: RetainedVolumeSweepRequest,
    now: datetime | None = None,
) -> tuple[RetainedVolumeSweepPlan, tuple[RetainedVolumeBinding, ...]]:
    record = store.read_product_retirement_record(request.retirement_record_id)
    profile = store.read_product_profile_record(record.product)
    observations = (record.provider_observation,) + tuple(
        preview.provider_observation
        for preview in record.preview_plans
        if preview.provider_observation is not None
    )
    bindings = tuple(binding for observed in observations for binding in observed.retained_volumes)
    reason = ""
    if (
        record.mode != "apply"
        or record.outcome not in {"retired", "already_absent"}
        or not record.mutation_evidence.provider_absence_verified
    ):
        reason = "retirement_not_completed"
    elif (
        record.instance in {"prod", "production"}
        or profile.production_use != "prelaunch"
        or record.retained_volume_production_use != "prelaunch"
    ):
        reason = "production_disposition_required"
    elif profile.lifecycle_state != "retired":
        reason = "product_not_retired"
    elif not all(observed.retained_volume_inventory_complete for observed in observations):
        reason = "retained_inventory_unavailable"
    else:
        try:
            completed = datetime.fromisoformat(record.completed_at.replace("Z", "+00:00"))
            current = now or datetime.now(timezone.utc)
            if completed.tzinfo is None or current.tzinfo is None:
                reason = "retirement_time_unknown"
            elif (current - completed).total_seconds() < request.minimum_age_seconds:
                reason = "minimum_age_not_met"
        except ValueError:
            reason = "retirement_time_unknown"
    identities = [(binding.server_id, binding.volume_name) for binding in bindings]
    if len(set(identities)) != len(identities):
        reason = "ambiguous_retained_inventory"
    items: list[RetainedVolumeSweepItem] = []
    for binding in bindings:
        held = reason or (
            "identity_unknown"
            if not binding.volume_identity_sha256 or not binding.server_id
            else ""
        )
        status: SweepStatus = "held"
        if not held:
            try:
                if not _custody_clear(store, record, binding):
                    held = "recovery_custody_unreleased"
                elif not _inventory_clear(root, binding):
                    held = "provider_reference_unavailable"
                else:
                    status, held = _host_result(root, binding, "dry-run", "inspection")
                    if status not in {"eligible", "absent"}:
                        status = "held"
            except (ValueError, OSError, click.ClickException, ProviderMutationUnknownError):
                held = "custody_evidence_unavailable"
        items.append(
            RetainedVolumeSweepItem(
                volume_identity_sha256=binding.volume_identity_sha256,
                status=status,
                reason=held or "eligible",
            )
        )
    if not bindings and reason:
        items.append(
            RetainedVolumeSweepItem(volume_identity_sha256="", status="held", reason=reason)
        )
    return RetainedVolumeSweepPlan(
        retirement_record_id=record.record_id,
        minimum_age_seconds=request.minimum_age_seconds,
        items=tuple(items),
    ), bindings


class RetainedVolumeSweepAdapter:
    def __init__(
        self,
        store: PostgresRecordStore,
        root: Path,
        request: RetainedVolumeSweepRequest,
        plan: RetainedVolumeSweepPlan,
        bindings: Sequence[RetainedVolumeBinding],
        trace_id: str,
    ) -> None:
        self.store, self.root, self.request = store, root, request
        self.plan, self.bindings, self.trace_id = plan, tuple(bindings), trace_id

    def target_key(self) -> str:
        return "retained-volume-sweep:" + self.request.retirement_record_id

    def reconciliation_key(self) -> str:
        return json.dumps(
            {
                "plan": self.plan.model_dump(mode="json"),
                "bindings": [binding.model_dump(mode="json") for binding in self.bindings],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _response(self, items: Sequence[RetainedVolumeSweepItem]) -> dict[str, object]:
        return RetainedVolumeSweepResponse(
            trace_id=self.trace_id,
            records={"retirement_record_id": self.request.retirement_record_id},
            plan_sha256=self.plan.digest(),
            items=tuple(items),
        ).model_dump(mode="json", exclude_none=True)

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        del provider_effect_phase
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        items: list[RetainedVolumeSweepItem] = []
        if not self.bindings:
            return ProviderObservation("present", response_payload=self._response(self.plan.items))
        try:
            for item, binding in zip(self.plan.items, self.bindings, strict=True):
                if item.status != "eligible":
                    items.append(item)
                    continue
                status, reason = _host_result(
                    self.root, binding, "readback", provider_operation_key
                )
                if status == "held" and reason in {"never_started", "no_effect"}:
                    items.append(item.model_copy(update={"status": status, "reason": reason}))
                    continue
                if status != "absent":
                    return ProviderObservation("unknown")
                items.append(item.model_copy(update={"status": status, "reason": reason}))
        except (ValueError, OSError, click.ClickException, ProviderMutationUnknownError):
            return ProviderObservation("unknown")
        return ProviderObservation("present", response_payload=self._response(items))

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        fresh, bindings = build_retained_volume_sweep(self.store, self.root, self.request)
        if (
            fresh != self.plan
            or bindings != self.bindings
            or fresh.digest() != self.request.reviewed_plan_sha256
        ):
            raise ProviderMutationRejectedError(ValueError("Reviewed volume custody changed."))
        if not self.bindings:
            return ProviderMutationOutcome(
                202, self._response(self.plan.items), provider_effect_performed=False
            )
        record = self.store.read_product_retirement_record(self.request.retirement_record_id)
        items: list[RetainedVolumeSweepItem] = []
        performed = False
        for item, binding in zip(self.plan.items, self.bindings, strict=True):
            if item.status != "eligible":
                items.append(item)
                continue
            lease.assert_current()
            if not _custody_clear(self.store, record, binding) or not _inventory_clear(
                self.root, binding
            ):
                if performed:
                    raise ProviderMutationUnknownError(
                        "Volume custody changed after a partial sweep."
                    )
                raise ProviderMutationRejectedError(ValueError("Current volume custody changed."))
            try:
                status, reason = _host_result(
                    self.root, binding, "apply", provider_operation_key, lease
                )
                if status not in {"removed", "absent"}:
                    raise ProviderMutationUnknownError("Retained volume removal is unverified.")
                if (
                    _host_result(self.root, binding, "readback", provider_operation_key)[0]
                    != "absent"
                ):
                    raise ProviderMutationUnknownError("Retained volume absence is unverified.")
            except (ValueError, OSError, click.ClickException) as error:
                raise ProviderMutationUnknownError(
                    "Retained volume transport is unverified."
                ) from error
            performed = True
            items.append(item.model_copy(update={"status": status, "reason": reason}))
        return ProviderMutationOutcome(
            202, self._response(items), provider_effect_performed=performed
        )
