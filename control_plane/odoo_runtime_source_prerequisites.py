"""Fixed testing read of installed importer options; no source connection or grant verdict."""

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.runtime_identity import (
    RUNTIME_IDENTITY_ENV_KEY,
    parse_runtime_identity_payload,
)
from control_plane.dokploy.compose import ODOO_WEB_CONFIG_PATH
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.odoo_import_diagnostics import DiagnosticStore, SOURCE_PREFIXES, import_authority
from control_plane.odoo_import_overrides import import_runtime_authority
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.odoo_secret_references import (
    read_secret_reference_authority,
    selected_config_parameter_bindings,
)
from control_plane.odoo_source_prerequisite_programs import CONTAINER_PROGRAM, HOST_PROGRAM
from control_plane.release_compatibility import _files
from control_plane.secrets import RUNTIME_ENVIRONMENT_SECRET_INTEGRATION

SourceName = Literal["cm_data", "repairshopr", "fishbowl"]
Agreement = Literal["match", "mismatch", "unknown"]
Origin = Literal["explicit", "empty", "absent", "unknown"]
TlsConfiguration = Literal[
    "enabled_with_verification", "disabled", "verification_disabled", "unknown"
]
OPTION_FIELDS = ("port", "use_ssl", "ssl_verify", "batch_size")
# Fixed integration adapters, not tenant defaults. Inventory paths are tenant-repository relative.
SOURCE_ADDONS = {
    "cm_data": ("cm_data_import", "cm_data_importer", "cm_data_client"),
    "repairshopr": ("repairshopr_import", "repairshopr_importer", "repairshopr_sync_client"),
    "fishbowl": ("fishbowl_import", "fishbowl_importer", "fishbowl_client"),
}
_READ_SLOTS = threading.BoundedSemaphore(4)


class PrerequisiteStore(DiagnosticStore, Protocol):
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_odoo_instance_override_record(
        self, *, context_name: str, instance_name: str
    ) -> OdooInstanceOverrideRecord: ...


class OptionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field: Literal["port", "use_ssl", "ssl_verify", "batch_size"]
    authority: Origin
    delivered: Origin
    applied: Origin
    authority_delivered: Agreement
    authority_applied: Agreement
    delivered_applied: Agreement


class SourcePrerequisites(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: SourceName
    producer: Literal["verified", "unknown"] = "unknown"
    addon_state: Literal["installed", "unknown"] = "unknown"
    options: tuple[OptionEvidence, ...] = Field(default=(), max_length=4)
    authority_tls_configuration: TlsConfiguration = "unknown"
    delivered_tls_configuration: TlsConfiguration = "unknown"
    applied_tls_configuration: TlsConfiguration = "unknown"
    delivered_settings: Literal["valid", "unknown"] = "unknown"
    applied_settings: Literal["valid", "unknown"] = "unknown"
    applied_delivered_password: Agreement = "unknown"
    binding: Literal["selected", "missing_or_unsafe"] = "missing_or_unsafe"
    secret_reference: Agreement = "unknown"
    selected_version_metadata: Literal["current", "unknown"] = "unknown"
    managed_password_value_agreement: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def unique_options(self) -> "SourcePrerequisites":
        if len({item.field for item in self.options}) != len(self.options):
            raise ValueError("Duplicate option evidence")
        return self


class SourcePrerequisiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    runtime: Literal["verified", "unknown"] = "unknown"
    sources: tuple[SourcePrerequisites, ...] = Field(min_length=3, max_length=3)
    applied: Literal[False] = False
    values_redacted: Literal[True] = True

    @model_validator(mode="after")
    def fixed_sources(self) -> "SourcePrerequisiteResult":
        if {item.source for item in self.sources} != set(SOURCE_ADDONS):
            raise ValueError("Invalid prerequisite sources")
        return self


def unknown_result() -> SourcePrerequisiteResult:
    return SourcePrerequisiteResult(
        sources=tuple(SourcePrerequisites(source=cast(SourceName, name)) for name in SOURCE_ADDONS)
    )


def producer_files(
    manifest: ArtifactIdentityManifest, connection: reads.OdooRuntimeConnection, repository: str
) -> dict[str, dict[str, str]]:
    identity = connection.selection.identity
    if (
        manifest.artifact_id != identity.artifact_id
        or manifest.source_commit != identity.source_git_ref
        or manifest.source_build is None
        or manifest.source_build.repository != repository
        or f"{manifest.image.repository}@{manifest.image.digest}" != identity.image_reference
    ):
        raise ValueError("Unverified prerequisite artifact")
    inventory = _files(manifest)
    result = {}
    for name, (addon, model, client) in SOURCE_ADDONS.items():
        paths = (
            "__init__.py",
            "__manifest__.py",
            "models/__init__.py",
            f"models/{model}.py",
            "services/__init__.py",
            f"services/{client}.py",
            "services/connection_settings.py",
        )
        try:
            result[name] = {
                f"/opt/project/addons/{addon}/{path}": inventory[
                    ("tenant", f"addons/{addon}/{path}")
                ].sha256
                for path in paths
            }
        except KeyError:
            result[name] = {}
    return result


@dataclass(frozen=True, repr=False)
class _Authority:
    snapshot: str
    options: dict[str, dict[str, dict[str, object]]]
    metadata: dict[str, dict[str, str]]


def _authority(store: PrerequisiteStore, product: str) -> _Authority:
    authority = import_authority(store, product)
    lane = next(lane for lane in authority.profile.lanes if lane.instance == "testing")
    values, expectation = import_runtime_authority(
        record_store=store, context=lane.context, instance=lane.instance
    )
    target = store.read_dokploy_target_record(
        context_name=lane.context, instance_name=lane.instance
    )
    bindings = store.list_secret_bindings(
        integration=RUNTIME_ENVIRONMENT_SECRET_INTEGRATION, context_name=lane.context
    )
    excluded = set(target.env)
    for record in expectation.records:
        excluded.update(record.retired_provider_keys)
    excluded.update(
        binding.binding_key
        for binding in bindings
        if binding.context == lane.context and binding.instance in {"", lane.instance}
    )
    options: dict[str, dict[str, dict[str, object]]] = {}
    metadata: dict[str, dict[str, str]] = {}
    try:
        overrides = store.read_odoo_instance_override_record(
            context_name=lane.context, instance_name=lane.instance
        )
        references = read_secret_reference_authority(store, overrides)
    except FileNotFoundError:
        overrides, references = None, None
    for name, prefix in SOURCE_PREFIXES.items():
        options[name] = {}
        for field in OPTION_FIELDS:
            key = prefix + "." + field
            runtime_key = config_parameter_runtime_key(key)
            # Excluded authority is unknown, never an omitted option/default.
            options[name][key] = {
                "known": runtime_key not in excluded,
                "present": runtime_key in values,
                "value": values.get(runtime_key, ""),
            }
        selected = authority.sources.get(name)
        reference: Agreement = "unknown"
        if selected is not None and overrides is not None and references is not None:
            try:
                chosen = selected_config_parameter_bindings(
                    overrides, references, (prefix + ".password",)
                )
                parameter = next(
                    item for item in overrides.config_parameters if item.key == prefix + ".password"
                )
                reference = (
                    "match"
                    if (
                        chosen[prefix + ".password"] == selected.binding
                        and parameter.value.secret_binding_id == selected.binding.binding_id
                    )
                    else "mismatch"
                )
            except ValueError:
                pass
        metadata[name] = {
            "binding": "selected" if selected else "missing_or_unsafe",
            "secret_reference": reference,
            "selected_version_metadata": "current" if selected else "unknown",
        }
    snapshot = json.dumps(
        {
            "base": authority.private_snapshot,
            "options": options,
            "overrides": overrides.model_dump(mode="json") if overrides else None,
            "references": {
                "metadata": references.metadata.model_dump(mode="json"),
                "runtime": references.runtime.model_dump(mode="json"),
            }
            if references
            else None,
            "excluded": sorted(excluded),
        },
        sort_keys=True,
    )
    return _Authority(snapshot, options, metadata)


def runtime_prerequisites(
    connection: reads.OdooRuntimeConnection,
    *,
    files: dict[str, dict[str, str]],
    authority: _Authority,
    observed_identity: str,
) -> SourcePrerequisiteResult:
    reads.require_runtime_identity(
        connection.selection.identity, parse_runtime_identity_payload(observed_identity)
    )
    # The remote transport never receives a managed secret value or settings object.
    program = CONTAINER_PROGRAM.replace("__CONFIG_PATH__", repr(ODOO_WEB_CONFIG_PATH)).replace(
        "__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)
    )
    frames: list[dict[str, object]] = []
    _read_remote(
        host=connection.host,
        token=connection.token,
        server_id=connection.server_id,
        program=HOST_PROGRAM.replace("__IDENTITY_KEY__", repr(RUNTIME_IDENTITY_ENV_KEY)),
        payload={
            "project": connection.app_name,
            "container": connection.evidence.container_id,
            "identity": json.loads(observed_identity),
            "files": files,
            "addons": {name: value[0] for name, value in SOURCE_ADDONS.items()},
            "prefixes": SOURCE_PREFIXES,
            "option_fields": OPTION_FIELDS,
            "authority": authority.options,
            "metadata": authority.metadata,
            "runtime_env_prefix": config_parameter_runtime_key(""),
            "password_env_keys": {
                name: config_parameter_secret_env_key(prefix + ".password")
                for name, prefix in SOURCE_PREFIXES.items()
            },
            "container_program": program,
        },
        receive=frames.append,
        timeout_seconds=60,
    )
    if len(frames) != 1 or set(frames[0]) != {"type", "result"} or frames[0]["type"] != "complete":
        raise ValueError("Invalid prerequisite frame")
    # JSON validation permits wire arrays for tuple fields, but preserves strict scalars.
    if len(json.dumps(frames[0])) > 16384:
        raise ValueError("Oversized prerequisite frame")
    result = SourcePrerequisiteResult.model_validate_json(json.dumps(frames[0]["result"]))
    return result


def diagnose_source_prerequisites(
    *, store: PrerequisiteStore, product: str, control_plane_root: Path, database_url: str | None
) -> SourcePrerequisiteResult:
    if not _READ_SLOTS.acquire(blocking=False):
        return unknown_result()
    try:
        authority = _authority(store, product)
        profile = store.read_product_profile_record(product)
        selection = reads.select_stable_runtime(store, profile, "testing")
        manifest = store.read_artifact_manifest(selection.identity.artifact_id)
        connection = reads.connect_runtime(
            store=store,
            selection=selection,
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        files = producer_files(manifest, connection, profile.repository)
        before = reads.inspect_runtime_container(
            connection.host,
            connection.token,
            connection.server_id,
            connection.evidence.container_id,
            connection.app_name,
            selection.identity,
        )
        result = runtime_prerequisites(
            connection,
            files=files,
            authority=authority,
            observed_identity=before[RUNTIME_IDENTITY_ENV_KEY],
        )
        reads.assert_selection_current(store, selection)
        after = reads.inspect_runtime_container(
            connection.host,
            connection.token,
            connection.server_id,
            connection.evidence.container_id,
            connection.app_name,
            selection.identity,
        )
        if (
            before != after
            or _authority(store, product).snapshot != authority.snapshot
            or store.read_artifact_manifest(selection.identity.artifact_id) != manifest
            or store.read_product_profile_record(product) != profile
        ):
            return unknown_result()
        return result
    except Exception:
        # Nothing from provider, producer or configuration exceptions crosses this boundary.
        return unknown_result()
    finally:
        _READ_SLOTS.release()
