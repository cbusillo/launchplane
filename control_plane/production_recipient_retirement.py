from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.production_recipient import (
    ProductionRecipientRecord,
    RecipientRecoveryRequest,
)
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.dokploy import api, compose, source
from control_plane.integration_allowances import canonical_sha256
from control_plane.production_consumer_reads import (
    current_source,
    qualify_consumer_reads,
    stable_source_authority,
    MAX_OBSERVATION_AGE,
)
from control_plane.production_recipient import (
    authority_snapshot,
    custody,
    provider_binding_digest,
    read_provider_compose,
)
from control_plane.production_recipient_publication import require_owned_publication_rows
from control_plane.production_recipient_recovery import RecipientRecoveryAdapter
from control_plane.production_recipient_retirement_host import inspect_retirement_resources
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.retained_volume_sweep_execution import (
    compose_volume_names,
    _host_result,
    _inventory_clear,
    _volume_referenced,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_promotion_backup import (
    GENERIC_WEB_PROMOTION_BACKUP_ACTION,
    require_production_promotion_backup,
)
from control_plane.workflows.ship import utc_now_timestamp

RETIREMENT_ROUTE = "/v1/products/{product}/production-recipient/retirement"
COMPOSE_DELETE_PATH = "/api/compose.delete"
ROLLBACK_WINDOW = timedelta(hours=24)
RETIREMENT_PHASES = frozenset({"retiring_old", "old_retired"})


class RecipientRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context: str = Field(min_length=1)
    preparation_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    publication_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recovery_generation: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    backup_record_id: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback", "receipt"] = "dry-run"
    reason: str = Field(min_length=1, max_length=512)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RetiredVolumeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity_sha256: str
    status: Literal["eligible", "held", "absent", "unknown"]


class RecipientRetirementPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    context: str
    preparation_plan_sha256: str
    publication_sha256: str
    plan_sha256: str
    observation_window_ends_at: str
    fallback_ended_at: str = ""
    compose_removed: bool = False
    completed: bool = False
    volumes: tuple[RetiredVolumeResult, ...] = ()


@dataclass
class RetirementDispatchLease:
    lease: ProviderOperationLease
    validate: Callable[[], None]

    def assert_current(self) -> None:
        self.lease.assert_current()

    def checkpoint_effect(self, phase: str) -> None:
        self.lease.assert_current()
        self.validate()
        self.lease.checkpoint_effect(phase)


def retirement_request_fingerprint(product: str, request: RecipientRetirementRequest) -> str:
    from control_plane.http_routes.mutation_support import request_fingerprint

    return request_fingerprint(
        {
            "product": product,
            **request.model_dump(mode="json", exclude={"recovery_generation", "backup_record_id"}),
        }
    )


def latest_volume_permit(
    record: ProductionRecipientRecord, identity: str
) -> tuple[str, dict[str, Any]]:
    prefix = "retirement_permit:" + identity + ":"
    keys = sorted(
        (key for key in record.execution if key.startswith(prefix)),
        key=lambda key: int(key.removeprefix(prefix)),
    )
    if not keys:
        return "", {}
    return keys[-1], record.execution[keys[-1]]


def require_before_retirement(record: ProductionRecipientRecord) -> None:
    if record.phase in RETIREMENT_PHASES or record.execution.get("retirement_started_at"):
        raise ValueError(
            "Fast fallback ended before old-resource retirement; preserve retirement custody."
        )


def volume_binding(manifest: dict[str, Any], row: dict[str, Any]) -> RetainedVolumeBinding:
    return RetainedVolumeBinding(
        volume_name=row["name"],
        server_id=manifest["server"],
        application_id=manifest["target"],
        application_name=manifest["project"],
        volume_identity_sha256=row["identity"],
        engine_identity_sha256=row["engine_identity"],
    )


def require_retirement_protection(
    store: PostgresRecordStore,
    record: ProductionRecipientRecord,
    request: RecipientRetirementRequest,
    after: datetime,
) -> float:
    evidence = require_production_promotion_backup(
        record_store=store,
        product=record.product,
        context=record.context,
        instance="prod",
        promotion_action=GENERIC_WEB_PROMOTION_BACKUP_ACTION,
        backup_record_id=request.backup_record_id,
    ).evidence
    if any(
        datetime.fromisoformat(evidence[key]) < after
        for key in ("snapshot_finished_at", "independent_backup_finished_at")
    ):
        raise ValueError("Current protection must follow the observation window.")
    policy = store.read_production_backup_policy_record(evidence["policy_record_id"])
    return min(
        datetime.fromisoformat(evidence["snapshot_finished_at"]).timestamp()
        + policy.fast_snapshot.max_evidence_age_seconds,
        datetime.fromisoformat(evidence["independent_backup_finished_at"]).timestamp()
        + policy.independent_backup.max_evidence_age_seconds,
    )


def require_retirement_evidence(
    adapter: "RecipientRetirementAdapter", record: ProductionRecipientRecord
) -> tuple[dict[str, Any], float]:
    request = adapter.request
    if (
        (record.plan_sha256, record.publication.get("published_sha256"))
        != (request.preparation_plan_sha256, request.publication_sha256)
        or record.phase not in {"published", *RETIREMENT_PHASES}
        or request.mode != "dry-run"
        and (record.actor, record.scope) != (adapter.actor, adapter.scope)
    ):
        raise ValueError("Retirement requires the original preparer and exact current publication.")
    published = datetime.fromisoformat(record.publication["published_at"])
    if published.utcoffset() != timedelta(0):
        raise ValueError("The successful cutover time is unknown.")
    after = published + ROLLBACK_WINDOW
    if datetime.now(timezone.utc) < after:
        raise ValueError("The successful cutover observation window has not ended.")
    require_owned_publication_rows(adapter.store, record)
    state = current_source(adapter.store, adapter.root, record.product, record.context)
    if state.cycle is None or state.cycle.observed < after or state.cycle.started < after:
        raise ValueError("Fresh post-window healthy incremental sync is required.")
    consumers = qualify_consumer_reads(
        store=adapter.store,
        root=adapter.root,
        product=record.product,
        context=record.context,
        can_read=adapter.can_read,
    )
    if (
        consumers.status != "verified"
        or consumers.publication_sha256 != request.publication_sha256
        or datetime.fromisoformat(consumers.observed_at) < after
        or {row.consumer for row in consumers.consumers} != set(state.review.consumers)
        or not all(
            row.status == row.query == row.populated == row.destination == "verified"
            for row in consumers.consumers
        )
    ):
        raise ValueError("Every reviewed production consumer requires a fresh actual read.")
    recovery = RecipientRecoveryAdapter(
        adapter.root,
        adapter.store,
        record.product,
        RecipientRecoveryRequest(
            context=record.context,
            preparation_plan_sha256=record.plan_sha256,
            purpose="published",
            generation=request.recovery_generation,
            reason=request.reason,
        ),
        record.actor,
        record.scope,
        adapter.trace_id,
    )
    _, recovered = recovery.current()
    if (
        not recovered.recovered
        or recovered.captured_database_empty is not False
        or datetime.fromisoformat(recovered.capture_started_at) < after
        or recovered.publication_sha256 != request.publication_sha256
    ):
        raise ValueError("Fresh post-window populated restored-data recovery is required.")
    protection_until = require_retirement_protection(adapter.store, record, request, after)
    return authority_snapshot(adapter.store, record.product, record.context), min(
        protection_until,
        datetime.fromisoformat(recovered.recovery_fresh_until).timestamp(),
        (state.cycle.observed + MAX_OBSERVATION_AGE).timestamp(),
        (datetime.fromisoformat(consumers.completed_at) + MAX_OBSERVATION_AGE).timestamp(),
    )


def service_references(store: PostgresRecordStore, names: tuple[str, ...]) -> bool:
    collections = (
        store.list_dokploy_target_records(),
        store.list_dokploy_target_id_records(),
        store.list_physical_provider_target_records(),
        store.list_runtime_environment_records(),
        store.list_preview_records(),
        store.list_preview_generation_records(),
        store.list_private_health_endpoint_records(),
        store.list_production_backup_policy_records(status="active"),
        store.list_odoo_prod_backup_restore_operation_records(),
        store.list_odoo_prod_retained_volume_backup_import_operation_records(),
    )
    for rows in collections:
        for row in rows:
            if any(
                _volume_referenced(name, row.model_dump(mode="json"), exact=True) for name in names
            ):
                return True
    with closing(store.retained_volume_recovery_payloads()) as payloads:
        if any(
            any(_volume_referenced(name, payload, exact=True) for name in names)
            for payload in payloads
        ):
            return True
    return False


def retained_manifest(root: Path, record: ProductionRecipientRecord) -> dict[str, Any]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=record.retained_target_id.target_id
    )
    if (
        provider_binding_digest(payload) != record.retained_provider_sha256
        or payload.get("composeType") != "docker-compose"
        or payload.get("autoDeploy") is not False
        or not isinstance(payload.get("backups"), list)
        or payload["backups"]
    ):
        raise ValueError(
            "Retained Compose ownership, deployment or backup references are unqualified."
        )
    if api.list_dokploy_schedules(
        host=host,
        token=token,
        target_id=record.retained_target_id.target_id,
        schedule_type="compose",
    ):
        raise ValueError("Retained Compose has schedule references.")
    document = read_provider_compose(
        compose.fetch_dokploy_converted_compose_file(
            host=host, token=token, compose_id=record.retained_target_id.target_id
        )
    )
    names = compose_volume_names(document, payload, set(record.request.volume_keys.values()))
    declared = set(document.get("volumes", {}))
    if document.get("configs") or document.get("secrets"):
        raise ValueError("Old file-backed configuration requires separate retained custody.")
    for service in document["services"].values():
        for mount in service.get("volumes", []):
            if isinstance(mount, str):
                parts = mount.split(":")
                if len(parts) not in {2, 3} or parts[0] not in declared:
                    raise ValueError("Old bind or anonymous storage is unsupported.")
            elif (
                not isinstance(mount, dict)
                or mount.get("type", "volume") != "volume"
                or mount.get("source") not in declared
            ):
                raise ValueError("Only exact old named storage can be retired.")
    if payload.get("domains") or payload.get("serviceNetworks"):
        raise ValueError("The old Compose retains routing or shared network references.")
    if len(names) != len(set(names)) or not names:
        raise ValueError("Retained storage is missing or aliased.")
    spec = {
        "server": payload.get("serverId"),
        "project": payload["appName"],
        "target": payload["composeId"],
        "volumes": sorted(names),
        "application_service": record.request.application_service,
    }
    if (
        not isinstance(spec["server"], str)
        or not spec["server"]
        or spec["server"] == record.request.destination_server_id
        and spec["project"] == record.app_name
    ):
        raise ValueError("The retained placement is unqualified.")
    observed = inspect_retirement_resources(root, spec)
    if {row["name"] for row in observed["volumes"]} != set(names):
        raise ValueError("Every reviewed retained volume must be positively identified.")
    return {
        **spec,
        "resources": observed["volumes"],
        "engine_identity": observed["engine_identity"],
        "containers": observed["containers"],
    }


@dataclass
class RecipientRetirementAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: RecipientRetirementRequest
    actor: str
    scope: str
    trace_id: str
    original_key: str
    can_read: Callable[[str, str, str], bool]
    reconciled: dict[str, str] = field(default_factory=dict, init=False)

    def record(self) -> ProductionRecipientRecord:
        result = custody(self.store, self.product, self.request.context)
        if result is None:
            raise ValueError("Retained production custody is required.")
        return result

    def provider_target_origin(self) -> ProviderTargetOrigin:
        record = self.record()
        return ProviderTargetOrigin(
            product=self.product,
            context=record.context,
            instance="prod",
            provider_id="dokploy",
            target_type="compose",
            target_id=record.retained_target_id.target_id,
        )

    def target_key(self) -> str:
        return self.provider_target_origin().target_key()

    def reconciliation_key(self) -> str:
        return self.request.reviewed_plan_sha256

    def require_original(self, record: ProductionRecipientRecord) -> None:
        if self.request.mode == "dry-run":
            return
        owner = record.execution.get("retirement_owner") or {}
        if owner and (
            (owner.get("actor"), owner.get("scope"), owner.get("key"))
            != (self.actor, self.scope, self.original_key)
            or self.request.reviewed_plan_sha256
            and owner.get("plan") != self.request.reviewed_plan_sha256
        ):
            raise ValueError("Retain retirement's original actor, key and reviewed plan.")

    def current(self) -> tuple[ProductionRecipientRecord, dict[str, Any], dict[str, Any], float]:
        record = self.record()
        self.require_original(record)
        authority, until = require_retirement_evidence(self, record)
        manifest = record.execution.get("retirement_manifest") or retained_manifest(
            self.root, record
        )
        self.references(record, manifest)
        if datetime.now(timezone.utc).timestamp() >= until:
            raise ValueError("Post-window evidence expired during reference checks.")
        return record, manifest, authority, until

    def references(self, record: ProductionRecipientRecord, manifest: dict[str, Any]) -> None:
        def endpoint(url: str) -> tuple[str | None, int]:
            parsed = urlsplit(url)
            return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

        probe = RetainedVolumeBinding(
            volume_name=manifest["project"],
            server_id=manifest["server"],
            application_id=manifest["target"],
            application_name=manifest["project"],
        )
        if not _inventory_clear(
            self.root, probe, ignored_compose_id=manifest["target"], exact=True
        ):
            raise ValueError("Old Compose provider references are incomplete or protected.")
        retained_endpoints = {
            endpoint(row["url"]) for row in record.publication["before"]["health_endpoints"]
        }
        if any(
            endpoint(row.url) in retained_endpoints
            for row in self.store.list_private_health_endpoint_records()
        ):
            raise ValueError("A current monitor still references the old placement.")
        if service_references(
            self.store,
            (manifest["target"], manifest["project"], manifest["project"] + "_default"),
        ):
            raise ValueError("The old Compose retains service or recovery references.")
        owner = (
            record.execution.get("retirement_owner", {}) if self.request.mode == "dry-run" else {}
        )
        original_scope = owner.get("scope", self.scope)
        original_key = owner.get("key", self.original_key)
        for reservation in self.store.list_held_provider_target_reservations(
            provider_target_key=self.target_key()
        ):
            if (reservation.scope, reservation.route_path, reservation.idempotency_key) != (
                original_scope,
                RETIREMENT_ROUTE.format(product=self.product),
                original_key,
            ):
                raise ValueError("An unsettled provider operation retains custody.")

    def digest(
        self, record: ProductionRecipientRecord, manifest: dict[str, Any], authority: dict[str, Any]
    ) -> str:
        return canonical_sha256(
            {
                "request": self.request.model_dump(
                    mode="json",
                    exclude={
                        "mode",
                        "reviewed_plan_sha256",
                        "recovery_generation",
                        "backup_record_id",
                    },
                ),
                "preparation": record.plan_sha256,
                "publication": record.publication,
                "manifest": manifest,
                "authority": stable_source_authority(authority),
            }
        )

    def plan(
        self, record: ProductionRecipientRecord, manifest: dict[str, Any], digest: str
    ) -> RecipientRetirementPlan:
        return RecipientRetirementPlan(
            product=self.product,
            context=record.context,
            preparation_plan_sha256=record.plan_sha256,
            publication_sha256=self.request.publication_sha256,
            plan_sha256=digest,
            observation_window_ends_at=(
                datetime.fromisoformat(record.publication["published_at"]) + ROLLBACK_WINDOW
            ).isoformat(),
            fallback_ended_at=record.execution.get("retirement_started_at", ""),
            compose_removed=bool(record.execution.get("retirement_compose_removed")),
            completed=record.phase == "old_retired",
            volumes=tuple(
                RetiredVolumeResult(
                    identity_sha256=row["identity"],
                    status=record.execution.get(
                        "retirement_volume:" + row["identity"],
                        "unknown"
                        if latest_volume_permit(record, row["identity"])[0]
                        else "held"
                        if row["held"]
                        else "eligible",
                    ),
                )
                for row in manifest["resources"]
            ),
        )

    def response(self, result: RecipientRetirementPlan) -> dict[str, object]:
        return provider_operation_response_payload(
            trace_id=self.trace_id, records={}, result=result.model_dump(mode="json")
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            record = self.record()
            self.require_original(record)
            if (
                record.execution.get("retirement_operation") != provider_operation_key
                or reconciliation_key != self.request.reviewed_plan_sha256
            ):
                return ProviderObservation("unknown")
            manifest = record.execution["retirement_manifest"]
            if record.phase == "old_retired":
                return ProviderObservation(
                    "present",
                    response_payload=self.response(self.plan(record, manifest, reconciliation_key)),
                )
            if not record.execution.get("retirement_compose_dispatched"):
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
            self.require_compose_absent(manifest)
            reconciled = {}
            for row in manifest["resources"]:
                identity = row["identity"]
                permit_key, permit = latest_volume_permit(record, identity)
                if (
                    not permit
                    or record.execution.get("retirement_fenced:" + permit_key)
                    or record.execution.get("retirement_volume:" + identity)
                ):
                    continue
                status, reason = _host_result(
                    self.root,
                    volume_binding(manifest, row),
                    "readback",
                    provider_operation_key + ":" + permit_key,
                    retirement=permit,
                )
                if status == "absent":
                    reconciled[identity] = "absent"
                elif status == "held" and reason in {"never_started", "no_effect"}:
                    reconciled[identity] = "fenced"
                else:
                    return ProviderObservation("unknown")
            self.reconciled = reconciled
            return ProviderObservation("absent", retry_safe=True, preserve_effect_checkpoint=True)
        except (ValueError, OSError, KeyError, click.ClickException):
            return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            record, manifest, authority, until = self.current()
            observation_end = (
                datetime.fromisoformat(record.publication["published_at"]) + ROLLBACK_WINDOW
            )
            digest = self.digest(record, manifest, authority)
            if (
                digest != self.request.reviewed_plan_sha256
                or record.phase not in {"published", "retiring_old"}
                or record.phase == "retiring_old"
                and record.execution.get("retirement_operation") != provider_operation_key
                or not self.original_key
                or not isinstance(lease, ProviderEvidenceLease)
            ):
                raise ValueError("Review the exact unstarted retirement and original key.")
        except Exception as error:
            self.reject_unstarted(error, provider_operation_key)
            raise

        def save(**entries: Any) -> None:
            nonlocal record
            lease.assert_current()
            updated = record.model_copy(update={"execution": {**record.execution, **entries}})
            self.store.write_production_recipient(
                expected=record.model_dump(mode="json"),
                replacement=updated.model_dump(mode="json"),
                reservation=lease.evidence_reservation(),
                expected_authority=authority,
            )
            record = updated

        try:
            lease.assert_current()
            if record.phase == "published":
                record = self.start(
                    record, manifest, authority, digest, provider_operation_key, lease
                )
            if not record.execution.get("retirement_compose_dispatched"):
                self.references(record, manifest)
                require_retirement_protection(self.store, record, self.request, observation_end)
                if datetime.now(timezone.utc).timestamp() >= until:
                    raise ValueError("Retirement evidence expired before Compose dispatch.")
                host, token = source.read_dokploy_config(control_plane_root=self.root)
                lease.checkpoint_effect("retirement_delete_compose")
                save(retirement_compose_dispatched=True)
                api.dokploy_request(
                    host=host,
                    token=token,
                    path=COMPOSE_DELETE_PATH,
                    method="POST",
                    payload={"composeId": manifest["target"], "deleteVolumes": False},
                )
            self.require_compose_absent(manifest)
            save(retirement_compose_removed=True)
            for row in manifest["resources"]:
                identity = row["identity"]
                if record.execution.get("retirement_volume:" + identity):
                    continue
                permit_key, previous_permit = latest_volume_permit(record, identity)
                reconciled = self.reconciled.get(identity)
                if reconciled == "absent":
                    save(**{"retirement_volume:" + identity: "absent"})
                    continue
                if reconciled == "fenced":
                    save(**{"retirement_fenced:" + permit_key: True})
                if previous_permit and not record.execution.get("retirement_fenced:" + permit_key):
                    raise ProviderMutationUnknownError(
                        "Observe the original volume step before continuing."
                    )
                _, _, current_authority, current_until = self.current()
                if current_authority != authority:
                    raise ValueError("Current publication authority changed.")
                host_observed = self.require_compose_absent(manifest)
                observed_volume = next(
                    (item for item in host_observed["volumes"] if item["name"] == row["name"]), None
                )
                if observed_volume is None:
                    raise ValueError("Retained volume identity changed before its step.")
                if (
                    observed_volume["identity"] != row["identity"]
                    or observed_volume["engine_identity"] != row["engine_identity"]
                ):
                    raise ValueError("Retained volume identity changed before its step.")
                binding = volume_binding(manifest, row)
                held = (
                    row["held"]
                    or observed_volume["held"]
                    or not _inventory_clear(self.root, binding, exact=True)
                    or service_references(self.store, (row["name"],))
                )
                if held:
                    save(**{"retirement_volume:" + identity: "held"})
                    continue
                attempt = int(permit_key.rsplit(":", 1)[-1]) + 1 if permit_key else 1
                permit_key = "retirement_permit:" + identity + ":" + str(attempt)
                fresh_until = min(until, current_until)
                permit = {
                    "binding": digest,
                    "fresh_until": fresh_until,
                    "boot_id": host_observed["boot_id"],
                    "monotonic_deadline": host_observed["monotonic"]
                    + fresh_until
                    - host_observed["received_at"],
                }
                save(**{permit_key: permit})

                def validate_volume_dispatch() -> None:
                    if not _inventory_clear(self.root, binding, exact=True):
                        raise ValueError("Volume provider references changed before dispatch.")
                    current_source(self.store, self.root, record.product, record.context)
                    self.references(record, manifest)
                    require_retirement_protection(self.store, record, self.request, observation_end)
                    if (
                        authority_snapshot(self.store, record.product, record.context) != authority
                        or datetime.now(timezone.utc).timestamp() >= min(until, current_until)
                        or service_references(self.store, (row["name"],))
                    ):
                        raise ValueError("Volume custody changed before dispatch.")

                status, reason = _host_result(
                    self.root,
                    binding,
                    "apply",
                    provider_operation_key + ":" + permit_key,
                    RetirementDispatchLease(lease, validate_volume_dispatch),
                    retirement=permit,
                )
                if status == "held" and reason in {"never_started", "no_effect"}:
                    save(**{"retirement_fenced:" + permit_key: True})
                    raise ProviderMutationUnknownError(
                        "The original volume attempt was fenced without effect; refresh evidence before another original-key apply."
                    )
                elif status in {"removed", "absent"}:
                    save(**{"retirement_volume:" + identity: "absent"})
                else:
                    raise ProviderMutationUnknownError("Exact volume retirement is uncertain.")
            terminal = record.model_copy(update={"phase": "old_retired"})
            self.store.write_production_recipient(
                expected=record.model_dump(mode="json"),
                replacement=terminal.model_dump(mode="json"),
                reservation=lease.evidence_reservation(),
                expected_authority=authority,
            )
            return ProviderMutationOutcome(
                202, self.response(self.plan(terminal, manifest, digest))
            )
        except Exception as error:
            self.reject_unstarted(error, provider_operation_key)
            raise

    def reject_unstarted(self, error: Exception, operation: str) -> None:
        try:
            started = self.record().execution.get("retirement_operation") == operation
        except Exception:
            raise ProviderMutationUnknownError(
                "Original retirement custody is unreadable."
            ) from error
        if started:
            raise ProviderMutationUnknownError(
                "Retain original retirement custody; reconcile recorded steps before continuation."
            ) from error
        raise ProviderMutationRejectedError(error) from error

    def start(
        self,
        record: ProductionRecipientRecord,
        manifest: dict[str, Any],
        authority: dict[str, Any],
        digest: str,
        provider_operation_key: str,
        lease: ProviderOperationLease,
    ) -> ProductionRecipientRecord:
        if not isinstance(lease, ProviderEvidenceLease):
            raise ValueError("Retirement requires its current evidence lease.")
        retiring = record.model_copy(
            update={
                "phase": "retiring_old",
                "execution": {
                    **record.execution,
                    "retirement_started_at": utc_now_timestamp(),
                    "retirement_owner": {
                        "actor": self.actor,
                        "scope": self.scope,
                        "key": self.original_key,
                        "plan": digest,
                    },
                    "retirement_operation": provider_operation_key,
                    "retirement_manifest": manifest,
                },
            }
        )
        from control_plane.production_recipient_retirement_names import provider_name_guard

        with provider_name_guard(self.store, manifest["project"]):
            self.references(record, manifest)
            self.store.write_production_recipient(
                expected=record.model_dump(mode="json"),
                replacement=retiring.model_dump(mode="json"),
                reservation=lease.evidence_reservation(),
                expected_authority=authority,
            )
        return retiring

    def require_compose_absent(self, manifest: dict[str, Any]) -> dict[str, Any]:
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        try:
            api.fetch_dokploy_target_payload(
                host=host, token=token, target_type="compose", target_id=manifest["target"]
            )
        except api.DokployRequestFailed as error:
            if error.status_code != 404:
                raise
        else:
            raise ValueError("The retained provider definition is still present.")
        observed = inspect_retirement_resources(self.root, manifest)
        if (
            not observed["project_absent"]
            or observed["engine_identity"] != manifest["engine_identity"]
        ):
            raise ValueError("The old project still has host effects or changed engine identity.")
        return observed
