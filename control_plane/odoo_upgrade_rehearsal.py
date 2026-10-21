"""Standalone Odoo rehearsals; canonical lanes are read-only inputs."""

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import gzip
import json
from pathlib import Path
import re
from typing import cast

import click

from control_plane import secrets
from control_plane._odoo_rehearsal_business_validation import (
    business_entrypoint_check_script,
    business_entrypoint_schedule,
    final_business_database,
    BUSINESS_VALIDATION_COMMAND_TIMEOUT_SECONDS,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_stable_target_replacement import artifact_odoo_version
from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_ROUTE,
    OdooUpgradeRehearsalRecord,
    OdooUpgradeRehearsalRequest,
    RehearsalPrerequisite,
)
from control_plane.dokploy import api, source
from control_plane.dokploy.network_capacity import (
    BridgeNetworkCapacityRefused,
    require_bridge_network_capacity,
)
from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
from control_plane.odoo_testing_restore import (
    encoded_restore_source,
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.odoo_testing_resources import OdooRehearsalResources
from control_plane.provider_operations import (
    ProviderObservation,
    ProviderOperationLease,
    ProviderMutationOutcome,
)
from control_plane.contracts.odoo_upgrade_rehearsal import RehearsalHostResult

REHEARSAL_HOST_FILENAME = "<rehearsal>"

REHEARSAL_SHAPE_KEYS = frozenset(
    {
        "ODOO_WORKERS",
        "ODOO_DB_MAXCONN",
        "ODOO_LIMIT_TIME_CPU",
        "ODOO_LIMIT_TIME_REAL",
        "ODOO_LIMIT_TIME_REAL_CRON",
        "ODOO_LIMIT_MEMORY_SOFT",
        "ODOO_LIMIT_MEMORY_HARD",
        "POSTGRES_ULIMIT_NOFILE_SOFT",
        "POSTGRES_ULIMIT_NOFILE_HARD",
        "POSTGRES_MAX_CONNECTIONS",
        "POSTGRES_MAX_FILES_PER_PROCESS",
        "POSTGRES_SHARED_BUFFERS",
        "POSTGRES_EFFECTIVE_CACHE_SIZE",
        "POSTGRES_WORK_MEM",
        "POSTGRES_MAINTENANCE_WORK_MEM",
        "POSTGRES_MAX_WAL_SIZE",
        "POSTGRES_MIN_WAL_SIZE",
        "POSTGRES_CHECKPOINT_TIMEOUT",
        "POSTGRES_RANDOM_PAGE_COST",
        "POSTGRES_EFFECTIVE_IO_CONCURRENCY",
    }
)


class RehearsalRefused(ValueError):
    def __init__(
        self, code: RehearsalPrerequisite, *, prerequisite: RehearsalPrerequisite | None = None
    ) -> None:
        self.code = code
        self.prerequisite = prerequisite
        super().__init__(code.value)


class RehearsalPreparationReadRefused(Exception):
    """A rejection cause exposed by the runner only after reservation release.

    Never raise this directly: carry it inside ProviderMutationRejectedError.
    If effects or unresolved custody prevent release, the runner keeps it held
    and the route cannot emit the release marker.
    """

    def __init__(self, error: api.DokployRequestFailed | NativeSourceAuthorizationDenied) -> None:
        self.status_code = (
            403
            if isinstance(error, NativeSourceAuthorizationDenied) or error.status_code in {401, 403}
            else 503
        )
        self.code = (
            "authorization_denied" if self.status_code == 403 else "rehearsal_provider_unavailable"
        )
        super().__init__("The rehearsal provider read was refused before any new effect.")


def _artifact(store: PostgresRecordStore, artifact_id: str) -> ArtifactIdentityManifest:
    try:
        manifest = store.read_artifact_manifest(artifact_id)
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", manifest.image.digest):
            raise ValueError("Artifact is not pinned.")
        if (
            not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", artifact_odoo_version(manifest))
            or not manifest.odoo_install_modules
        ):
            raise ValueError("Artifact lacks exact version and module intent.")
        return manifest
    except (ValueError, FileNotFoundError):
        raise RehearsalRefused(RehearsalPrerequisite.ARTIFACT_UNAVAILABLE) from None


def _source_values(
    store: PostgresRecordStore, product: str, context: str
) -> tuple[dict[str, str], dict[str, str]]:
    try:
        profile = store.read_product_profile_record(product)
        if profile.driver_id != "odoo" or not profile.is_active:
            raise ValueError("An active Odoo product is required.")
        if not any((lane.context, lane.instance) == (context, "testing") for lane in profile.lanes):
            raise ValueError("Source wiring must name the owned testing reader.")
        values: dict[str, str] = {}
        for scope in ("context", "instance"):
            for runtime in store.list_runtime_environment_records(context_name=context):
                if runtime.scope == scope and runtime.instance == (
                    "testing" if scope == "instance" else ""
                ):
                    values.update({key: str(value) for key, value in runtime.env.items()})
        target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
        values.update({key: str(value) for key, value in target.env.items()})
        copy_source = json.loads(base64.b64decode(encoded_restore_source(values)))
        # SSH material remains on its existing host. It is never delivered to a container.
        for key in ("DATA_WORKFLOW_SSH_DIR", "DATA_WORKFLOW_SSH_KEY"):
            if values.get(key):
                copy_source[key] = values[key]
        for key in (
            "OPENUPGRADE_ENABLED",
            "OPENUPGRADE_TARGET_VERSION",
            "OPENUPGRADE_SCRIPTS_PATH",
            "OPENUPGRADE_SKIP_UPDATE_ADDONS",
        ):
            if key in values:
                copy_source[key] = values[key]
        if copy_source.get("OPENUPGRADE_ENABLED", "").lower() not in {
            "true",
            "1",
        } or not copy_source.get("OPENUPGRADE_SCRIPTS_PATH"):
            raise ValueError("Recorded OpenUpgrade intent is unavailable.")
        production = [lane for lane in profile.lanes if lane.instance == "prod"]
        if len(production) != 1:
            raise ValueError("A unique production shape is required.")
        prod = production[0]
        from control_plane.contracts.product_profile_record import (
            is_exclusive_product_context,
            product_context_owner_map,
        )

        owners = product_context_owner_map(store.list_product_profile_records())
        if not all(
            is_exclusive_product_context(context=selected, product=product, owners=owners)
            for selected in (context, prod.context)
        ):
            raise ValueError("The source and production shape must have unambiguous ownership.")
        shape: dict[str, str] = {}
        for scope in ("context", "instance"):
            for runtime in store.list_runtime_environment_records(context_name=prod.context):
                if runtime.scope == scope and runtime.instance == (
                    "prod" if scope == "instance" else ""
                ):
                    shape.update({key: str(value) for key, value in runtime.env.items()})
        shape.update(
            store.read_dokploy_target_record(context_name=prod.context, instance_name="prod").env
        )
        # Only sizing settings are reused. Identity, credentials, integrations,
        # mounts and routing are derived for the rehearsal independently.
        shape = {key: value for key, value in shape.items() if key in REHEARSAL_SHAPE_KEYS}
        if any(
            not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[A-Za-z]*", value) for value in shape.values()
        ):
            raise ValueError("Production sizing contains an unsupported value.")
        return copy_source, shape
    except (ValueError, FileNotFoundError, StopIteration):
        raise RehearsalRefused(RehearsalPrerequisite.SOURCE_UNAVAILABLE) from None


def rehearsal_credentials(store: PostgresRecordStore, rehearsal_id: str) -> dict[str, str]:
    """Use only pre-existing purpose-local bindings; never generate or copy keys."""
    values = secrets.resolve_secret_values_for_integration_from_store(
        record_store=store,
        integration="runtime_environment",
        context_name=rehearsal_id,
        instance_name="rehearsal",
        scopes=frozenset({"context_instance"}),
    )
    if set(values) != FRESH_TESTING_LOCAL_CREDENTIAL_KEYS or any(
        not value for value in values.values()
    ):
        raise RehearsalRefused(RehearsalPrerequisite.CREDENTIALS_UNAVAILABLE)
    # Scope/name alone is not isolation evidence. Compare opaque values against
    # every managed production value inside the service.
    for profile in store.list_product_profile_records():
        for lane in profile.lanes:
            if lane.instance != "prod":
                continue
            live = secrets.resolve_secret_values_for_integration_from_store(
                record_store=store,
                integration="runtime_environment",
                context_name=lane.context,
                instance_name="prod",
                scopes=frozenset({"context", "context_instance"}),
            )
            if any(
                hmac.compare_digest(value.encode(), other.encode())
                for value in values.values()
                for other in live.values()
                if other
            ):
                raise RehearsalRefused(RehearsalPrerequisite.CREDENTIALS_UNAVAILABLE)
    return values


def rehearsal_credential_bindings(store: PostgresRecordStore, rehearsal_id: str) -> str:
    bindings = store.list_secret_bindings(
        integration="runtime_environment",
        context_name=rehearsal_id,
        instance_name="rehearsal",
        limit=None,
    )
    return canonical_json_sha256(
        sorted(
            (
                binding.binding_id,
                binding.secret_id,
                store.read_secret_record(binding.secret_id).current_version_id,
            )
            for binding in bindings
        )
    )


def rehearsal_inputs(
    root: Path,
    store: PostgresRecordStore,
    product: str,
    request: OdooUpgradeRehearsalRequest,
    principal: str,
) -> tuple[OdooUpgradeRehearsalRecord, dict[str, str], dict[str, str]]:
    previous = store.read_odoo_upgrade_rehearsal(request.rehearsal_id)
    if previous is not None and request.action in {
        "simulate-final",
        "validate-business",
        "verify",
        "retire",
    }:
        if (
            previous.product != product
            or previous.principal != principal
            or previous.intent_sha256 != canonical_json_sha256(request.intent())
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        if (
            request.action == "verify"
            and previous.business_validations
            and not previous.business_validations[-1].passed
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        if (
            request.action == "simulate-final"
            and previous.credential_bindings_sha256
            != rehearsal_credential_bindings(store, previous.rehearsal_id)
        ):
            raise RehearsalRefused(RehearsalPrerequisite.CREDENTIALS_UNAVAILABLE)
        return previous, previous.upgrade_environment, previous.shape_environment
    copy_source, shape = _source_values(store, product, request.source_context)
    new = _artifact(store, request.target_artifact_id)
    supported_sources = new.build_flags.values.get("sql_copy_source_versions", "").split(",")
    if (
        int(request.source_odoo_version.split(".")[0])
        >= int(artifact_odoo_version(new).split(".")[0])
        or copy_source.get("OPENUPGRADE_TARGET_VERSION", "").split(".")[0]
        != artifact_odoo_version(new).split(".")[0]
        or request.source_odoo_version.split(".")[0]
        not in {version.strip().split(".")[0] for version in supported_sources if version.strip()}
    ):
        raise RehearsalRefused(RehearsalPrerequisite.ARTIFACT_UNAVAILABLE)
    profile = store.read_product_profile_record(product)
    if any(
        manifest.source_build is None or manifest.source_build.repository != profile.repository
        for manifest in (new,)
    ):
        raise RehearsalRefused(RehearsalPrerequisite.ARTIFACT_UNAVAILABLE)
    identity = store.read_dokploy_target_id_record(
        context_name=request.source_context, instance_name="testing"
    )
    try:
        host, token = source.read_dokploy_config(control_plane_root=root)
    except (ValueError, OSError, click.ClickException):
        raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE) from None
    placement = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=identity.target_id
    )
    server = str(placement.get("serverId") or "")
    if not server or server == "local":
        raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
    now = datetime.now(timezone.utc)
    record = OdooUpgradeRehearsalRecord(
        rehearsal_id=request.rehearsal_id,
        product=product,
        principal=principal,
        intent_sha256=canonical_json_sha256(request.intent()),
        source_binding_sha256=canonical_json_sha256(copy_source),
        shape_sha256=canonical_json_sha256(shape),
        shape_environment=shape,
        upgrade_environment={
            key: value for key, value in copy_source.items() if key.startswith("OPENUPGRADE_")
        },
        source_odoo_version=request.source_odoo_version,
        target_artifact=new,
        server_id=server,
        storage=request.storage,
        created_at=now,
        updated_at=now,
    )
    previous = store.read_odoo_upgrade_rehearsal(request.rehearsal_id)
    if previous is not None:
        if previous.model_dump(
            exclude={
                "project_id",
                "environment_id",
                "compose_id",
                "state",
                "copies",
                "elapsed_seconds",
                "created_at",
                "updated_at",
                "credential_bindings_sha256",
            }
        ) != record.model_dump(
            exclude={
                "project_id",
                "environment_id",
                "compose_id",
                "state",
                "copies",
                "elapsed_seconds",
                "created_at",
                "updated_at",
                "credential_bindings_sha256",
            }
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        record = previous
        if (
            record.credential_bindings_sha256
            and record.credential_bindings_sha256
            != rehearsal_credential_bindings(store, record.rehearsal_id)
        ):
            raise RehearsalRefused(RehearsalPrerequisite.CREDENTIALS_UNAVAILABLE)
    return record, copy_source, shape


def require_rehearsal_transition(record: OdooUpgradeRehearsalRecord, action: str) -> None:
    allowed = {
        "prepare": {"planned"},
        "execute": {"prepared"},
        "simulate-final": {"upgraded"},
        "validate-business": {"switched"},
        "verify": {"switched"},
        "retire": {"verified"},
    }
    if record.state not in allowed.get(action, set()):
        raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)


def require_unaliased_rehearsal(
    store: PostgresRecordStore, record: OdooUpgradeRehearsalRecord
) -> None:
    if any(
        item.target_id == record.compose_id for item in store.list_dokploy_target_id_records()
    ) or any(
        item.target_id == record.compose_id
        and (item.context, item.instance) != (record.rehearsal_id, "rehearsal")
        for item in store.list_physical_provider_target_records()
    ):
        raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
    if record.compose_id:
        owned = store.read_provider_target_record(
            context_name=record.rehearsal_id, instance_name="rehearsal"
        )
        if (
            owned.target_id != record.compose_id
            or owned.source_label != "service:odoo-upgrade-rehearsal"
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
    if any(
        lane.context == record.rehearsal_id
        for profile in store.list_product_profile_records()
        for lane in profile.lanes
    ):
        raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)


def rehearsal_resource(record: OdooUpgradeRehearsalRecord) -> OdooRehearsalResources:
    from control_plane.odoo_testing_resources import OdooRehearsalResources

    return OdooRehearsalResources(
        product=record.product,
        context=record.rehearsal_id,
        operation_id=record.rehearsal_id,
        target_id=record.compose_id,
        namespace=record.rehearsal_id,
    )


def business_validation_identity(
    record: OdooUpgradeRehearsalRecord, operation_key: str
) -> dict[str, str]:
    return {
        "product": record.product,
        "rehearsal_id": record.rehearsal_id,
        "artifact_id": record.target_artifact.artifact_id,
        "source_commit": record.target_artifact.source_commit,
        "database": final_business_database(record.rehearsal_id),
        "attempt_sha256": canonical_json_sha256({"operation_key": operation_key}),
    }


def rehearsal_host_program() -> str:
    from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
    from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
    from control_plane._odoo_upgrade_rehearsal_program import ODOO_UPGRADE_REHEARSAL_PROGRAM

    from control_plane._odoo_incomplete_rehearsal_retirement import (
        INCOMPLETE_REHEARSAL_RETIREMENT_LIBRARY,
    )

    return (
        INCOMPLETE_REHEARSAL_RETIREMENT_LIBRARY
        + COPY_RUNTIME_INSPECTION_LIBRARY
        + STORAGE_MAINTENANCE_LIBRARY
        + ODOO_UPGRADE_REHEARSAL_PROGRAM
    )


def rehearsal_host_spec(
    record: OdooUpgradeRehearsalRecord,
    action: str,
    mode: str,
    source_values: dict[str, str],
    shape: dict[str, str],
    credentials: dict[str, str],
) -> dict[str, object]:
    import yaml
    from control_plane.dokploy import post_deploy
    from control_plane.dokploy.compose import (
        render_odoo_raw_compose_file,
        ODOO_DATABASE_DATA_DIRECTORY,
    )
    from control_plane.integration_readback import (
        INTEGRATION_FAMILIES,
        IntegrationReadbackPolicy,
        INTEGRATION_READBACK_PROGRAM,
        INTEGRATION_READBACK_PASSED_PATH,
    )
    from control_plane.odoo_testing_restore import rehearsal_copy_runner

    new = record.target_artifact.image.repository + "@" + record.target_artifact.image.digest
    old = new  # The source phase is SQL-only; no source-version registry runs.
    resources = rehearsal_resource(record)
    rendered = {}
    for image in (new,):
        payload = yaml.safe_load(
            render_odoo_raw_compose_file(
                image_reference=image,
                hold_web_until_integration_readback=True,
                publish_host_ports=False,
                mount_workflow_ssh=False,
                isolated_testing=True,
            )
        )
        for service in ("web", "script-runner"):
            payload["services"][service]["environment"].update(
                {
                    "ODOO_MASTER_PASSWORD": "${ODOO_MASTER_PASSWORD:?missing}",
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": "${LAUNCHPLANE_RUNTIME_IDENTITY_JSON:?missing}",
                    "ODOO_VERSION": "${ODOO_VERSION:?missing}",
                }
            )
        rendered[image] = yaml.safe_dump(payload, sort_keys=False)
    environment = {
        **shape,
        **credentials,
        **resources.volumes,
        "PLATFORM_CONTEXT": record.rehearsal_id,
        "PLATFORM_INSTANCE": "rehearsal",
        "ODOO_PROJECT_NAME": record.rehearsal_id,
        "ODOO_STACK_NAME": record.rehearsal_id,
        "ODOO_MAX_CRON_THREADS": "0",
        "ODOO_DB_USER": "odoo",
        "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": "",
    }
    host_record = record.model_dump(mode="json", exclude={"target_artifact"})
    host_record["target_artifact"] = {
        "artifact_id": record.target_artifact.artifact_id,
        "source_commit": record.target_artifact.source_commit,
    }
    return {
        "record": host_record,
        "action": action,
        "mode": mode,
        "capture_comparison_timeout_seconds": (
            post_deploy.DEFAULT_ODOO_UPSTREAM_RESTORE_EXECUTION_TIMEOUT_SECONDS * 3 // 4
        ),
        "source": source_values,
        "resources_binding_sha256": resources.binding_sha256,
        "compose": rendered,
        "environment": environment,
        "source_image": old,
        "target_image": new,
        "versions": {
            new: artifact_odoo_version(record.target_artifact),
        },
        "artifacts": {
            new: record.target_artifact.artifact_id,
        },
        "refs": {
            new: record.target_artifact.source_commit,
        },
        "workflow": {
            **{
                key: value for key, value in source_values.items() if key.startswith("OPENUPGRADE_")
            },
            "ODOO_UPDATE_MODULES": ",".join(record.target_artifact.odoo_install_modules),
        },
        "readback_program": INTEGRATION_READBACK_PROGRAM,
        "readback_spec": IntegrationReadbackPolicy(
            families=INTEGRATION_FAMILIES,
            allowed_integrations=(),
            protected_shopify_store_handles=(),
        ).encoded_spec(),
        "web_gate_path": INTEGRATION_READBACK_PASSED_PATH,
        "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
        "sanitize_runner": rehearsal_copy_runner(sanitize_source_only=True),
        "upgrade_runner": rehearsal_copy_runner(sanitize_source_only=False),
        "program": rehearsal_host_program(),
    }


@dataclass
class OdooRehearsalAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: OdooUpgradeRehearsalRequest
    principal: str
    trace: str
    observation_timeout_seconds: int | None = None
    _observed_project_id: str = field(default="", init=False)
    _continue_prepare_host: bool = field(default=False, init=False)
    _cooperative_wait: bool = field(default=False, init=False)

    def checkpoint_remote_job(self, schedule_id: str, deployment_id: str) -> None:
        from control_plane.provider_operations import ProviderMutationCheckpoint
        from control_plane.rehearsal_deploy_resume import drain_requested, persist_checkpoint

        if self.request.action == "execute" and drain_requested(self.store):
            raise ProviderMutationCheckpoint(
                lambda reservation: persist_checkpoint(
                    self, reservation, schedule_id, deployment_id
                )
            )

    def can_continue_prepare_host(
        self, record: OdooUpgradeRehearsalRecord, result: dict[str, object], phase: str
    ) -> bool:
        if not (
            self.request.action == "prepare"
            and phase == "prepare"
            and record.state == "planned"
            and record.project_id
            and record.environment_id
            and record.compose_id
            and record.credential_bindings_sha256
            and not record.copies
            and not record.elapsed_seconds
            and result.get("prepare_not_started") is True
            and result.get("completed") is False
            and result.get("state") == "planned"
            and result.get("copies") == []
            and result.get("raw_source_retained") is False
        ):
            return False
        fresh, _, _ = self.inputs()
        return fresh == record

    @staticmethod
    def initial_prepare_binding(record: OdooUpgradeRehearsalRecord) -> str:
        return record.model_copy(
            update={
                "project_id": "",
                "environment_id": "",
                "compose_id": "",
                "credential_bindings_sha256": "",
            }
        ).binding_sha256

    def can_retry_empty_prepare(self, record: OdooUpgradeRehearsalRecord, phase: str) -> bool:
        from control_plane.dokploy import post_deploy
        from control_plane.dokploy.sandbox_placement import created_placement

        if (
            self.request.action != "prepare"
            or phase
            or record.state != "planned"
            or any((record.project_id, record.environment_id, record.compose_id))
            or record.credential_bindings_sha256
            or record.copies
            or record.elapsed_seconds
        ):
            return False
        fresh, _, _ = self.inputs()
        if fresh != record or any(created_placement(self.root, record.rehearsal_id)):
            return False
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        name, app_name = business_entrypoint_schedule(record.rehearsal_id)
        schedule = api.find_matching_dokploy_schedule(
            host=host,
            token=token,
            target_id=record.server_id,
            schedule_type="server",
            schedule_name=name,
            app_name=app_name,
        )
        return not post_deploy._has_running_schedule_deployment(schedule)

    def partial_prepare_project(self, record: OdooUpgradeRehearsalRecord, phase: str) -> str:
        from control_plane.dokploy.sandbox_placement import observe_empty_isolated_project

        if (
            self.request.action != "prepare"
            or record.state != "planned"
            or phase != "project_id"
            or record.environment_id
            or record.compose_id
            or record.copies
            or record.elapsed_seconds
            or not record.credential_bindings_sha256
            or record.credential_bindings_sha256
            != rehearsal_credential_bindings(self.store, record.rehearsal_id)
        ):
            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        fresh, _, _ = self.inputs()
        if fresh != record:
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        identifier = observe_empty_isolated_project(self.root, record.rehearsal_id)
        if record.project_id and record.project_id != identifier:
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        return identifier

    def operation_readback(
        self, record: OdooUpgradeRehearsalRecord, idempotency_key: str
    ) -> dict[str, object]:
        """Read a journal bound to the original request and unchanged partial record."""
        from control_plane.http_routes.mutation_support import request_fingerprint
        from control_plane.provider_operations import build_provider_operation_key

        reservation = self.store.read_idempotency_record(
            scope=record.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=record.product),
            idempotency_key=idempotency_key,
        )
        if not idempotency_key or reservation is None:
            raise RehearsalRefused(RehearsalPrerequisite.OPERATION_UNAVAILABLE)
        original = self.request.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": self.initial_prepare_binding(record)
                if self.request.action == "prepare"
                else self.request.reviewed_plan_sha256
                if self.request.action == "validate-business"
                else record.binding_sha256,
            }
        )
        if (
            reservation.provider_target_key != record.rehearsal_id
            or reservation.reconciliation_key != self.reconciliation_key()
            or reservation.request_fingerprint
            != request_fingerprint(
                {
                    "product": record.product,
                    **original.model_dump(mode="json"),
                }
            )
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        return {
            "operation_state": reservation.state,
            "provider_effect_phase": reservation.provider_effect_phase,
            "provider_effect_started_at": reservation.provider_effect_started_at,
            "_stored_refusal_code": self.stored_refusal_code(reservation.response_payload),
            "_provider_operation_key": build_provider_operation_key(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=idempotency_key,
                request_fingerprint=reservation.request_fingerprint,
                reconciliation_key=reservation.reconciliation_key,
            ),
        }

    @staticmethod
    def stored_refusal_code(payload: dict[str, object]) -> str:
        from control_plane.contracts.odoo_upgrade_rehearsal import REHEARSAL_PUBLIC_FAILURE_MESSAGES

        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) and code in REHEARSAL_PUBLIC_FAILURE_MESSAGES else ""

    def prepare_readback(
        self, record: OdooUpgradeRehearsalRecord, idempotency_key: str
    ) -> dict[str, object]:
        """Read the original journal before a compose/host receipt exists."""
        journal = self.operation_readback(record, idempotency_key)
        journal.pop("_provider_operation_key")
        journal.pop("_stored_refusal_code")
        disposition = "hold_unknown"
        reason = "operation_not_recoverable"
        if journal["operation_state"] == "reconcile_required" and not record.compose_id:
            try:
                if self.can_retry_empty_prepare(record, str(journal["provider_effect_phase"])):
                    reason = "empty_original_placement_observed"
                else:
                    self.partial_prepare_project(record, str(journal["provider_effect_phase"]))
                    reason = "empty_original_project_observed"
                disposition = "continue_original_prepare"
            except api.DokployRequestFailed as error:
                if error.status_code in {401, 403}:
                    raise
                reason = "observation_unavailable"
            except RehearsalRefused as error:
                reason = error.code.value
            except ValueError:
                reason = "placement_not_qualified"
            except (RuntimeError, OSError, click.ClickException):
                reason = "observation_unavailable"
        return {
            "state": record.state,
            "copies": [],
            "completed": False,
            "raw_source_retained": False,
            **journal,
            "recovery_disposition": disposition,
            "recovery_reason": reason,
        }

    def failure_events(
        self, record: OdooUpgradeRehearsalRecord, started_at: str, *, operation_key: str = ""
    ) -> tuple[list[dict[str, object]], bool]:
        """Read event candidates and exact-operation private diagnostic fallbacks."""
        import ast
        from control_plane.dokploy import post_deploy
        from control_plane.contracts.odoo_upgrade_rehearsal import RehearsalChildFailure

        if not started_at:
            raise ValueError("Missing original checkpoint")
        checkpoint_time = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.compose_id
        )
        if payload.get("serverId") != record.server_id or not re.fullmatch(
            re.escape(record.rehearsal_id) + r"(?:-[a-z0-9]{6})?", str(payload.get("appName") or "")
        ):
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        kind, lookup_id, _, _ = post_deploy._resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=record.compose_id,
            compose_name=record.rehearsal_id,
            target_payload=payload,
        )
        schedule = api.find_matching_dokploy_schedule(
            host=host,
            token=token,
            target_id=lookup_id,
            schedule_type=kind,
            schedule_name="Launchplane Odoo upgrade rehearsal",
            app_name=record.rehearsal_id,
        )
        if schedule is None:
            raise ValueError("Missing original schedule")
        deployments = api.extract_deployments(
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/deployment.allByType",
                query={"id": api.schedule_key(schedule), "type": "schedule"},
            )
        )
        functions = {
            node.name
            for node in ast.walk(ast.parse(rehearsal_host_program()))
            if isinstance(node, ast.FunctionDef)
        }
        events: list[dict[str, object]] = []
        complete = True
        candidates = []
        for deployment in deployments:
            created = api._deployment_created_at(deployment)
            if created is None:
                complete = False
            elif (
                created
                >= checkpoint_time.timestamp() - api._DOKPLOY_SCHEDULE_START_CLOCK_SKEW_SECONDS
            ):
                candidates.append((created, deployment))
        if len(candidates) > 32:
            complete = False
        for created, deployment in sorted(candidates, key=lambda item: item[0])[:32]:
            identifier = api.deployment_key(deployment)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
                complete = False
                continue
            log_available = True
            try:
                lines = api.fetch_dokploy_deployment_logs(
                    host=host,
                    token=token,
                    deployment_id=identifier,
                    line_count=100,
                )
            except api.DokployRequestFailed as error:
                if error.status_code in {401, 403}:
                    raise
                lines = ()
                log_available = complete = False
            except (ValueError, OSError, click.ClickException):
                lines = ()
                log_available = complete = False
            log = "\n".join(lines)
            locations = [
                {"function": match[2], "line": int(match[1])}
                for match in re.finditer(
                    r'File "'
                    + re.escape(REHEARSAL_HOST_FILENAME)
                    + r'", line ([0-9]{1,9}), in ([A-Za-z_][A-Za-z0-9_]*)(?:\n|$)',
                    log,
                )
                if match[2] in functions
            ]
            frames = sorted({str(location["function"]) for location in locations})
            status = api.deployment_status(deployment)
            fallback: dict[str, object] = {}
            if operation_key:
                marker = post_deploy.extract_odoo_post_deploy_readback_markers({"logs": log}).get(
                    "rehearsal_child_failure"
                )
                if marker:
                    try:
                        child = RehearsalChildFailure.model_validate_json(bytes.fromhex(marker))
                    except ValueError:
                        pass
                    else:
                        if (
                            child.intent_sha256 == record.intent_sha256
                            and child.record_binding_sha256 == record.binding_sha256
                            and child.attempt_sha256
                            == canonical_json_sha256({"operation_key": operation_key})
                        ):
                            fallback = {
                                "child_failure": child.model_dump(mode="json"),
                                "diagnostics_private": True,
                            }
            events.append(
                {
                    "deployment_id": identifier,
                    "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                    "provider_status": status
                    if status in {"done", "error", "running", "queued", "cancelled"}
                    else "unknown",
                    "log_available": log_available,
                    "traceback_present": "Traceback (most recent call last):" in log,
                    "failure_frames": frames,
                    "failure_locations": locations,
                    **fallback,
                }
            )
        return events, complete

    def native_readback(
        self,
        record: OdooUpgradeRehearsalRecord,
        idempotency_key: str,
        checkpoint: Callable[[], None],
    ) -> dict[str, object]:
        """Assemble independent evidence; no observation grants mutation authority."""
        journal: dict[str, object] = {}
        diagnostics: dict[str, object] = {}
        operation_key = ""
        journal_status = "not_requested"
        if idempotency_key:
            try:
                journal = self.operation_readback(record, idempotency_key)
                operation_key = str(journal.pop("_provider_operation_key"))
                journal_status = "bound"
            except RehearsalRefused as error:
                journal_status = (
                    "missing"
                    if error.code == RehearsalPrerequisite.OPERATION_UNAVAILABLE
                    else "unbound"
                )
            if journal.get("operation_state") in {"running", "reconcile_required"} and journal.get(
                "provider_effect_started_at"
            ):
                try:
                    events, complete = self.failure_events(
                        record,
                        str(journal["provider_effect_started_at"]),
                        operation_key=operation_key,
                    )
                    diagnostics["failure_events_after_checkpoint"] = events
                    if not complete:
                        diagnostics["failure_events_unavailable"] = True
                except api.DokployRequestFailed as error:
                    if error.status_code in {401, 403}:
                        raise
                    diagnostics["failure_events_unavailable"] = True
                except RehearsalRefused:
                    raise
                except (ValueError, OSError, click.ClickException):
                    diagnostics["failure_events_unavailable"] = True
        try:
            observation = self.host(
                record,
                "observe",
                checkpoint,
                operation_key=operation_key,
                inspect_capture=(
                    journal_status == "bound"
                    and journal.get("operation_state") in {"running", "reconcile_required"}
                    and journal.get("provider_effect_phase") == self.request.action
                    and bool(journal.get("provider_effect_started_at"))
                ),
            )
        except api.DokployRequestFailed as error:
            if error.status_code in {401, 403}:
                raise
            observation = {
                "completed": False,
                "host_observation_unavailable": True,
                "host_observation_reason": "provider_transport_unavailable",
            }
        except RehearsalRefused as error:
            if error.code != RehearsalPrerequisite.RECOVERY_REQUIRED:
                raise
            observation = {
                "completed": False,
                "host_observation_unavailable": True,
                "host_observation_reason": error.code.value,
            }
        except OSError:
            observation = {
                "completed": False,
                "host_observation_unavailable": True,
                "host_observation_reason": "host_io_unavailable",
            }
        result = {
            **observation,
            **journal,
            **diagnostics,
            "operation_journal_status": journal_status,
        }
        if (
            result.get("child_failure") is not None
            or result.get("retained_upgrade_logs") is not None
            or any(
                event.get("child_failure") is not None
                for event in cast(
                    list[dict[str, object]], diagnostics.get("failure_events_after_checkpoint", [])
                )
            )
        ):
            result["diagnostics_private"] = True
        else:
            result.pop("diagnostics_private", None)
        stored_refusal = result.pop("_stored_refusal_code", "")
        if observation.get("completed") is not True:
            terminal = (
                journal.get("operation_state") == "completed"
                and bool(stored_refusal)
                and stored_refusal == observation.get("refusal_code")
            )
            reason = (
                str(stored_refusal)
                if terminal
                else "host_observation_unavailable"
                if observation.get("host_observation_unavailable")
                else "operation_journal_" + journal_status
                if journal_status != "bound"
                else "no_effect_checkpoint"
                if not journal["provider_effect_phase"]
                and not journal["provider_effect_started_at"]
                else "incomplete_original_effect"
            )
            result.update(
                recovery_disposition="completed_refusal" if terminal else "hold_unknown",
                recovery_reason=reason,
                original_key_required=not terminal,
            )
        if (
            self.request.action == "prepare"
            and record.state == "planned"
            and journal.get("operation_state") == "reconcile_required"
            and not observation.get("host_observation_unavailable")
        ):
            try:
                eligible = self.can_continue_prepare_host(
                    record, observation, str(journal["provider_effect_phase"])
                )
            except RehearsalRefused as error:
                if error.code not in {
                    RehearsalPrerequisite.SOURCE_UNAVAILABLE,
                    RehearsalPrerequisite.ARTIFACT_UNAVAILABLE,
                    RehearsalPrerequisite.CREDENTIALS_UNAVAILABLE,
                }:
                    raise
                result["recovery_reason"] = error.code.value
            else:
                if eligible:
                    result.update(
                        recovery_disposition="continue_original_prepare",
                        recovery_reason="prepare_host_not_started",
                    )
        if observation.get("completed") is True and journal_status == "unbound":
            result.pop("operation_journal_status")
        return result

    def target_key(self) -> str:
        return self.request.rehearsal_id

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {"rehearsal": self.request.rehearsal_id, "action": self.request.action}
        )

    def inputs(self) -> tuple[OdooUpgradeRehearsalRecord, dict[str, str], dict[str, str]]:
        return rehearsal_inputs(self.root, self.store, self.product, self.request, self.principal)

    def reviewed_binding(self, record: OdooUpgradeRehearsalRecord) -> str:
        return record.binding_sha256

    def run_script(
        self, record: OdooUpgradeRehearsalRecord, script: str, checkpoint: Callable[[], None]
    ) -> dict[str, str]:
        from control_plane.dokploy import post_deploy

        api.validate_dokploy_schedule_script({"script": script, "appName": record.rehearsal_id})
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.compose_id
        )
        if payload.get("serverId") != record.server_id or not re.fullmatch(
            re.escape(record.rehearsal_id) + r"(?:-[a-z0-9]{6})?", str(payload.get("appName") or "")
        ):
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        if payload.get("env") not in (None, "") or payload.get("composeFile") not in (None, ""):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        from control_plane.dokploy_target_setup_http import (
            fetch_dokploy_compose_domains_for_target_setup,
        )

        if fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=record.compose_id
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        kind, lookup_id, _project, server = post_deploy._resolve_dokploy_schedule_runtime(
            host=host,
            token=token,
            compose_id=record.compose_id,
            compose_name=record.rehearsal_id,
            target_payload=payload,
        )
        name = "Launchplane Odoo upgrade rehearsal"
        schedule = api.find_matching_dokploy_schedule(
            host=host,
            token=token,
            target_id=lookup_id,
            schedule_type=kind,
            schedule_name=name,
            app_name=record.rehearsal_id,
        )
        if post_deploy._has_running_schedule_deployment(schedule):
            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
        checkpoint()
        return post_deploy.run_manual_data_schedule(
            host=host,
            token=token,
            target_definition=source.DokployTargetDefinition(
                context=record.rehearsal_id,
                instance="rehearsal",
                target_id=record.compose_id,
                target_name=record.rehearsal_id,
            ),
            schedule_lookup_id=lookup_id,
            schedule_type=kind,
            schedule_name=name,
            schedule_app_name=record.rehearsal_id,
            schedule_payload={
                "name": name,
                "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
                "appName": record.rehearsal_id,
                "shellType": "bash",
                "scheduleType": kind,
                "command": "control-plane isolated Odoo upgrade rehearsal",
                "script": script,
                "serverId": server,
                "userId": lookup_id if kind == "dokploy-server" else None,
                "enabled": False,
                "timezone": "UTC",
            },
            resolved_schedule_execution_timeout_seconds=(
                self.observation_timeout_seconds
                or post_deploy.DEFAULT_ODOO_UPSTREAM_RESTORE_EXECUTION_TIMEOUT_SECONDS
            ),
            before_provider_mutation=lambda _phase: checkpoint(),
            on_observation=self.checkpoint_remote_job if self._cooperative_wait else None,
        )

    def check_business_entrypoint(
        self, record: OdooUpgradeRehearsalRecord, operation_key: str, checkpoint: Callable[[], None]
    ) -> None:
        """Check the pinned image on its bound server before allocating a copy."""
        from control_plane.dokploy import post_deploy

        image = record.target_artifact.image.repository + "@" + record.target_artifact.image.digest
        binding = {
            "intent_sha256": record.intent_sha256,
            "record_binding_sha256": record.binding_sha256,
            "attempt_sha256": canonical_json_sha256({"operation_key": operation_key}),
            "image": image,
        }
        script = business_entrypoint_check_script(image, binding)
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        name, app_name = business_entrypoint_schedule(record.rehearsal_id)
        schedule = api.find_matching_dokploy_schedule(
            host=host,
            token=token,
            target_id=record.server_id,
            schedule_type="server",
            schedule_name=name,
            app_name=app_name,
        )
        if post_deploy._has_running_schedule_deployment(schedule):
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)
        evidence = post_deploy.run_manual_data_schedule(
            host=host,
            token=token,
            target_definition=source.DokployTargetDefinition(
                context=record.rehearsal_id, instance="rehearsal"
            ),
            schedule_lookup_id=record.server_id,
            schedule_type="server",
            schedule_name=name,
            schedule_app_name=app_name,
            schedule_payload={
                "name": name,
                "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
                "appName": app_name,
                "shellType": "bash",
                "scheduleType": "server",
                "command": "control-plane isolated candidate capability check",
                "script": script,
                "serverId": record.server_id,
                "enabled": False,
                "timezone": "UTC",
            },
            resolved_schedule_execution_timeout_seconds=BUSINESS_VALIDATION_COMMAND_TIMEOUT_SECONDS
            + 60,
            before_provider_mutation=lambda _phase: checkpoint(),
        )
        try:
            result = json.loads(bytes.fromhex(evidence["rehearsal_result"]))
        except (KeyError, ValueError):
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE) from None
        if (
            not isinstance(result, dict)
            or set(result) != {*binding, "status"}
            or any(result[key] != value for key, value in binding.items())
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        if result["status"] == "entrypoint_unavailable":
            raise RehearsalRefused(RehearsalPrerequisite.ARTIFACT_UNAVAILABLE)
        if result["status"] != "available":
            raise RehearsalRefused(RehearsalPrerequisite.PLACEMENT_UNAVAILABLE)

    def host_script(
        self,
        record: OdooUpgradeRehearsalRecord,
        mode: str,
        *,
        operation_key: str = "",
        inspect_capture: bool = False,
    ) -> str:
        if mode == "observe" or self.request.action in {
            "validate-business",
            "verify",
            "retire",
            "retire-incomplete",
        }:
            source_values: dict[str, str] = {}
            shape: dict[str, str] = {}
            credentials: dict[str, str] = {}
        else:
            _, source_values, shape = self.inputs()
            credentials = rehearsal_credentials(self.store, record.rehearsal_id)
        spec = rehearsal_host_spec(
            record, self.request.action, mode, source_values, shape, credentials
        )
        spec["attempt_sha256"] = canonical_json_sha256({"operation_key": operation_key})
        spec["record_binding_sha256"] = record.binding_sha256
        spec["inspect_capture"] = mode == "observe" and inspect_capture
        if self.request.action == "validate-business":
            spec["business_validation_identity"] = business_validation_identity(
                record, operation_key
            )
        program = str(spec.pop("program"))
        encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
        # Keep both Dokploy's remote shell materializer and Python's argv small.
        # The full reviewed record binding stays above; only the host's projection
        # and reviewed program cross this transport, through Python's stdin.
        program = "import sys\nsys.argv = ['rehearsal', " + repr(encoded) + "]\n" + program
        packed = base64.b64encode(gzip.compress(program.encode(), mtime=0)).decode()
        script = (
            "#!/usr/bin/env bash\nset -euo pipefail\npython3 - <<'LP_REHEARSAL'\n"
            + "import base64, gzip\nexec(compile(gzip.decompress(base64.b64decode("
            + repr(packed)
            + ")), "
            + repr(REHEARSAL_HOST_FILENAME)
            + ", 'exec'), {})"
            + "\nLP_REHEARSAL\n"
        )
        return script

    @staticmethod
    def validate_apply_script(record: OdooUpgradeRehearsalRecord, script: str) -> None:
        from control_plane.provider_operations import ProviderMutationRejectedError

        try:
            api.validate_dokploy_schedule_script({"script": script, "appName": record.rehearsal_id})
        except click.ClickException:
            raise ProviderMutationRejectedError(
                RehearsalRefused(
                    RehearsalPrerequisite.PRECHECK_REFUSED,
                    prerequisite=RehearsalPrerequisite.STATE_UNVERIFIED,
                )
            ) from None

    def host(
        self,
        record: OdooUpgradeRehearsalRecord,
        mode: str,
        checkpoint: Callable[[], None],
        *,
        operation_key: str = "",
        prepared_script: str | None = None,
        inspect_capture: bool = False,
    ) -> dict[str, object]:
        script = (
            prepared_script
            if prepared_script is not None
            else self.host_script(
                record, mode, operation_key=operation_key, inspect_capture=inspect_capture
            )
        )
        evidence = self.run_script(record, script, checkpoint)
        try:
            payload = json.loads(bytes.fromhex(evidence["rehearsal_result"]))
            if mode == "observe" and isinstance(payload, dict) and payload.get("host_busy") is True:
                from control_plane.contracts.odoo_upgrade_rehearsal import RehearsalHostBusy

                busy = RehearsalHostBusy.model_validate(payload)
                if busy.intent_sha256 != record.intent_sha256:
                    raise ValueError("Unbound observation")
                return {
                    "completed": False,
                    "host_observation_unavailable": True,
                    "host_observation_reason": "host_busy",
                }
            result = RehearsalHostResult.model_validate(payload).model_dump(mode="json")
        except (KeyError, ValueError):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED) from None
        if (
            result["intent_sha256"] != record.intent_sha256
            or result["public_route"] is not False
            or result["business_host_frozen"] is not False
            or result["resources_preserved"] is not True
            or (
                result["refusal_code"]
                and (
                    result["attempt_sha256"]
                    != canonical_json_sha256({"operation_key": operation_key})
                    or result["completed"] is not False
                    or result["state"] != record.state
                    or result["copies"] != [copy.model_dump(mode="json") for copy in record.copies]
                )
            )
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        mismatch = result.get("attachment_mismatch")
        if mismatch is not None and (
            not isinstance(mismatch, dict)
            or mode != "observe"
            or not inspect_capture
            or self.request.action != "execute"
            or record.state != "prepared"
            or result["completed"] is not False
            or result["copies"]
            or mismatch["intent_sha256"] != record.intent_sha256
            or mismatch["record_binding_sha256"] != record.binding_sha256
            or mismatch["attempt_sha256"] != canonical_json_sha256({"operation_key": operation_key})
            or result["capture_attachment_validation"] != mismatch["attachment"]["reason"]
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        child = result.get("child_failure")
        if child is not None and (
            not isinstance(child, dict)
            or mode != "observe"
            or not inspect_capture
            or result["completed"] is not False
            or child["intent_sha256"] != record.intent_sha256
            or child["record_binding_sha256"] != record.binding_sha256
            or child["attempt_sha256"] != canonical_json_sha256({"operation_key": operation_key})
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        if result.get("retained_upgrade_logs") is not None and (
            mode != "observe"
            or not inspect_capture
            or self.request.action != "execute"
            or record.state != "prepared"
            or result["completed"] is not False
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        if child is not None or result.get("retained_upgrade_logs") is not None:
            result["diagnostics_private"] = True
        pending = result.get("pending_business_validation")
        if pending is not None:
            if (
                not isinstance(pending, dict)
                or mode != "observe"
                or self.request.action != "validate-business"
                or result["completed"] is not False
                or result["business_validation"] is not None
                or result["business_validation_postcheck_verified"] is not False
                or pending["intent_sha256"] != record.intent_sha256
                or pending["record_binding_sha256"] != record.binding_sha256
                or pending["attempt_sha256"]
                != canonical_json_sha256({"operation_key": operation_key})
            ):
                raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
            result["diagnostics_private"] = True
        if self.request.action == "validate-business" and (
            result.get("completed") is True or pending is not None
        ):
            validation = (
                pending["receipt"]
                if isinstance(pending, dict)
                else result.get("business_validation")
            )
            expected = business_validation_identity(record, operation_key)
            if not isinstance(validation, dict) or any(
                validation[key] != value for key, value in expected.items()
            ):
                raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        return result

    def refusal_response(self, result: dict[str, object]) -> dict[str, object]:
        return {
            "trace_id": self.trace,
            "error": {"code": result["refusal_code"]},
        }

    def publish(
        self, record: OdooUpgradeRehearsalRecord, result: dict[str, object]
    ) -> dict[str, object]:
        allowed = {
            "prepare": "prepared",
            "execute": "upgraded",
            "simulate-final": "switched",
            "validate-business": "switched",
            "verify": "verified",
            "retire": "retired",
        }
        if (
            result.get("completed", True) is not True
            or result["state"] != allowed[self.request.action]
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        validations = list(record.business_validations)
        if self.request.action == "validate-business":
            from control_plane.contracts.odoo_rehearsal_business_validation import (
                BusinessValidationReceipt,
            )

            validation = BusinessValidationReceipt.model_validate(result["business_validation"])
            prior = next(
                (item for item in validations if item.attempt_sha256 == validation.attempt_sha256),
                None,
            )
            if prior is not None and prior != validation:
                raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
            if prior is None:
                validations.append(validation)
        updated = OdooUpgradeRehearsalRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "state": result["state"],
                "copies": result["copies"],
                "elapsed_seconds": result["elapsed_seconds"],
                "updated_at": datetime.now(timezone.utc),
                "business_validations": validations,
            }
        )
        old_copies = {copy.database: copy for copy in record.copies}
        if any(
            old_copies[copy.database] != copy
            for copy in updated.copies
            if copy.database in old_copies
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        if not set(old_copies) <= {copy.database for copy in updated.copies}:
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        if self.request.action == "verify" and record.business_validations:
            final = next(
                (copy for copy in updated.copies if copy.database.endswith("_final_upgrade")), None
            )
            if final is None or final.business_validation_sha256 != canonical_json_sha256(
                [item.model_dump(mode="json") for item in record.business_validations]
            ):
                raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        self.store.compare_and_write_odoo_upgrade_rehearsal(record, updated)
        public = self.public(updated)
        if self.request.action == "validate-business":
            public["business_validation"] = result["business_validation"]
        return public

    @staticmethod
    def public(record: OdooUpgradeRehearsalRecord) -> dict[str, object]:
        return {
            "rehearsal_id": record.rehearsal_id,
            "product": record.product,
            "state": record.state,
            "source_odoo_version": record.source_odoo_version,
            "target_artifact_id": record.target_artifact.artifact_id,
            "source_binding_sha256": record.source_binding_sha256,
            "shape_sha256": record.shape_sha256,
            "copies": [copy.model_dump(mode="json") for copy in record.copies],
            "elapsed_seconds": record.elapsed_seconds,
            **({"resources_preserved": True} if record.incomplete_retirement is None else {}),
            "public_route": False,
            "business_host_frozen": False,
            **(
                {
                    "business_validations": [
                        item.model_dump(mode="json") for item in record.business_validations
                    ]
                }
                if record.business_validations
                else {}
            ),
        }

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        *,
        expected_record: OdooUpgradeRehearsalRecord | None = None,
    ) -> ProviderObservation:
        from control_plane.provider_operations import (
            ProviderObservation,
            provider_operation_response_payload,
        )

        try:
            with self.store.testing_replacement_execution_lock(self.request.rehearsal_id) as check:
                if check is None:
                    return ProviderObservation("unknown")
                record = self.store.read_odoo_upgrade_rehearsal(self.request.rehearsal_id)
                if (
                    record is None
                    or record.product != self.product
                    or (expected_record is not None and record != expected_record)
                ):
                    return ProviderObservation("unknown")
                require_unaliased_rehearsal(self.store, record)
                if self.request.action == "prepare" and not provider_effect_phase:
                    if (
                        record.binding_sha256 != self.request.reviewed_plan_sha256
                        or not self.can_retry_empty_prepare(record, provider_effect_phase)
                    ):
                        return ProviderObservation("unknown")
                    check()
                    return ProviderObservation("absent", retry_safe=True)
                if self.request.action == "prepare" and provider_effect_phase == "project_id":
                    if self.initial_prepare_binding(record) != self.request.reviewed_plan_sha256:
                        return ProviderObservation("unknown")
                    self._observed_project_id = self.partial_prepare_project(
                        record, provider_effect_phase
                    )
                    check()
                    # Only remaining child creation may continue. The project
                    # is recorded later, under the reacquired provider lease.
                    return ProviderObservation(
                        "absent", retry_safe=True, preserve_effect_checkpoint=True
                    )
                result = self.host(record, "observe", check, operation_key=provider_operation_key)
                if self.can_continue_prepare_host(record, result, provider_effect_phase):
                    if self.initial_prepare_binding(record) != self.request.reviewed_plan_sha256:
                        return ProviderObservation("unknown")
                    self.validate_apply_script(
                        record,
                        self.host_script(record, "apply", operation_key=provider_operation_key),
                    )
                    self._continue_prepare_host = True
                    return ProviderObservation(
                        "absent", retry_safe=True, preserve_effect_checkpoint=True
                    )
                if result.get("refusal_code"):
                    return ProviderObservation(
                        "present",
                        response_status_code=409,
                        response_payload=self.refusal_response(result),
                    )
                if result.get("completed") is not True:
                    return ProviderObservation("unknown")
                public = self.publish(record, result)
                return ProviderObservation(
                    "present",
                    response_payload=provider_operation_response_payload(
                        trace_id=self.trace,
                        records={"rehearsal": record.rehearsal_id},
                        result=public,
                    ),
                )
        except Exception:
            return ProviderObservation("unknown")

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        from control_plane.provider_operations import (
            ProviderMutationOutcome,
            ProviderMutationRejectedError,
            provider_operation_response_payload,
        )
        from control_plane.dokploy.sandbox_placement import (
            IsolatedPlacementObservationRequired,
            REHEARSAL_PROJECT_DESCRIPTION,
            create_isolated_placement,
            created_placement,
        )
        from control_plane.testing_local_credentials import prepare_rehearsal_local_credentials

        with self.store.testing_replacement_execution_lock(self.request.rehearsal_id) as check:
            if check is None:
                raise ProviderMutationRejectedError(
                    RehearsalRefused(
                        RehearsalPrerequisite.PRECHECK_REFUSED,
                        prerequisite=RehearsalPrerequisite.RECOVERY_REQUIRED,
                    )
                )
            try:
                record, _, _ = self.inputs()
                require_rehearsal_transition(record, self.request.action)
                require_unaliased_rehearsal(self.store, record)
            except api.DokployRequestFailed as error:
                raise ProviderMutationRejectedError(
                    RehearsalPreparationReadRefused(error)
                ) from None
            except RehearsalRefused as error:
                raise ProviderMutationRejectedError(
                    RehearsalRefused(
                        RehearsalPrerequisite.PRECHECK_REFUSED, prerequisite=error.code
                    )
                ) from None
            recovering_project = bool(self._observed_project_id)
            reviewed_binding = (
                self.initial_prepare_binding(record)
                if recovering_project or self._continue_prepare_host
                else self.reviewed_binding(record)
            )
            if reviewed_binding != self.request.reviewed_plan_sha256:
                raise ProviderMutationRejectedError(
                    RehearsalRefused(
                        RehearsalPrerequisite.PRECHECK_REFUSED,
                        prerequisite=RehearsalPrerequisite.STATE_UNVERIFIED,
                    )
                )

            def checkpoint() -> None:
                lease.assert_current()
                check()
                fresh, _, _ = self.inputs()
                if fresh != record:
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
                require_unaliased_rehearsal(self.store, fresh)

            if self.request.action == "prepare" and not self._continue_prepare_host:
                existing_placement: tuple[str, str, str] = ("", "", "")
                if not recovering_project:
                    try:
                        existing_placement = created_placement(self.root, record.rehearsal_id)
                    except IsolatedPlacementObservationRequired:
                        raise ProviderMutationRejectedError(
                            RehearsalRefused(
                                RehearsalPrerequisite.PRECHECK_REFUSED,
                                prerequisite=RehearsalPrerequisite.RECOVERY_REQUIRED,
                            )
                        ) from None
                    except api.DokployRequestFailed as error:
                        raise ProviderMutationRejectedError(
                            RehearsalPreparationReadRefused(error)
                        ) from None
                    except (ValueError, OSError, click.ClickException):
                        raise ProviderMutationRejectedError(
                            RehearsalRefused(
                                RehearsalPrerequisite.PRECHECK_REFUSED,
                                prerequisite=RehearsalPrerequisite.PLACEMENT_UNAVAILABLE,
                            )
                        ) from None
                if recovering_project:
                    checkpoint()
                    if (
                        self.partial_prepare_project(record, "project_id")
                        != self._observed_project_id
                    ):
                        raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
                    updated = record.model_copy(update={"project_id": self._observed_project_id})
                    self.store.compare_and_write_odoo_upgrade_rehearsal(record, updated)
                    record = updated
                elif any(existing_placement):
                    raise ProviderMutationRejectedError(
                        RehearsalRefused(
                            RehearsalPrerequisite.PRECHECK_REFUSED,
                            prerequisite=RehearsalPrerequisite.RECOVERY_REQUIRED,
                        )
                    )
                elif not any((record.project_id, record.environment_id, record.compose_id)):
                    # Only a genuinely new placement can be refused cleanly.
                    # Partial creation stays on its original recovery protocol.
                    try:
                        host, token = source.read_dokploy_config(control_plane_root=self.root)
                    except (ValueError, OSError, click.ClickException):
                        raise ProviderMutationRejectedError(
                            RehearsalRefused(
                                RehearsalPrerequisite.PRECHECK_REFUSED,
                                prerequisite=RehearsalPrerequisite.PLACEMENT_UNAVAILABLE,
                            )
                        ) from None
                    try:
                        require_bridge_network_capacity(
                            host=host, token=token, server_id=record.server_id, required_networks=1
                        )
                    except NativeSourceAuthorizationDenied as error:
                        raise ProviderMutationRejectedError(
                            RehearsalPreparationReadRefused(error)
                        ) from None
                    except BridgeNetworkCapacityRefused as error:
                        raise ProviderMutationRejectedError(error) from None
                    try:
                        self.check_business_entrypoint(record, provider_operation_key, checkpoint)
                    except api.DokployRequestFailed as error:
                        raise ProviderMutationRejectedError(
                            RehearsalPreparationReadRefused(error)
                        ) from None
                    except (ValueError, OSError, click.ClickException, RehearsalRefused) as error:
                        raise ProviderMutationRejectedError(
                            RehearsalRefused(
                                RehearsalPrerequisite.PRECHECK_REFUSED,
                                prerequisite=error.code
                                if isinstance(error, RehearsalRefused)
                                else RehearsalPrerequisite.PLACEMENT_UNAVAILABLE,
                            )
                        ) from None
                # The built-in local-password writer is scoped to the new record;
                # it creates no key that can reach a live system or person grant.
                prepare_rehearsal_local_credentials(
                    self.store, record.rehearsal_id, actor=self.principal
                )
                updated = record.model_copy(
                    update={
                        "credential_bindings_sha256": rehearsal_credential_bindings(
                            self.store, record.rehearsal_id
                        )
                    }
                )
                self.store.compare_and_write_odoo_upgrade_rehearsal(record, updated)
                record = updated

                def record_identity(field: str, identifier: str) -> None:
                    nonlocal record
                    updated = record.model_copy(update={field: identifier})
                    self.store.compare_and_write_odoo_upgrade_rehearsal(record, updated)
                    record = updated

                try:
                    create_isolated_placement(
                        self.root,
                        namespace=record.rehearsal_id,
                        server_id=record.server_id,
                        description=REHEARSAL_PROJECT_DESCRIPTION,
                        checkpoint=checkpoint,
                        before_create=lease.checkpoint_effect,
                        record_identity=record_identity,
                        recorded_placement=(
                            record.project_id,
                            record.environment_id,
                            record.compose_id,
                        ),
                    )
                except IsolatedPlacementObservationRequired:
                    raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED) from None
                resources = rehearsal_resource(record)
                checkpoint()
                storage_script = resources.script("prepare")
                self.validate_apply_script(record, storage_script)
                lease.checkpoint_effect("independent_storage")
                evidence = self.run_script(record, storage_script, checkpoint)
                if (
                    evidence.get("testing_resources_ok") != "true"
                    or evidence.get("testing_resources_binding_sha256") != resources.binding_sha256
                ):
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
            elif self.request.action == "prepare":
                checkpoint()
                if created_placement(self.root, record.rehearsal_id) != (
                    record.project_id,
                    record.environment_id,
                    record.compose_id,
                ):
                    raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
                resources = rehearsal_resource(record)
                evidence = self.run_script(record, resources.script("readback"), checkpoint)
                if (
                    evidence.get("testing_resources_ok") != "true"
                    or evidence.get("testing_resources_binding_sha256") != resources.binding_sha256
                ):
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
            checkpoint()
            script = self.host_script(record, "apply", operation_key=provider_operation_key)
            self.validate_apply_script(record, script)
            lease.checkpoint_effect(self.request.action)
            self._cooperative_wait = self.request.action == "execute"
            try:
                result = self.host(
                    record,
                    "apply",
                    checkpoint,
                    operation_key=provider_operation_key,
                    prepared_script=script,
                )
            finally:
                self._cooperative_wait = False
            if result.get("refusal_code"):
                return ProviderMutationOutcome(409, self.refusal_response(result))
            public = self.publish(record, result)
            return ProviderMutationOutcome(
                202,
                provider_operation_response_payload(
                    trace_id=self.trace, records={"rehearsal": record.rehearsal_id}, result=public
                ),
            )
