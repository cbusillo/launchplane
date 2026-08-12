from collections.abc import Callable
import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.deploy_target import DeployedTargetReference, ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, DokployTargetType
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProductAuthorityBundleStore,
    ProviderTargetWrite,
)
from control_plane.workflows.provider_target_dual_write import (
    ensure_provider_target_identity_unbound_elsewhere,
    prepare_provider_target_from_dokploy_records,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.dokploy.api import JsonObject


class DokployTargetAdoptionRecordStore(ProductAuthorityBundleStore, Protocol):
    def write_dokploy_target_record(self, record: DokployTargetRecord) -> None: ...

    def write_dokploy_target_id_record(self, record: DokployTargetIdRecord) -> None: ...

    def write_provider_target_record(self, record: ProviderTargetRecord) -> None: ...

    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def compare_and_write_dokploy_target_domains(
        self,
        *,
        expected_record: DokployTargetRecord,
        expected_target_id_record: DokployTargetIdRecord,
        expected_provider_target_record: ProviderTargetRecord,
        domains: tuple[str, ...],
        updated_at: str,
        source_label: str,
    ) -> tuple[DokployTargetRecord, ProviderTargetRecord]: ...


class DokployTargetAdoptionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    provider_target_record: ProviderTargetRecord
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None] = {}
    warnings: tuple[str, ...] = ()


class DokployTargetDomainRepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    provider_target_record: ProviderTargetRecord
    requested_domains: tuple[str, ...]
    live_provider_domains: tuple[str, ...]
    warnings: tuple[str, ...] = ()


FetchDokployTargetPayload = Callable[[str, str, DokployTargetType, str], JsonObject]
FetchDokployTargetDomains = Callable[[str, str, DokployTargetType, str], tuple[JsonObject, ...]]
MutateDokployPayload = Callable[[str, str, str, JsonObject], JsonObject]

_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_dokploy_domain_host(raw_host: str) -> str:
    host = raw_host.strip().lower()
    if not host or any(character.isspace() for character in host):
        raise ValueError("Dokploy domain authority requires bare DNS hosts.")
    if "://" in host or "/" in host or ":" in host or "@" in host:
        raise ValueError("Dokploy domain authority requires bare DNS hosts, not URLs.")
    host = host.rstrip(".")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("Dokploy domain authority contains an invalid DNS host.") from error
    labels = ascii_host.split(".")
    if (
        len(labels) < 2
        or len(ascii_host) > 253
        or any(not _DNS_LABEL_PATTERN.fullmatch(label) for label in labels)
    ):
        raise ValueError("Dokploy domain authority contains an invalid DNS host.")
    return ascii_host


def normalize_dokploy_domain_hosts(raw_hosts: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_host in raw_hosts:
        host = normalize_dokploy_domain_host(raw_host)
        if host in normalized:
            raise ValueError("Dokploy domain authority requires unique DNS hosts.")
        normalized.append(host)
    if not normalized:
        raise ValueError("Dokploy domain authority requires at least one DNS host.")
    return tuple(normalized)


def repair_dokploy_target_domain_authority(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    host: str,
    token: str,
    context: str,
    instance: str,
    target_type: DokployTargetType,
    target_id: str,
    domains: tuple[str, ...],
    expected_current_provider_target: DeployedTargetReference,
    source_label: str = "service:dokploy-targets:setup:repair-domain-authority",
    updated_at: str = "",
    apply: bool = False,
    fetch_target_payload: FetchDokployTargetPayload,
    fetch_target_domains: FetchDokployTargetDomains,
) -> DokployTargetDomainRepairResult:
    normalized_context = _normalize_route_part(context, "context")
    normalized_instance = _normalize_route_part(instance, "instance")
    normalized_target_id = _require_non_empty(target_id, "target_id")
    requested_domains = normalize_dokploy_domain_hosts(domains)
    recorded_at = updated_at.strip() or utc_now_timestamp()
    normalized_source_label = source_label.strip()

    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
        provider_target_record = record_store.read_provider_target_record(
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
    except FileNotFoundError as error:
        raise ValueError(
            "Dokploy domain authority repair requires tracked target, target-id, "
            "and provider-target records."
        ) from error

    if target_record.target_type != target_type:
        raise ValueError(
            "Dokploy domain authority repair target type does not match the tracked target."
        )
    if target_id_record.target_id != normalized_target_id:
        raise ValueError(
            "Dokploy domain authority repair target id does not match the tracked target."
        )
    if provider_target_record.to_deployed_target_reference() != expected_current_provider_target:
        raise ValueError(
            "Dokploy domain authority repair provider-target expectation did not match current authority."
        )
    if (
        provider_target_record.provider_id != "dokploy"
        or provider_target_record.target_id != normalized_target_id
        or provider_target_record.target_category != target_type
        or provider_target_record.provider_target_type != target_type
    ):
        raise ValueError(
            "Dokploy domain authority repair provider-target identity is inconsistent."
        )

    provider_payload = fetch_target_payload(host, token, target_type, normalized_target_id)
    identity_key = "applicationId" if target_type == "application" else "composeId"
    observed_identity = str(provider_payload.get(identity_key) or "").strip()
    if observed_identity != normalized_target_id:
        raise ValueError(
            "Dokploy domain authority repair live provider target identity did not match."
        )

    live_provider_domains = normalize_dokploy_domain_hosts(
        tuple(
            dict.fromkeys(
                str(domain.get("host") or "")
                for domain in fetch_target_domains(host, token, target_type, normalized_target_id)
            )
        )
    )
    if tuple(sorted(requested_domains)) != tuple(sorted(live_provider_domains)):
        raise ValueError(
            "Dokploy domain authority repair requested domains did not exactly match live provider domains."
        )

    projected_target_record = target_record.model_copy(
        update={
            "domains": requested_domains,
            "updated_at": recorded_at,
            "source_label": normalized_source_label,
        }
    )
    projected_provider_target_record = provider_target_record.model_copy(
        update={
            "updated_at": recorded_at,
            "source_label": normalized_source_label,
        }
    )
    if apply:
        target_record, provider_target_record = (
            record_store.compare_and_write_dokploy_target_domains(
                expected_record=target_record,
                expected_target_id_record=target_id_record,
                expected_provider_target_record=provider_target_record,
                domains=requested_domains,
                updated_at=recorded_at,
                source_label=normalized_source_label,
            )
        )
    else:
        target_record = projected_target_record
        provider_target_record = projected_provider_target_record

    return DokployTargetDomainRepairResult(
        applied=apply,
        target_record=target_record,
        target_id_record=target_id_record,
        provider_target_record=provider_target_record,
        requested_domains=requested_domains,
        live_provider_domains=live_provider_domains,
        warnings=()
        if apply
        else ("dry run only; provider was not mutated and records were not written",),
    )


class DokployTargetCreatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: dict[str, str]
    environment: dict[str, str]
    application: dict[str, str]


class DokployTargetCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    plan: DokployTargetCreatePlan
    project_id: str = ""
    environment_id: str = ""
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    provider_target_record: ProviderTargetRecord
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None] = {}
    provider_requests: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()


class DokployComposeTargetCreatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: dict[str, str]
    environment: dict[str, str]
    compose: dict[str, str]


class DokployComposeTargetCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    plan: DokployComposeTargetCreatePlan
    project_id: str = ""
    environment_id: str = ""
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    provider_target_record: ProviderTargetRecord
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None] = {}
    provider_requests: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()


def adopt_dokploy_target(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    host: str,
    token: str,
    context: str,
    instance: str,
    target_type: DokployTargetType,
    target_id: str,
    project_name: str = "",
    target_name: str = "",
    source_git_ref: str = "origin/main",
    healthcheck_path: str = "",
    domains: tuple[str, ...] = (),
    deploy_timeout_seconds: int | None = None,
    expected_current_provider_target: DeployedTargetReference | None = None,
    source_label: str = "cli:dokploy-targets:adopt",
    updated_at: str = "",
    apply: bool = False,
    fetch_target_payload: FetchDokployTargetPayload,
) -> DokployTargetAdoptionResult:
    normalized_context = _normalize_route_part(context, "context")
    normalized_instance = _normalize_route_part(instance, "instance")
    normalized_target_id = _require_non_empty(target_id, "target_id")
    recorded_at = updated_at.strip() or utc_now_timestamp()
    provider_payload = fetch_target_payload(host, token, target_type, normalized_target_id)
    provider_fields = _provider_fields(provider_payload=provider_payload, target_type=target_type)
    normalized_source_git_ref = _resolved_source_git_ref(
        explicit_source_git_ref=source_git_ref,
        provider_fields=provider_fields,
    )
    resolved_target_name = target_name.strip() or _provider_string_field(
        provider_fields, "target_name"
    )
    resolved_project_name = project_name.strip() or _provider_string_field(
        provider_fields, "project_name"
    )
    resolved_healthcheck_path = healthcheck_path.strip()
    if target_type == "compose" and not resolved_healthcheck_path:
        resolved_healthcheck_path = "/web/health"
    if resolved_healthcheck_path and not resolved_healthcheck_path.startswith("/"):
        raise ValueError("Dokploy target adoption requires --healthcheck-path to start with /")

    warnings = _adoption_warnings(
        target_type=target_type,
        provider_payload=provider_payload,
        target_name=resolved_target_name,
        project_name=resolved_project_name,
    )
    healthcheck_enabled = _bool_field(provider_payload, "healthcheckEnabled")
    if healthcheck_enabled is None:
        healthcheck_enabled = True
    target_record = DokployTargetRecord(
        context=normalized_context,
        instance=normalized_instance,
        project_name=resolved_project_name,
        target_type=target_type,
        target_name=resolved_target_name,
        source_git_ref=normalized_source_git_ref,
        git_branch=_provider_string_field(provider_fields, "git_branch")
        or _provider_string_field(provider_fields, "branch"),
        source_type=_provider_string_field(provider_fields, "source_type"),
        custom_git_url=_provider_string_field(provider_fields, "custom_git_url"),
        custom_git_branch=_provider_string_field(provider_fields, "custom_git_branch"),
        compose_path=_provider_string_field(provider_fields, "compose_path"),
        watch_paths=_tuple_string_field(provider_fields.get("watch_paths")),
        enable_submodules=_optional_bool_field(provider_fields.get("enable_submodules")),
        deploy_timeout_seconds=deploy_timeout_seconds,
        healthcheck_enabled=healthcheck_enabled,
        healthcheck_path=resolved_healthcheck_path,
        healthcheck_timeout_seconds=_int_field(provider_payload, "healthcheckTimeoutSeconds"),
        domains=domains,
        updated_at=recorded_at,
        source_label=source_label.strip() or "cli:dokploy-targets:adopt",
    )
    target_id_record = DokployTargetIdRecord(
        context=normalized_context,
        instance=normalized_instance,
        target_id=normalized_target_id,
        updated_at=recorded_at,
        source_label=source_label.strip() or "cli:dokploy-targets:adopt",
    )
    provider_target_record = ProviderTargetRecord.from_dokploy_records(
        target_record=target_record,
        target_id_record=target_id_record,
    )
    ensure_provider_target_identity_unbound_elsewhere(
        record_store=record_store,
        provider_target_record=provider_target_record,
    )

    if apply:
        provider_target_record = prepare_provider_target_from_dokploy_records(
            record_store=record_store,
            target_record=target_record,
            target_id_record=target_id_record,
            expected_current_provider_target=expected_current_provider_target,
        )
        current_provider_targets = {
            (record.context, record.instance): record
            for record in record_store.list_physical_provider_target_records()
        }
        requested_route = (provider_target_record.context, provider_target_record.instance)
        record_store.write_product_authority_bundle(
            ProductAuthorityBundle(
                dokploy_targets=(target_record,),
                dokploy_target_ids=(target_id_record,),
                provider_target_writes=(
                    ProviderTargetWrite(
                        record=provider_target_record,
                        expected_record=current_provider_targets.get(requested_route),
                        expected_absent=requested_route not in current_provider_targets,
                    ),
                ),
            )
        )

    return DokployTargetAdoptionResult(
        applied=apply,
        target_record=target_record,
        target_id_record=target_id_record,
        provider_target_record=provider_target_record,
        provider_fields=provider_fields,
        warnings=warnings,
    )


def create_dokploy_application_target(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    host: str,
    token: str,
    context: str,
    instance: str,
    target_name: str,
    project_id: str = "",
    project_name: str = "",
    project_description: str = "",
    environment_id: str = "",
    environment_name: str = "",
    environment_description: str = "",
    server_id: str = "",
    app_name: str = "",
    application_description: str = "",
    source_git_ref: str = "origin/main",
    healthcheck_path: str = "",
    domains: tuple[str, ...] = (),
    deploy_timeout_seconds: int | None = None,
    expected_current_provider_target: DeployedTargetReference | None = None,
    source_label: str = "cli:dokploy-targets:create-application",
    updated_at: str = "",
    apply: bool = False,
    mutate_provider: MutateDokployPayload,
    fetch_target_payload: FetchDokployTargetPayload,
) -> DokployTargetCreateResult:
    normalized_context = _normalize_route_part(context, "context")
    normalized_instance = _normalize_route_part(instance, "instance")
    normalized_target_name = _require_non_empty(target_name, "target_name")
    normalized_project_id = project_id.strip()
    normalized_project_name = project_name.strip()
    normalized_environment_id = environment_id.strip()
    normalized_environment_name = environment_name.strip() or normalized_instance
    normalized_healthcheck_path = healthcheck_path.strip()
    if normalized_healthcheck_path and not normalized_healthcheck_path.startswith("/"):
        raise ValueError("Dokploy target creation requires --healthcheck-path to start with /")
    if not normalized_environment_id and not normalized_project_id and not normalized_project_name:
        raise ValueError(
            "Dokploy target creation requires --project-id or --project-name when --environment-id is not supplied"
        )

    provider_requests = _planned_provider_requests(
        project_id=normalized_project_id,
        project_name=normalized_project_name,
        project_description=project_description,
        environment_id=normalized_environment_id,
        environment_name=normalized_environment_name,
        environment_description=environment_description,
        target_name=normalized_target_name,
        app_name=app_name,
        application_description=application_description,
        server_id=server_id,
    )
    plan = DokployTargetCreatePlan(
        project={
            "action": _project_plan_action(
                project_id=normalized_project_id,
                environment_id=normalized_environment_id,
            ),
            "project_id": normalized_project_id,
            "project_name": normalized_project_name,
        },
        environment={
            "action": "use_existing" if normalized_environment_id else "create",
            "environment_id": normalized_environment_id,
            "environment_name": normalized_environment_name,
        },
        application={
            "action": "create",
            "target_name": normalized_target_name,
            "app_name": app_name.strip(),
            "server_id": server_id.strip(),
        },
    )

    if not apply:
        target_record = DokployTargetRecord(
            context=normalized_context,
            instance=normalized_instance,
            project_name=normalized_project_name,
            target_type="application",
            target_name=normalized_target_name,
            source_git_ref=source_git_ref.strip() or "origin/main",
            deploy_timeout_seconds=deploy_timeout_seconds,
            healthcheck_path=normalized_healthcheck_path,
            domains=domains,
            updated_at=updated_at.strip() or utc_now_timestamp(),
            source_label=source_label.strip() or "cli:dokploy-targets:create-application",
        )
        target_id_record = DokployTargetIdRecord(
            context=normalized_context,
            instance=normalized_instance,
            target_id="planned-application-id",
            updated_at=target_record.updated_at,
            source_label=target_record.source_label,
        )
        provider_target_record = ProviderTargetRecord.from_dokploy_records(
            target_record=target_record,
            target_id_record=target_id_record,
        )
        return DokployTargetCreateResult(
            applied=False,
            plan=plan,
            target_record=target_record,
            target_id_record=target_id_record,
            provider_target_record=provider_target_record,
            provider_requests=provider_requests,
            warnings=("dry run only; provider was not mutated and records were not written",),
        )

    _ensure_create_route_has_no_provider_target(
        record_store=record_store,
        context=normalized_context,
        instance=normalized_instance,
        expected_current_provider_target=expected_current_provider_target,
    )

    created_project_id = normalized_project_id
    if not created_project_id and not normalized_environment_id:
        project_payload: JsonObject = {"name": normalized_project_name}
        if project_description.strip():
            project_payload["description"] = project_description.strip()
        created_project = mutate_provider(host, token, "/api/project.create", project_payload)
        created_project_id = _extract_provider_id(created_project, "projectId", "project")

    created_environment_id = normalized_environment_id
    if not created_environment_id:
        environment_payload: JsonObject = {
            "name": normalized_environment_name,
            "projectId": created_project_id,
        }
        if environment_description.strip():
            environment_payload["description"] = environment_description.strip()
        created_environment = mutate_provider(
            host, token, "/api/environment.create", environment_payload
        )
        created_environment_id = _extract_provider_id(
            created_environment, "environmentId", "environment"
        )

    application_payload: JsonObject = {
        "name": normalized_target_name,
        "environmentId": created_environment_id,
    }
    if app_name.strip():
        application_payload["appName"] = app_name.strip()
    if application_description.strip():
        application_payload["description"] = application_description.strip()
    if server_id.strip():
        application_payload["serverId"] = server_id.strip()
    created_application = mutate_provider(
        host, token, "/api/application.create", application_payload
    )
    application_id = _extract_provider_id(created_application, "applicationId", "application")

    adoption = adopt_dokploy_target(
        record_store=record_store,
        host=host,
        token=token,
        context=normalized_context,
        instance=normalized_instance,
        target_type="application",
        target_id=application_id,
        project_name=normalized_project_name,
        target_name=normalized_target_name,
        source_git_ref=source_git_ref,
        healthcheck_path=normalized_healthcheck_path,
        domains=domains,
        deploy_timeout_seconds=deploy_timeout_seconds,
        expected_current_provider_target=expected_current_provider_target,
        source_label=source_label,
        updated_at=updated_at,
        apply=True,
        fetch_target_payload=fetch_target_payload,
    )
    return DokployTargetCreateResult(
        applied=True,
        plan=plan,
        project_id=created_project_id,
        environment_id=created_environment_id,
        target_record=adoption.target_record,
        target_id_record=adoption.target_id_record,
        provider_target_record=adoption.provider_target_record,
        provider_fields=adoption.provider_fields,
        provider_requests=provider_requests,
        warnings=adoption.warnings,
    )


def create_dokploy_compose_target(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    host: str,
    token: str,
    context: str,
    instance: str,
    target_name: str,
    project_id: str = "",
    project_name: str = "",
    project_description: str = "",
    environment_id: str = "",
    environment_name: str = "",
    environment_description: str = "",
    server_id: str = "",
    app_name: str = "",
    compose_description: str = "",
    source_git_ref: str = "origin/main",
    source_type: str = "raw",
    compose_path: str = "docker-compose.yml",
    healthcheck_path: str = "",
    domains: tuple[str, ...] = (),
    deploy_timeout_seconds: int | None = None,
    expected_current_provider_target: DeployedTargetReference | None = None,
    source_label: str = "cli:dokploy-targets:create-compose",
    updated_at: str = "",
    apply: bool = False,
    mutate_provider: MutateDokployPayload,
    fetch_target_payload: FetchDokployTargetPayload,
) -> DokployComposeTargetCreateResult:
    normalized_context = _normalize_route_part(context, "context")
    normalized_instance = _normalize_route_part(instance, "instance")
    normalized_target_name = _require_non_empty(target_name, "target_name")
    normalized_project_id = project_id.strip()
    normalized_project_name = project_name.strip()
    normalized_environment_id = environment_id.strip()
    normalized_environment_name = environment_name.strip() or normalized_instance
    normalized_server_id = _require_non_empty(server_id, "server_id")
    normalized_healthcheck_path = healthcheck_path.strip()
    if normalized_healthcheck_path and not normalized_healthcheck_path.startswith("/"):
        raise ValueError(
            "Dokploy compose target creation requires --healthcheck-path to start with /"
        )
    if not normalized_environment_id and not normalized_project_id and not normalized_project_name:
        raise ValueError(
            "Dokploy compose target creation requires --project-id or --project-name when --environment-id is not supplied"
        )

    provider_requests = _planned_compose_provider_requests(
        project_id=normalized_project_id,
        project_name=normalized_project_name,
        project_description=project_description,
        environment_id=normalized_environment_id,
        environment_name=normalized_environment_name,
        environment_description=environment_description,
        target_name=normalized_target_name,
        app_name=app_name,
        compose_description=compose_description,
        server_id=normalized_server_id,
        source_type=source_type,
        compose_path=compose_path,
    )
    plan = DokployComposeTargetCreatePlan(
        project={
            "action": _project_plan_action(
                project_id=normalized_project_id,
                environment_id=normalized_environment_id,
            ),
            "project_id": normalized_project_id,
            "project_name": normalized_project_name,
        },
        environment={
            "action": "use_existing" if normalized_environment_id else "create",
            "environment_id": normalized_environment_id,
            "environment_name": normalized_environment_name,
        },
        compose={
            "action": "create",
            "target_name": normalized_target_name,
            "app_name": app_name.strip(),
            "server_id": normalized_server_id,
            "source_type": source_type.strip() or "raw",
            "compose_path": compose_path.strip() or "docker-compose.yml",
        },
    )

    recorded_at = updated_at.strip() or utc_now_timestamp()
    if not apply:
        target_record = DokployTargetRecord(
            context=normalized_context,
            instance=normalized_instance,
            project_name=normalized_project_name,
            target_type="compose",
            target_name=normalized_target_name,
            source_git_ref=source_git_ref.strip() or "origin/main",
            source_type=source_type.strip() or "raw",
            compose_path=compose_path.strip() or "docker-compose.yml",
            deploy_timeout_seconds=deploy_timeout_seconds,
            healthcheck_path=normalized_healthcheck_path,
            domains=domains,
            updated_at=recorded_at,
            source_label=source_label.strip() or "cli:dokploy-targets:create-compose",
        )
        target_id_record = DokployTargetIdRecord(
            context=normalized_context,
            instance=normalized_instance,
            target_id="planned-compose-id",
            updated_at=recorded_at,
            source_label=target_record.source_label,
        )
        provider_target_record = ProviderTargetRecord.from_dokploy_records(
            target_record=target_record,
            target_id_record=target_id_record,
        )
        return DokployComposeTargetCreateResult(
            applied=False,
            plan=plan,
            target_record=target_record,
            target_id_record=target_id_record,
            provider_target_record=provider_target_record,
            provider_requests=provider_requests,
            warnings=("dry run only; provider was not mutated and records were not written",),
        )

    _ensure_create_route_has_no_provider_target(
        record_store=record_store,
        context=normalized_context,
        instance=normalized_instance,
        expected_current_provider_target=expected_current_provider_target,
    )

    created_project_id = normalized_project_id
    if not created_project_id and not normalized_environment_id:
        project_payload: JsonObject = {"name": normalized_project_name}
        if project_description.strip():
            project_payload["description"] = project_description.strip()
        created_project = mutate_provider(host, token, "/api/project.create", project_payload)
        created_project_id = _extract_provider_id(created_project, "projectId", "project")

    created_environment_id = normalized_environment_id
    if not created_environment_id:
        environment_payload: JsonObject = {
            "name": normalized_environment_name,
            "projectId": created_project_id,
        }
        if environment_description.strip():
            environment_payload["description"] = environment_description.strip()
        created_environment = mutate_provider(
            host, token, "/api/environment.create", environment_payload
        )
        created_environment_id = _extract_provider_id(
            created_environment, "environmentId", "environment"
        )

    compose_payload: JsonObject = {
        "name": normalized_target_name,
        "appName": app_name.strip() or normalized_target_name,
        "environmentId": created_environment_id,
        "serverId": normalized_server_id,
        "composeType": "docker-compose",
    }
    if compose_description.strip():
        compose_payload["description"] = compose_description.strip()
    created_compose = mutate_provider(host, token, "/api/compose.create", compose_payload)
    compose_id = _extract_provider_id(created_compose, "composeId", "compose")

    adoption = adopt_dokploy_target(
        record_store=record_store,
        host=host,
        token=token,
        context=normalized_context,
        instance=normalized_instance,
        target_type="compose",
        target_id=compose_id,
        project_name=normalized_project_name,
        target_name=normalized_target_name,
        source_git_ref=source_git_ref,
        healthcheck_path=normalized_healthcheck_path,
        domains=domains,
        deploy_timeout_seconds=deploy_timeout_seconds,
        expected_current_provider_target=expected_current_provider_target,
        source_label=source_label,
        updated_at=updated_at,
        apply=True,
        fetch_target_payload=fetch_target_payload,
    )
    target_record = adoption.target_record.model_copy(
        update={
            "source_type": adoption.target_record.source_type or source_type.strip() or "raw",
            "compose_path": adoption.target_record.compose_path
            or compose_path.strip()
            or "docker-compose.yml",
        }
    )
    if target_record != adoption.target_record:
        provider_target_record = prepare_provider_target_from_dokploy_records(
            record_store=record_store,
            target_record=target_record,
            target_id_record=adoption.target_id_record,
        )
        record_store.write_product_authority_bundle(
            ProductAuthorityBundle(
                dokploy_targets=(target_record,),
                provider_target_writes=(
                    ProviderTargetWrite(
                        record=provider_target_record,
                        expected_record=adoption.provider_target_record,
                    ),
                ),
            )
        )
    else:
        provider_target_record = adoption.provider_target_record
    return DokployComposeTargetCreateResult(
        applied=True,
        plan=plan,
        project_id=created_project_id,
        environment_id=created_environment_id,
        target_record=target_record,
        target_id_record=adoption.target_id_record,
        provider_target_record=provider_target_record,
        provider_fields=adoption.provider_fields,
        provider_requests=provider_requests,
        warnings=adoption.warnings,
    )


def _require_non_empty(value: str, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"Dokploy target adoption requires {field_name}")
    return normalized_value


def _ensure_create_route_has_no_provider_target(
    *,
    record_store: DokployTargetAdoptionRecordStore,
    context: str,
    instance: str,
    expected_current_provider_target: DeployedTargetReference | None = None,
) -> None:
    found_current_provider_target = False
    for record in record_store.list_physical_provider_target_records():
        if record.context != context or record.instance != instance:
            continue
        found_current_provider_target = True
        if expected_current_provider_target is None:
            raise ValueError(
                "Provider target dual-write conflict for "
                f"{context}/{instance}; create would replace an explicit provider target."
            )
        if record.to_deployed_target_reference() != expected_current_provider_target:
            raise ValueError(
                "Provider target replacement expectation did not match current authority for "
                f"{context}/{instance}."
            )
    if expected_current_provider_target is not None and not found_current_provider_target:
        raise ValueError(
            "Provider target replacement expected an existing provider target for "
            f"{context}/{instance}."
        )


def _normalize_route_part(value: str, field_name: str) -> str:
    return _require_non_empty(value, field_name).lower()


def _resolved_source_git_ref(
    *,
    explicit_source_git_ref: str,
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None],
) -> str:
    resolved_source_git_ref = explicit_source_git_ref.strip()
    if resolved_source_git_ref:
        return resolved_source_git_ref
    provider_source_git_ref = str(provider_fields.get("source_git_ref") or "").strip()
    if provider_source_git_ref:
        return provider_source_git_ref
    provider_branch = str(provider_fields.get("branch") or "").strip()
    if provider_branch:
        return provider_branch
    return "origin/main"


def _provider_string_field(
    provider_fields: dict[str, str | tuple[str, ...] | bool | int | None], key: str
) -> str:
    value = provider_fields.get(key)
    if isinstance(value, str):
        return value
    return ""


def _planned_provider_requests(
    *,
    project_id: str,
    project_name: str,
    project_description: str,
    environment_id: str,
    environment_name: str,
    environment_description: str,
    target_name: str,
    app_name: str,
    application_description: str,
    server_id: str,
) -> tuple[dict[str, object], ...]:
    requests: list[dict[str, object]] = []
    if not environment_id and not project_id:
        project_payload: dict[str, object] = {"name": project_name}
        if project_description.strip():
            project_payload["description"] = project_description.strip()
        requests.append({"path": "/api/project.create", "payload": project_payload})
    if not environment_id:
        environment_payload: dict[str, object] = {
            "name": environment_name,
            "projectId": project_id or "<created-project-id>",
        }
        if environment_description.strip():
            environment_payload["description"] = environment_description.strip()
        requests.append({"path": "/api/environment.create", "payload": environment_payload})
    application_payload: dict[str, object] = {
        "name": target_name,
        "environmentId": environment_id or "<created-environment-id>",
    }
    if app_name.strip():
        application_payload["appName"] = app_name.strip()
    if application_description.strip():
        application_payload["description"] = application_description.strip()
    if server_id.strip():
        application_payload["serverId"] = server_id.strip()
    requests.append({"path": "/api/application.create", "payload": application_payload})
    return tuple(requests)


def _planned_compose_provider_requests(
    *,
    project_id: str,
    project_name: str,
    project_description: str,
    environment_id: str,
    environment_name: str,
    environment_description: str,
    target_name: str,
    app_name: str,
    compose_description: str,
    server_id: str,
    source_type: str,
    compose_path: str,
) -> tuple[dict[str, object], ...]:
    requests: list[dict[str, object]] = []
    if not environment_id and not project_id:
        project_payload: dict[str, object] = {"name": project_name}
        if project_description.strip():
            project_payload["description"] = project_description.strip()
        requests.append({"path": "/api/project.create", "payload": project_payload})
    if not environment_id:
        environment_payload: dict[str, object] = {
            "name": environment_name,
            "projectId": project_id or "<created-project-id>",
        }
        if environment_description.strip():
            environment_payload["description"] = environment_description.strip()
        requests.append({"path": "/api/environment.create", "payload": environment_payload})
    compose_payload: dict[str, object] = {
        "name": target_name,
        "appName": app_name.strip() or target_name,
        "environmentId": environment_id or "<created-environment-id>",
        "serverId": server_id,
        "composeType": "docker-compose",
    }
    if compose_description.strip():
        compose_payload["description"] = compose_description.strip()
    requests.append({"path": "/api/compose.create", "payload": compose_payload})
    return tuple(requests)


def _project_plan_action(*, project_id: str, environment_id: str) -> str:
    if project_id:
        return "use_existing"
    if environment_id:
        return "not_required"
    return "create"


def _extract_provider_id(payload: JsonObject, id_key: str, object_key: str) -> str:
    direct_id = _string_field(payload, id_key) or _string_field(payload, "id")
    if direct_id:
        return direct_id
    nested_id = _nested_string_field(payload, (object_key,), id_key) or _nested_string_field(
        payload, (object_key,), "id"
    )
    if nested_id:
        return nested_id
    raise ValueError(f"Dokploy {object_key}.create did not return {id_key}")


def _provider_fields(
    *, provider_payload: JsonObject, target_type: DokployTargetType
) -> dict[str, str | tuple[str, ...] | bool | int | None]:
    fields: dict[str, str | tuple[str, ...] | bool | int | None] = {}
    name = _string_field(provider_payload, "name")
    if name:
        fields["target_name"] = name
    _put_if_present(fields, "source_git_ref", _string_field(provider_payload, "sourceGitRef"))
    _put_if_present(fields, "branch", _string_field(provider_payload, "branch"))
    _put_if_present(fields, "git_branch", _string_field(provider_payload, "gitBranch"))
    project_name = _nested_string_field(provider_payload, ("project",), "name")
    if project_name:
        fields["project_name"] = project_name
    environment_project_name = _nested_string_field(
        provider_payload, ("environment", "project"), "name"
    )
    if environment_project_name:
        fields["project_name"] = environment_project_name

    if target_type == "compose":
        _put_if_present(fields, "source_type", _string_field(provider_payload, "sourceType"))
        _put_if_present(fields, "custom_git_url", _string_field(provider_payload, "customGitUrl"))
        _put_if_present(
            fields, "custom_git_branch", _string_field(provider_payload, "customGitBranch")
        )
        _put_if_present(
            fields, "custom_git_build_path", _string_field(provider_payload, "customGitBuildPath")
        )
        _put_if_present(
            fields, "custom_git_ssh_key_id", _string_field(provider_payload, "customGitSSHKeyId")
        )
        _put_if_present(fields, "compose_path", _string_field(provider_payload, "composePath"))
        watch_paths = _string_list_field(provider_payload, "watchPaths")
        if watch_paths:
            fields["watch_paths"] = watch_paths
        enable_submodules = _bool_field(provider_payload, "enableSubmodules")
        if enable_submodules is not None:
            fields["enable_submodules"] = enable_submodules
    return fields


def _adoption_warnings(
    *,
    target_type: DokployTargetType,
    provider_payload: JsonObject,
    target_name: str,
    project_name: str,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if not target_name:
        warnings.append("provider target name was not discovered; pass --target-name if needed")
    if not project_name:
        warnings.append("provider project name was not discovered; pass --project-name if needed")
    if target_type == "application":
        env_text = _string_field(provider_payload, "env")
        if env_text:
            warnings.append(
                "provider env exists but was not copied; store runtime values through Launchplane runtime/secret records"
            )
    return tuple(warnings)


def _put_if_present(
    fields: dict[str, str | tuple[str, ...] | bool | int | None], key: str, value: str
) -> None:
    if value:
        fields[key] = value


def _string_list_field(payload: JsonObject, key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    items: list[str] = []
    for raw_item in value:
        item = str(raw_item).strip()
        if item:
            items.append(item)
    return tuple(items)


def _tuple_string_field(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _optional_bool_field(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _bool_field(payload: JsonObject, key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return None


def _int_field(payload: JsonObject, key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _string_field(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _nested_string_field(payload: JsonObject, path: tuple[str, ...], key: str) -> str:
    current: object = payload
    for path_key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(path_key)
    if not isinstance(current, dict):
        return ""
    value = current.get(key)
    if value is None:
        return ""
    return str(value).strip()
