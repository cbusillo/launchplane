"""Value-free testing import agreement and managed-source grant observations."""

import hmac
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from pydantic import BaseModel, ConfigDict

from control_plane import odoo_runtime_reads as reads
from control_plane.odoo_diagnostic_identity import DIAGNOSTIC_IDENTITY_UNAVAILABLE
from control_plane.odoo_runtime_source_grants import runtime_grants
from control_plane.odoo_source_probe import GrantStatus
from control_plane.integration_readback import INTEGRATION_FAMILIES
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.odoo_import_overrides import ImportRuntimeStore, import_runtime_authority
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.odoo_product_driver_http import resolve_odoo_product_route
from control_plane.product_secret_copy import ProductSecretCopyStore, product_secret_bindings
from control_plane.secrets import _decrypt_secret_value
from control_plane.runtime_environments import site_delivered_secret_bindings_from_store
from control_plane.runtime_key_safety import ALLOWED_SECRET_CLASSES_BY_ENVIRONMENT
from control_plane.workflows.odoo_preview_runtime import odoo_compose_has_domain
from control_plane.contracts.runtime_identity import runtime_identity_from_health_payload

SOURCE_PREFIXES = {
    name: next(
        key.removesuffix(".password")
        for family in INTEGRATION_FAMILIES
        if family.integration == name
        for key in family.config_parameters
        if key.endswith(".password")
    )
    for name in ("cm_data", "repairshopr", "fishbowl")
}
FIELDS = ("host", "user", "db", "port", "ssl_verify")
Agreement = Literal["match", "mismatch", "missing", "unknown"]
ProbeKey = tuple[str, str, str, str, str]
_PROBE_LOCK = threading.Lock()
_ACTIVE_PROBES: set[ProbeKey] = set()
_FAILED_PROBES: dict[ProbeKey, float] = {}


class DiagnosticStore(
    reads.OdooRuntimeReadStore, ProductSecretCopyStore, ImportRuntimeStore, Protocol
):
    pass


class ParameterAgreement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["host", "user", "db", "port", "ssl_verify", "password"]
    applied_present: bool
    agreement: Agreement


class SourceDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["cm_data", "repairshopr", "fishbowl"]
    binding: Literal["selected", "missing_or_unsafe"] = "missing_or_unsafe"
    parameters: tuple[ParameterAgreement, ...] = ()
    managed_connection_grants: Literal["read_only", "unsafe", "unknown"] = "unknown"
    applied_connection_grants: GrantStatus = "unknown"


class ImportDiagnosticResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: Literal["verified", "unknown"] = "unknown"
    sources: tuple[SourceDiagnostic, ...]
    applied: Literal[False] = False
    values_redacted: Literal[True] = True


def unknown_result() -> ImportDiagnosticResult:
    return ImportDiagnosticResult(
        sources=tuple(
            SourceDiagnostic(source=cast(Literal["cm_data", "repairshopr", "fishbowl"], name))
            for name in SOURCE_PREFIXES
        )
    )


@dataclass(frozen=True, repr=False)
class SourceAuthority:
    binding: SecretBinding
    record: SecretRecord
    version: SecretVersion
    settings: dict[str, str] = field(repr=False)


@dataclass(frozen=True, repr=False)
class ImportAuthority:
    profile: LaunchplaneProductProfileRecord
    private_snapshot: str = field(repr=False)
    sources: dict[str, SourceAuthority] = field(repr=False)
    settings: dict[str, dict[str, str]] = field(repr=False)


def import_authority(store: DiagnosticStore, product: str) -> ImportAuthority:
    profile = resolve_odoo_product_route(record_store=store, product=product, instance="testing")
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    values, expectation = import_runtime_authority(
        record_store=store, context=lane.context, instance=lane.instance
    )
    target = store.read_dokploy_target_record(
        context_name=lane.context, instance_name=lane.instance
    )
    target_id = store.read_dokploy_target_id_record(
        context_name=lane.context, instance_name=lane.instance
    )
    pairs = product_secret_bindings(store, product=product)
    delivered = site_delivered_secret_bindings_from_store(
        record_store=store, context_name=lane.context, instance_name=lane.instance
    )
    source_settings = {
        name: {
            field_name: values.get(config_parameter_runtime_key(prefix + "." + field_name), "")
            for field_name in FIELDS
        }
        for name, prefix in SOURCE_PREFIXES.items()
    }
    sources: dict[str, SourceAuthority] = {}
    selected_metadata: list[object] = []
    for name, prefix in SOURCE_PREFIXES.items():
        key = config_parameter_secret_env_key(prefix + ".password")
        candidates = [
            (binding, record)
            for binding, record in pairs
            if binding.binding_key == key
            and record.context == lane.context
            and record.instance in {"", lane.instance}
        ]
        exact = [pair for pair in candidates if pair[1].scope == "context_instance"]
        selected = exact or candidates
        # Snapshot even refused candidates, so a concurrent correction invalidates
        # stale missing/unsafe evidence as well as successful observations.
        selected_metadata.extend(
            (binding.model_dump(mode="json"), record.model_dump(mode="json"))
            for binding, record in candidates
        )
        if len(selected) != 1:
            continue
        binding, record = selected[0]
        if delivered.get(key) != binding:
            continue
        sharing = binding.sharing_reason
        if (
            record.scope != "context_instance"
            or binding.declared_secret_class not in ALLOWED_SECRET_CLASSES_BY_ENVIRONMENT["testing"]
            or (
                binding.declared_secret_class == "shared_safe"
                and (sharing is None or sharing.kind != "read_only_source")
            )
        ):
            continue
        if (
            sum(
                item.integration == name and item.kind == "read_only_source"
                for item in target.policies.integration_allowances
            )
            != 1
        ):
            continue
        try:
            version = store.read_secret_version(record.current_version_id)
        except FileNotFoundError:
            continue
        if version.secret_id != record.secret_id or version.version_id != record.current_version_id:
            continue
        sources[name] = SourceAuthority(binding, record, version, source_settings[name])
    snapshot = json.dumps(
        {
            "profile": profile.model_dump(mode="json"),
            "target": target.model_dump(mode="json"),
            "target_id": target_id.model_dump(mode="json"),
            "runtime": expectation.model_dump(mode="json"),
            "bindings": selected_metadata,
            "settings": source_settings,
            "versions": {
                name: authority.version.model_dump(mode="json")
                for name, authority in sources.items()
            },
        },
        sort_keys=True,
    )
    return ImportAuthority(profile, snapshot, sources, source_settings)


def read_applied_parameters(connection: reads.OdooRuntimeConnection) -> dict[str, str]:
    origin = connection.selection.base_url.rstrip("/")
    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid diagnostic origin")
    if not odoo_compose_has_domain(
        host=connection.host,
        token=connection.token,
        compose_id=connection.evidence.target_id,
        domain_host=url.hostname,
    ):
        raise ValueError("Diagnostic origin mismatch")
    authentication = reads.diagnostic_authentication(connection)
    opener = build_opener(reads._NoRedirects(), HTTPCookieProcessor(CookieJar()))

    def health() -> None:
        reads.require_runtime_identity(
            connection.selection.identity,
            runtime_identity_from_health_payload(
                reads._http_json(opener, Request(origin + "/launchplane/health"))
            ),
        )

    health()
    authenticated = reads.authenticate_diagnostic(
        connection,
        lambda: reads._rpc(opener, origin, "/web/session/authenticate", authentication),
    )
    keys = [
        prefix + "." + name for prefix in SOURCE_PREFIXES.values() for name in (*FIELDS, "password")
    ]
    try:
        reads.require_diagnostic_user(connection, authenticated)
        raw = reads._rpc(
            opener,
            origin,
            "/web/dataset/call_kw",
            {
                "model": "ir.config_parameter",
                "method": "search_read",
                "args": [[["key", "in", keys]]],
                "kwargs": {"fields": ["key", "value"], "limit": len(keys) + 1, "order": "key"},
            },
        )
    finally:
        reads.destroy_diagnostic_session(
            connection,
            lambda: reads._rpc(opener, origin, "/web/session/destroy", {}, expect_result=False),
        )
    health()
    if not isinstance(raw, list) or len(raw) > len(keys):
        raise ValueError("Invalid diagnostic rows")
    result: dict[str, str] = {}
    for row in raw:
        if (
            not isinstance(row, dict)
            or row.get("key") not in keys
            or not isinstance(row.get("value"), str)
            or row["key"] in result
        ):
            raise ValueError("Invalid diagnostic parameter")
        result[row["key"]] = row["value"]
    return result


def managed_grants(
    settings: dict[str, str], *, binding_id: str, version_id: str
) -> Literal["read_only", "unsafe", "unknown"]:
    # Single-flight and failure cooldown use immutable credential references,
    # never plaintext or password-derived hashes. Successful grants are never
    # cached: every successful result is a fresh source observation.
    key = (
        binding_id,
        version_id,
        settings.get("host", ""),
        settings.get("user", ""),
        settings.get("port", ""),
    )
    now = time.monotonic()
    with _PROBE_LOCK:
        for expired in [item for item, until in _FAILED_PROBES.items() if until <= now]:
            del _FAILED_PROBES[expired]
        if (
            key in _ACTIVE_PROBES
            or key in _FAILED_PROBES
            or len(_ACTIVE_PROBES) >= 4
            or len(_FAILED_PROBES) >= 128
        ):
            return "unknown"
        _ACTIVE_PROBES.add(key)
    result: Literal["read_only", "unsafe", "unknown"] = "unknown"
    try:
        result = _run_managed_grants(settings)
        return result
    finally:
        with _PROBE_LOCK:
            _ACTIVE_PROBES.discard(key)
            if result == "unknown":
                _FAILED_PROBES[key] = time.monotonic() + 60


def _run_managed_grants(settings: dict[str, str]) -> Literal["read_only", "unsafe", "unknown"]:
    if not all(settings.get(key) for key in ("host", "user", "password")):
        return "unknown"
    if settings.get("ssl_verify", "").lower() not in {"", "true", "1"}:
        return "unknown"
    private_input = {
        key: value
        for key, value in settings.items()
        if key in {"host", "user", "password", "port"} and value
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("odoo_source_probe.py"))],
            input=json.dumps(private_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                name: os.environ[name]
                for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")
                if os.environ.get(name)
            },
            timeout=10,
            check=False,
        )
        if completed.returncode == 0 and len(completed.stdout) < 128:
            payload = json.loads(completed.stdout)
            if payload == {"grants": "read_only"}:
                return "read_only"
            if payload == {"grants": "unsafe"}:
                return "unsafe"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return "unknown"


def runtime_origin_grants(
    connection: reads.OdooRuntimeConnection, selected: dict[str, SourceAuthority]
) -> dict[str, GrantStatus]:
    keys = {
        name: (
            source.binding.binding_id,
            source.version.version_id,
            connection.evidence.container_id,
            "runtime",
            name,
        )
        for name, source in selected.items()
    }
    active = {}
    results: dict[str, GrantStatus] = {name: "unknown" for name in selected}
    with _PROBE_LOCK:
        now = time.monotonic()
        for expired in [key for key, until in _FAILED_PROBES.items() if until <= now]:
            del _FAILED_PROBES[expired]
        for name, key in keys.items():
            if (
                key not in _ACTIVE_PROBES
                and key not in _FAILED_PROBES
                and len(_ACTIVE_PROBES) < 4
                and len(_FAILED_PROBES) < 128
            ):
                _ACTIVE_PROBES.add(key)
                active[name] = key
    try:
        if not active:
            return results
        results.update(
            runtime_grants(
                connection,
                tuple(active),
                prefixes=SOURCE_PREFIXES,
                fields=FIELDS,
                environment_keys={
                    name: {
                        field: (
                            config_parameter_secret_env_key(prefix + ".password")
                            if field == "password"
                            else config_parameter_runtime_key(prefix + "." + field)
                        )
                        for field in (*FIELDS, "password")
                    }
                    for name, prefix in SOURCE_PREFIXES.items()
                },
            )
        )
        return results
    finally:
        with _PROBE_LOCK:
            for name, key in active.items():
                _ACTIVE_PROBES.discard(key)
                if results[name] == "unknown":
                    _FAILED_PROBES[key] = time.monotonic() + 60


def parameter_agreement(key: str, expected: str, applied: str) -> Agreement:
    if not applied:
        return "missing"
    if not expected:
        return "unknown"
    if key == "ssl_verify":
        booleans = {"true": "true", "1": "true", "false": "false", "0": "false"}
        expected = booleans.get(expected.lower(), expected)
        applied = booleans.get(applied.lower(), applied)
    return "match" if hmac.compare_digest(expected.encode(), applied.encode()) else "mismatch"


def diagnose_imports(
    *, store: DiagnosticStore, product: str, control_plane_root: Path, database_url: str | None
) -> ImportDiagnosticResult:
    try:
        authority = import_authority(store, product)
        selection = reads.select_stable_runtime(store, authority.profile, "testing")
        connection = reads.connect_runtime(
            store=store,
            selection=selection,
            control_plane_root=control_plane_root,
            database_url=database_url,
            diagnostics=True,
        )
        applied = read_applied_parameters(connection)
        runtime_environment = reads.inspect_runtime_container(
            connection.host,
            connection.token,
            connection.server_id,
            connection.evidence.container_id,
            connection.app_name,
            selection.identity,
        )
        runtime_sources: dict[str, SourceAuthority] = {}
        results: list[SourceDiagnostic] = []
        for name, prefix in SOURCE_PREFIXES.items():
            source_name = cast(Literal["cm_data", "repairshopr", "fishbowl"], name)
            selected = authority.sources.get(name)
            if selected is None:
                results.append(
                    SourceDiagnostic(
                        source=source_name,
                        parameters=tuple(
                            ParameterAgreement(
                                field=cast(
                                    Literal["host", "user", "db", "port", "ssl_verify", "password"],
                                    key,
                                ),
                                applied_present=bool(applied.get(prefix + "." + key)),
                                agreement=parameter_agreement(
                                    key,
                                    authority.settings[name].get(key, ""),
                                    applied.get(prefix + "." + key, ""),
                                ),
                            )
                            for key in (*FIELDS, "password")
                        ),
                    )
                )
                continue
            try:
                password = _decrypt_secret_value(
                    selected.version.ciphertext, selected.version.key_id
                )
                settings = {**selected.settings, "password": password}
                parameters = tuple(
                    ParameterAgreement(
                        field=cast(
                            Literal["host", "user", "db", "port", "ssl_verify", "password"], key
                        ),
                        applied_present=bool(applied.get(prefix + "." + key)),
                        agreement=parameter_agreement(
                            key, settings[key], applied.get(prefix + "." + key, "")
                        ),
                    )
                    for key in (*FIELDS, "password")
                )
                # Runtime evidence needs every applied field and the already
                # delivered container values to agree with this selected binding.
                if all(item.agreement == "match" for item in parameters) and all(
                    parameter_agreement(
                        key,
                        settings[key],
                        runtime_environment.get(
                            config_parameter_secret_env_key(prefix + ".password")
                            if key == "password"
                            else config_parameter_runtime_key(prefix + "." + key),
                            "",
                        ),
                    )
                    == "match"
                    for key in (*FIELDS, "password")
                ):
                    runtime_sources[name] = selected
                results.append(
                    SourceDiagnostic(
                        source=source_name,
                        binding="selected",
                        parameters=parameters,
                        managed_connection_grants=managed_grants(
                            settings,
                            binding_id=selected.binding.binding_id,
                            version_id=selected.version.version_id,
                        )
                        if all(
                            settings[key] and settings[key] == applied.get(prefix + "." + key)
                            for key in ("host", "port")
                        )
                        and not (
                            settings["user"].strip() == applied.get(prefix + ".user", "").strip()
                            and parameter_agreement(
                                "password", password, applied.get(prefix + ".password", "")
                            )
                            != "match"
                        )
                        else "unknown",
                    )
                )
            except Exception:
                results.append(SourceDiagnostic(source=source_name))
        runtime_results = runtime_origin_grants(connection, runtime_sources)
        results = [
            result.model_copy(
                update={"applied_connection_grants": runtime_results.get(result.source, "unknown")}
            )
            for result in results
        ]
        reads.assert_selection_current(store, selection)
        if import_authority(store, product).private_snapshot != authority.private_snapshot:
            return unknown_result()
        # Observe the same immutable container again, including its image and
        # runtime payload, after source I/O and before releasing any agreement.
        if (
            reads.inspect_runtime_container(
                connection.host,
                connection.token,
                connection.server_id,
                connection.evidence.container_id,
                connection.app_name,
                selection.identity,
            )
            != runtime_environment
        ):
            return unknown_result()
        if read_applied_parameters(connection) != applied:
            return unknown_result()
        if import_authority(store, product).private_snapshot != authority.private_snapshot:
            return unknown_result()
        reads.assert_selection_current(store, selection)
        return ImportDiagnosticResult(runtime="verified", sources=tuple(results))
    except reads.OdooRuntimeReadError as error:
        if error.code == DIAGNOSTIC_IDENTITY_UNAVAILABLE:
            raise
        return unknown_result()
    except Exception:
        # No provider/RPC/driver/decryption exception text or chained context is
        # returned. This fixed response contains no caller/server-controlled text.
        return unknown_result()
