"""Standalone Odoo rehearsals; canonical lanes are read-only inputs."""

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
import re

import click

from control_plane import secrets
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
    def __init__(self, code: RehearsalPrerequisite) -> None:
        self.code = code
        super().__init__(code.value)


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
    if previous is not None and request.action in {"simulate-final", "verify", "retire"}:
        if (
            previous.product != product
            or previous.principal != principal
            or previous.intent_sha256 != canonical_json_sha256(request.intent())
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
    host, token = source.read_dokploy_config(control_plane_root=root)
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


def rehearsal_host_spec(
    record: OdooUpgradeRehearsalRecord,
    action: str,
    mode: str,
    source_values: dict[str, str],
    shape: dict[str, str],
    credentials: dict[str, str],
) -> dict[str, object]:
    import yaml
    from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
    from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
    from control_plane._odoo_upgrade_rehearsal_program import ODOO_UPGRADE_REHEARSAL_PROGRAM
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
    return {
        "record": record.model_dump(mode="json"),
        "action": action,
        "mode": mode,
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
        "program": COPY_RUNTIME_INSPECTION_LIBRARY
        + STORAGE_MAINTENANCE_LIBRARY
        + ODOO_UPGRADE_REHEARSAL_PROGRAM,
    }


@dataclass
class OdooRehearsalAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: OdooUpgradeRehearsalRequest
    principal: str
    trace: str
    _observed_project_id: str = field(default="", init=False)

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

    def prepare_readback(
        self, record: OdooUpgradeRehearsalRecord, idempotency_key: str
    ) -> dict[str, object]:
        """Read the original journal before a compose/host receipt exists."""
        from control_plane.http_routes.mutation_support import request_fingerprint

        reservation = self.store.read_idempotency_record(
            scope=record.principal,
            route_path=ODOO_UPGRADE_REHEARSAL_ROUTE.format(product=record.product),
            idempotency_key=idempotency_key,
        )
        original = self.request.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": self.initial_prepare_binding(record),
            }
        )
        if (
            not idempotency_key
            or reservation is None
            or reservation.provider_target_key != record.rehearsal_id
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
        disposition = "hold_unknown"
        reason = "operation_not_recoverable"
        if reservation.state == "reconcile_required":
            try:
                self.partial_prepare_project(record, reservation.provider_effect_phase)
                disposition = "continue_original_prepare"
                reason = "empty_original_project_observed"
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
            "operation_state": reservation.state,
            "provider_effect_phase": reservation.provider_effect_phase,
            "provider_effect_started_at": reservation.provider_effect_started_at,
            "recovery_disposition": disposition,
            "recovery_reason": reason,
        }

    def target_key(self) -> str:
        return self.request.rehearsal_id

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {"rehearsal": self.request.rehearsal_id, "action": self.request.action}
        )

    def inputs(self) -> tuple[OdooUpgradeRehearsalRecord, dict[str, str], dict[str, str]]:
        return rehearsal_inputs(self.root, self.store, self.product, self.request, self.principal)

    def run_script(
        self, record: OdooUpgradeRehearsalRecord, script: str, checkpoint: Callable[[], None]
    ) -> dict[str, str]:
        from control_plane.dokploy import post_deploy

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
            resolved_schedule_execution_timeout_seconds=post_deploy.DEFAULT_ODOO_UPSTREAM_RESTORE_EXECUTION_TIMEOUT_SECONDS,
            before_provider_mutation=lambda _phase: checkpoint(),
        )

    def host(
        self,
        record: OdooUpgradeRehearsalRecord,
        mode: str,
        checkpoint: Callable[[], None],
        *,
        operation_key: str = "",
    ) -> dict[str, object]:
        if mode == "observe" or self.request.action in {"verify", "retire"}:
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
        program = str(spec.pop("program"))
        encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
        script = (
            "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " <<'LP_REHEARSAL'\n"
            + program
            + "\nLP_REHEARSAL\n"
        )
        evidence = self.run_script(record, script, checkpoint)
        try:
            result = RehearsalHostResult.model_validate_json(
                bytes.fromhex(evidence["rehearsal_result"])
            ).model_dump(mode="json")
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
                    result["attempt_sha256"] != spec["attempt_sha256"]
                    or result["completed"] is not False
                    or result["state"] != record.state
                    or result["copies"] != [copy.model_dump(mode="json") for copy in record.copies]
                )
            )
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
            "verify": "verified",
            "retire": "retired",
        }
        if (
            result.get("completed", True) is not True
            or result["state"] != allowed[self.request.action]
        ):
            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
        updated = OdooUpgradeRehearsalRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "state": result["state"],
                "copies": result["copies"],
                "elapsed_seconds": result["elapsed_seconds"],
                "updated_at": datetime.now(timezone.utc),
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
        self.store.compare_and_write_odoo_upgrade_rehearsal(record, updated)
        return self.public(updated)

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
            "resources_preserved": True,
            "public_route": False,
            "business_host_frozen": False,
        }

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
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
                if record is None or record.product != self.product:
                    return ProviderObservation("unknown")
                require_unaliased_rehearsal(self.store, record)
                if self.request.action == "prepare" and provider_effect_phase == "project_id":
                    if self.initial_prepare_binding(record) != self.request.reviewed_plan_sha256:
                        return ProviderObservation("unknown")
                    self._observed_project_id = self.partial_prepare_project(
                        record, provider_effect_phase
                    )
                    check()
                    # Only remaining child creation may continue. The project
                    # is recorded later, under the reacquired provider lease.
                    return ProviderObservation("absent", retry_safe=True)
                result = self.host(record, "observe", check, operation_key=provider_operation_key)
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
                    RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
                )
            record, _, _ = self.inputs()
            require_rehearsal_transition(record, self.request.action)
            require_unaliased_rehearsal(self.store, record)
            recovering_project = bool(self._observed_project_id)
            reviewed_binding = (
                self.initial_prepare_binding(record)
                if recovering_project
                else record.binding_sha256
            )
            if reviewed_binding != self.request.reviewed_plan_sha256:
                raise ProviderMutationRejectedError(
                    RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
                )

            def checkpoint() -> None:
                lease.assert_current()
                check()
                fresh, _, _ = self.inputs()
                if fresh != record:
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
                require_unaliased_rehearsal(self.store, fresh)

            if self.request.action == "prepare":
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
                elif any(created_placement(self.root, record.rehearsal_id)):
                    raise ProviderMutationRejectedError(
                        RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
                    )
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
                lease.checkpoint_effect("independent_storage")
                evidence = self.run_script(record, resources.script("prepare"), checkpoint)
                if (
                    evidence.get("testing_resources_ok") != "true"
                    or evidence.get("testing_resources_binding_sha256") != resources.binding_sha256
                ):
                    raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
            checkpoint()
            lease.checkpoint_effect(self.request.action)
            result = self.host(record, "apply", checkpoint, operation_key=provider_operation_key)
            if result.get("refusal_code"):
                return ProviderMutationOutcome(409, self.refusal_response(result))
            public = self.publish(record, result)
            return ProviderMutationOutcome(
                202,
                provider_operation_response_payload(
                    trace_id=self.trace, records={"rehearsal": record.rehearsal_id}, result=public
                ),
            )
