"""Service-owned recovery; missing or uncertain effects keep the testing lane fenced."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Protocol, cast
from time import monotonic

from pydantic import BaseModel

from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationCancellation,
    DurableOperationReconciliationAttestation,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.contracts.testing_replacement_recovery import (
    TestingReplacementEffect,
    TestingReplacementRecoveryJournal,
)
from control_plane.dokploy import api, source
from control_plane.launchplane_reconcile_authorization import (
    launchplane_reconcile_authorization_allows,
)


class TestingRecoveryStore(Protocol):
    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        artifact_id: str = "",
        source_git_ref: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]: ...
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...
    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...
    def read_odoo_stable_target_replacement_operation_record(
        self, operation_id: str
    ) -> OdooStableTargetReplacementOperationRecord: ...
    def list_odoo_stable_target_replacement_operation_records(
        self, *, instance_name: str = "", statuses: tuple[str, ...] = (), limit: int | None = None
    ) -> tuple[OdooStableTargetReplacementOperationRecord, ...]: ...
    def complete_odoo_stable_target_replacement_operation_record(
        self, *, record: OdooStableTargetReplacementOperationRecord, lease_owner: str
    ) -> bool: ...
    def cancel_pending_odoo_stable_target_replacement_operation_record(
        self,
        record: OdooStableTargetReplacementOperationRecord,
        *,
        expected_record: OdooStableTargetReplacementOperationRecord | None = None,
    ) -> bool: ...
    def testing_replacement_execution_lock(
        self, operation_id: str
    ) -> AbstractContextManager[Callable[[], None] | None]: ...
    def request_product_reconcile(
        self, target: ProductReconcileTarget, requested_at: str
    ) -> object: ...


def recovery_binding_fingerprint(bindings: Sequence[object]) -> str:
    return hashlib.sha256(
        json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def current_binding_fingerprint(
    store: TestingRecoveryStore, operation: OdooStableTargetReplacementOperationRecord
) -> str:
    bindings: list[BaseModel] = [
        store.read_product_profile_record(operation.product),
        store.read_dokploy_target_record(
            context_name=operation.context, instance_name=operation.instance
        ),
        store.read_dokploy_target_id_record(
            context_name=operation.context, instance_name=operation.instance
        ),
    ]
    return recovery_binding_fingerprint(
        [item.model_dump(mode="json", exclude_none=True) for item in bindings]
    )


def service_testing_operation(operation: OdooStableTargetReplacementOperationRecord) -> bool:
    authorization = operation.authorization
    return (
        operation.instance == "testing"
        and authorization is not None
        and authorization.grant == "launchplane_reconcile"
        and not authorization.release_decision_record_id
        and operation.request.strategy == "recreate-in-place"
        and operation.request.data_source_mode == "existing"
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def guard_testing_replacement(
    store: TestingRecoveryStore,
    operation: OdooStableTargetReplacementOperationRecord,
    lease_owner: str,
    authorization_checkpoint: Callable[[str], None],
) -> Iterator[Callable[[str], None]]:
    """The worker keeps exclusion through effects and the final inventory commit.

    A dispatch intent commits before HTTP. Lost responses are never acknowledged;
    a timeout is not proof that the remote job ended or that a write did not happen.
    """
    with store.testing_replacement_execution_lock(operation.operation_id) as check_lock:
        if check_lock is None:
            raise RuntimeError("The testing replacement still has an execution owner.")
        journal: TestingReplacementRecoveryJournal | None = None

        def initialize() -> TestingReplacementRecoveryJournal:
            nonlocal journal
            if journal is None:
                journal = TestingReplacementRecoveryJournal(
                    attempt=operation.attempt,
                    target_fingerprint=current_binding_fingerprint(store, operation),
                    target_id=store.read_dokploy_target_id_record(
                        context_name=operation.context,
                        instance_name=operation.instance,
                    ).target_id,
                )
            return journal

        def save() -> None:
            check_lock()
            current = store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            if current.attempt != operation.attempt or current.status != "running":
                raise RuntimeError("Testing replacement lost its execution generation.")
            if (
                journal is not None
                and current_binding_fingerprint(store, current) != journal.target_fingerprint
            ):
                raise RuntimeError("Testing replacement product or target binding changed.")
            if not store.complete_odoo_stable_target_replacement_operation_record(
                record=current.model_copy(update={"recovery_journal": journal}),
                lease_owner=lease_owner,
            ):
                raise RuntimeError("Testing replacement lost its live lease.")

        def checkpoint(name: str) -> None:
            nonlocal journal
            authorization_checkpoint(name)
            journal = initialize()
            if name == "target_replacement_records_commit":
                # An interrupted publication cannot be classified as safe to retry.
                journal = journal.model_copy(
                    update={
                        "effects": (
                            *journal.effects,
                            TestingReplacementEffect(
                                reference=f"{operation.operation_id}:{operation.attempt}:records",
                                path="records_commit",
                                dispatched_at=_now(),
                            ),
                        )
                    }
                )
            save()

        def observe(
            host: str, path: str, payload: api.JsonObject, dispatch: Callable[[], api.JsonValue]
        ) -> api.JsonValue:
            nonlocal journal
            authorization_checkpoint(path)
            journal = initialize()
            provider_fingerprint = recovery_binding_fingerprint([host.rstrip("/")])
            if (
                journal.provider_fingerprint
                and journal.provider_fingerprint != provider_fingerprint
            ):
                raise RuntimeError("Testing replacement provider authority changed.")
            journal = journal.model_copy(update={"provider_fingerprint": provider_fingerprint})
            target_id = str(payload.get("composeId") or payload.get("applicationId") or "")
            if target_id and target_id != journal.target_id:
                raise RuntimeError("Testing replacement provider target changed.")
            reference = f"{operation.operation_id}:{operation.attempt}:{len(journal.effects)}"
            title = ""
            if path in {"/api/compose.deploy", "/api/compose.redeploy"}:
                title = f"launchplane-testing-{hashlib.sha256(reference.encode()).hexdigest()}"
                payload["title"] = title
            effect = TestingReplacementEffect(
                reference=reference,
                path=path,
                dispatched_at=_now(),
                target_id=target_id,
                schedule_id=str(payload.get("scheduleId") or ""),
                deployment_title=title,
            )
            journal = journal.model_copy(update={"effects": (*journal.effects, effect)})
            save()
            response = dispatch()
            schedule_id = effect.schedule_id
            if path == "/api/schedule.create" and isinstance(response, dict):
                schedule_id = api.schedule_key(response)
            acknowledged = effect.model_copy(
                update={"acknowledged": True, "schedule_id": schedule_id}
            )
            journal = journal.model_copy(update={"effects": (*journal.effects[:-1], acknowledged)})
            save()
            return response

        with api.observe_dokploy_mutations(observe):
            yield checkpoint


# Only these acknowledged calls are synchronous configuration writes. New provider
# capabilities must earn a classifier; an unknown endpoint stays fenced.
_SYNCHRONOUS_PATHS = frozenset(
    {
        "/api/compose.update",
        "/api/domain.create",
        "/api/domain.update",
        "/api/compose.getConvertedCompose",
        "/api/schedule.create",
        "/api/schedule.update",
        "/api/schedule.delete",
    }
)
_TERMINAL_PROVIDER_STATUSES = frozenset({"done", "error"})


def inspect_testing_replacement(
    operation: OdooStableTargetReplacementOperationRecord,
    root: Path,
    *,
    deadline_at: float | None = None,
) -> dict[str, str] | None:
    journal = operation.recovery_journal
    if journal is None or journal.attempt != operation.attempt:
        return None
    if any(not effect.acknowledged for effect in journal.effects):
        return None
    host, token = source.read_dokploy_config(control_plane_root=root)
    if journal.effects and journal.provider_fingerprint != recovery_binding_fingerprint(
        [host.rstrip("/")]
    ):
        return None

    # Provider identity and the full returned histories must be readable. An empty
    # target history is valid only when there has been no deploy trigger.
    def read(path: str, query: dict[str, str | int]) -> api.JsonValue:
        timeout: float = 60
        if deadline_at is not None:
            timeout = min(5, deadline_at - monotonic())
            if timeout <= 0:
                raise TimeoutError("Testing recovery inspection deadline expired.")
        return api.dokploy_request(
            host=host, token=token, path=path, query=query, timeout_seconds=timeout
        )

    target = read("/api/compose.one", {"composeId": journal.target_id})
    if not isinstance(target, dict) or target.get("composeId") != journal.target_id:
        return None
    raw_history = read("/api/deployment.allByType", {"id": journal.target_id, "type": "compose"})
    if not isinstance(raw_history, list) or any(not isinstance(item, dict) for item in raw_history):
        return None
    history = cast(list[api.JsonObject], raw_history)
    if any(api.deployment_status(item) not in _TERMINAL_PROVIDER_STATUSES for item in history):
        return None
    inspection = {"target_id": journal.target_id, "state": "quiescent_safe_to_retry"}
    for effect in journal.effects:
        if effect.path in {"/api/compose.deploy", "/api/compose.redeploy"}:
            matched = [item for item in history if item.get("title") == effect.deployment_title]
            if len(matched) != 1 or not api.deployment_key(matched[0]):
                return None
            inspection[effect.reference] = (
                f"deployment:{api.deployment_key(matched[0])}:{api.deployment_status(matched[0])}"
            )
        elif effect.path == "/api/schedule.runManually" or effect.path.startswith("/api/schedule."):
            if effect.path not in _SYNCHRONOUS_PATHS and effect.path != "/api/schedule.runManually":
                return None
            if not effect.schedule_id:
                return None
            schedule = read("/api/schedule.one", {"scheduleId": effect.schedule_id})
            if (
                not isinstance(schedule, dict)
                or api.schedule_key(schedule) != effect.schedule_id
                or schedule.get("enabled") is not False
            ):
                return None
            raw = read("/api/deployment.allByType", {"id": effect.schedule_id, "type": "schedule"})
            if not isinstance(raw, list) or any(
                not isinstance(item, dict)
                or api.deployment_status(item) not in _TERMINAL_PROVIDER_STATUSES
                for item in raw
            ):
                return None
            if effect.path == "/api/schedule.runManually" and not raw:
                return None
            inspection[effect.reference] = f"schedule:{effect.schedule_id}:terminal"
        elif effect.path not in _SYNCHRONOUS_PATHS:
            return None
        else:
            inspection[effect.reference] = "configuration_acknowledged"
    return inspection


def testing_replacement_recovery_read(
    store: TestingRecoveryStore,
    operation: OdooStableTargetReplacementOperationRecord,
    root: Path,
    *,
    diagnostics: list[str] | None = None,
) -> dict[str, object]:
    """Classify persisted evidence without publishing or retiring an operation.

    This view contains fixed codes, counts and agreement facts only. Historical
    dispatch references, coordinates and authorization stay inside the service.
    Missing journal is persisted absence, never provider-effect absence.
    """
    journal = operation.recovery_journal
    view: dict[str, object] = {
        "status": "refused",
        "reason": "recovery_scope_unsupported",
        "journal": "present" if journal is not None else "absent",
        "attempt_matches": journal.attempt == operation.attempt if journal else None,
        "binding_matches": None,
        "effect_count": len(journal.effects) if journal else 0,
        "effects_acknowledged": all(effect.acknowledged for effect in journal.effects)
        if journal
        else None,
        "provider_effects": "unknown",
        "provider_inspected": False,
        "safe_to_release": False,
    }
    if not service_testing_operation(operation):
        return view

    def refuse(reason: str) -> dict[str, object]:
        view["reason"] = reason
        return view

    if operation.status != "reconciliation_required":
        return refuse("recovery_operation_not_held")

    try:
        with store.testing_replacement_execution_lock(operation.operation_id) as check_lock:
            if check_lock is None:
                return refuse("recovery_execution_exclusion_unavailable")
            current = store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            if current != operation:
                return refuse("recovery_operation_changed")
            assert current.authorization is not None
            if not launchplane_reconcile_authorization_allows(
                authorization=current.authorization,
                product=current.product,
                context=current.context,
                instances=("testing",),
                record_store=store,
            ):
                return refuse("recovery_authorization_changed")
            if journal is None:
                from control_plane.testing_legacy_replacement_inspection import (
                    inspect_legacy_replacement,
                )

                fingerprint = current_binding_fingerprint(store, current)
                legacy_inspection = inspect_legacy_replacement(
                    store, current, root, deadline_at=monotonic() + 15
                )
                check_lock()
                if (
                    store.read_odoo_stable_target_replacement_operation_record(
                        operation.operation_id
                    )
                    != current
                    or current_binding_fingerprint(store, current) != fingerprint
                ):
                    return refuse("recovery_operation_or_binding_changed")
                view["provider_inspected"] = legacy_inspection.provider_inspected
                if diagnostics is not None:
                    diagnostics.append(legacy_inspection.description)
                return refuse("recovery_journal_absent")
            if not view["attempt_matches"]:
                return refuse("recovery_attempt_changed")
            view["binding_matches"] = (
                current_binding_fingerprint(store, current) == journal.target_fingerprint
            )
            if not view["binding_matches"]:
                return refuse("recovery_binding_changed")
            if not view["effects_acknowledged"]:
                return refuse("recovery_dispatch_unacknowledged")
            if any(
                effect.path
                not in _SYNCHRONOUS_PATHS
                | {"/api/compose.deploy", "/api/compose.redeploy", "/api/schedule.runManually"}
                for effect in journal.effects
            ):
                return refuse("recovery_effect_unsupported")
            if _now() < current.updated_at:
                return refuse("recovery_inspection_before_fence")
            inspection = inspect_testing_replacement(current, root, deadline_at=monotonic() + 15)
            if inspection is None:
                return refuse("recovery_provider_evidence_incomplete")
            view["provider_inspected"] = True
            check_lock()
            if (
                store.read_odoo_stable_target_replacement_operation_record(operation.operation_id)
                != current
                or current_binding_fingerprint(store, current) != journal.target_fingerprint
            ):
                view.update(
                    journal="unknown",
                    attempt_matches=None,
                    binding_matches=None,
                    effects_acknowledged=None,
                )
                return refuse("recovery_operation_or_binding_changed")
            view.update(status="eligible", reason="", provider_effects="quiescent_observed")
            # Eligibility is a read-time observation, never a cancellation attestation.
            return view
    except Exception as error:
        view.update(attempt_matches=None, binding_matches=None, effects_acknowledged=None)
        logging.getLogger(__name__).info(
            "Testing recovery read unavailable for %s (%s)",
            operation.operation_id,
            type(error).__name__,
        )
        return refuse("recovery_evidence_unavailable")


def recover_stranded_testing_replacements(
    store: TestingRecoveryStore, root: Path
) -> tuple[str, ...]:
    recovered = []
    for operation in store.list_odoo_stable_target_replacement_operation_records(
        instance_name="testing",
        statuses=("reconciliation_required",),
        limit=10,
    ):
        if not service_testing_operation(operation):
            continue
        try:
            with store.testing_replacement_execution_lock(operation.operation_id) as check_lock:
                if check_lock is None:
                    continue
                current = store.read_odoo_stable_target_replacement_operation_record(
                    operation.operation_id
                )
                if current != operation or current.recovery_journal is None:
                    continue
                authorization = current.authorization
                assert authorization is not None
                if not launchplane_reconcile_authorization_allows(
                    authorization=authorization,
                    product=current.product,
                    context=current.context,
                    instances=("testing",),
                    record_store=store,
                ):
                    continue
                if (
                    current_binding_fingerprint(store, current)
                    != current.recovery_journal.target_fingerprint
                ):
                    continue
                inspected_at = _now()
                if inspected_at < current.updated_at:
                    continue
                inspection = inspect_testing_replacement(current, root)
                if inspection is None:
                    continue
                check_lock()
                evidence_digest = recovery_binding_fingerprint([inspection])
                cancellation = DurableOperationCancellation(
                    reason="Launchplane proved its testing effects quiescent; the bounded reconciler may retry.",
                    cancelled_at=_now(),
                    caller=authorization.caller,
                    reconciliation_attestation=DurableOperationReconciliationAttestation(
                        provider_inspected_at=inspected_at,
                        provider_state=inspection["state"],
                        evidence_reference=f"operation:{current.operation_id}:recovery:{evidence_digest}",
                        safe_to_release=True,
                    ),
                )
                cancelled = current.model_copy(
                    update={
                        "status": "cancelled",
                        "phase": "cancelled",
                        "updated_at": cancellation.cancelled_at,
                        "finished_at": cancellation.cancelled_at,
                        "error_code": "",
                        "error_message": "",
                        "cancellation": cancellation,
                        "recovery_journal": current.recovery_journal.model_copy(
                            update={"inspection": inspection}
                        ),
                    }
                )
                if store.cancel_pending_odoo_stable_target_replacement_operation_record(
                    cancelled, expected_record=current
                ):
                    recovered.append(current.operation_id)
                    store.request_product_reconcile(
                        ProductReconcileTarget(product=current.product, target_kind="testing"),
                        requested_at=_now(),
                    )
        except Exception:
            # Provider, storage, or authority uncertainty never releases the fence.
            logging.exception(
                "Testing replacement recovery could not prove safety for %s", operation.operation_id
            )
    return tuple(recovered)
