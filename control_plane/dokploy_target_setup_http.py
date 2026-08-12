from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import DeployedTargetReference
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.dokploy_target_adoption import (
    DokployComposeTargetCreateResult,
    DokployTargetAdoptionResult,
    DokployTargetCreateResult,
    adopt_dokploy_target,
    create_dokploy_application_target,
    create_dokploy_compose_target,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import compose as dokploy_compose


class DokployTargetSetupEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry-run", "apply"] = "dry-run"
    operation: Literal[
        "adopt",
        "create-application",
        "create-compose",
        "prune-compose-domain",
        "reconcile-compose-domain",
    ]
    product: str = "launchplane"
    context: str
    instance: str
    target_type: Literal["application", "compose"] = "compose"
    target_id: str = ""
    target_name: str = ""
    project_id: str = ""
    project_name: str = ""
    project_description: str = ""
    environment_id: str = ""
    environment_name: str = ""
    environment_description: str = ""
    server_id: str = ""
    app_name: str = ""
    description: str = ""
    source_git_ref: str = "origin/main"
    source_type: str = "raw"
    compose_path: str = "docker-compose.yml"
    healthcheck_path: str = ""
    domains: tuple[str, ...] = ()
    runtime_port: int | None = Field(default=None, ge=1, le=65535)
    deploy_timeout_seconds: int | None = Field(default=None, ge=1)
    expected_current_provider_target: DeployedTargetReference | None = None
    confirmation: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _validate_setup(self) -> "DokployTargetSetupEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Dokploy target setup requires product 'launchplane'.")
        self.product = "launchplane"
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.target_id = self.target_id.strip()
        self.target_name = self.target_name.strip()
        self.project_id = self.project_id.strip()
        self.project_name = self.project_name.strip()
        self.environment_id = self.environment_id.strip()
        self.environment_name = self.environment_name.strip()
        self.server_id = self.server_id.strip()
        self.app_name = self.app_name.strip()
        self.source_git_ref = self.source_git_ref.strip() or "origin/main"
        self.source_type = self.source_type.strip() or "raw"
        self.compose_path = self.compose_path.strip() or "docker-compose.yml"
        self.healthcheck_path = self.healthcheck_path.strip()
        self.confirmation = self.confirmation.strip()
        self.reason = self.reason.strip()
        self.domains = tuple(domain.strip() for domain in self.domains if domain.strip())
        if not self.context:
            raise ValueError("Dokploy target setup requires context.")
        if not self.instance:
            raise ValueError("Dokploy target setup requires instance.")
        if self.operation == "adopt" and not self.target_id:
            raise ValueError("Dokploy target adoption requires target_id.")
        if self.operation == "create-application" and not self.target_name:
            raise ValueError("Dokploy application target creation requires target_name.")
        if self.operation == "create-compose":
            if not self.target_name:
                raise ValueError("Dokploy compose target creation requires target_name.")
            if not self.server_id:
                raise ValueError("Dokploy compose target creation requires server_id.")
        if self.runtime_port is not None and self.operation not in {
            "create-compose",
            "reconcile-compose-domain",
        }:
            raise ValueError(
                "Dokploy target setup runtime_port is only supported for create-compose or reconcile-compose-domain."
            )
        if self.runtime_port is not None and not self.domains:
            raise ValueError("Dokploy target setup runtime_port requires at least one domain.")
        if self.operation == "reconcile-compose-domain":
            if not self.domains:
                raise ValueError("Dokploy compose domain reconciliation requires domains.")
            if self.runtime_port is None:
                raise ValueError("Dokploy compose domain reconciliation requires runtime_port.")
        if self.operation == "prune-compose-domain" and not self.domains:
            raise ValueError("Dokploy compose domain prune requires domains.")
        if self.healthcheck_path and not self.healthcheck_path.startswith("/"):
            raise ValueError("Dokploy target setup healthcheck_path must start with /.")
        return self


class DokployComposeDomainReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    domains: tuple[str, ...]
    runtime_port: int
    route_domain_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class DokployComposeDomainPruneResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool
    target_record: DokployTargetRecord
    target_id_record: DokployTargetIdRecord
    domains: tuple[str, ...]
    matched_domain_ids: tuple[str, ...] = ()
    deleted_domain_ids: tuple[str, ...] = ()
    missing_domains: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def mutate_dokploy_payload_for_target_setup(
    host: str,
    token: str,
    path: str,
    payload: dict[str, dokploy_api.JsonValue],
) -> dict[str, dokploy_api.JsonValue]:
    response = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path=path,
        method="POST",
        payload=payload,
    )
    response_object = dokploy_api.as_json_object(response)
    if response_object is None:
        raise click.ClickException(f"Dokploy API POST {path} returned an invalid response.")
    return response_object


def fetch_dokploy_compose_domains_for_target_setup(
    *,
    host: str,
    token: str,
    compose_id: str,
) -> tuple[dict[str, dokploy_api.JsonValue], ...]:
    response = dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.byComposeId",
        query={"composeId": compose_id},
    )
    if not isinstance(response, list):
        raise click.ClickException(
            f"Dokploy domain lookup for compose {compose_id} returned an invalid response."
        )
    domains: list[dict[str, dokploy_api.JsonValue]] = []
    for raw_domain in response:
        domain = dokploy_api.as_json_object(raw_domain)
        if domain is not None:
            domains.append(domain)
    return tuple(domains)


def delete_dokploy_domain_for_target_setup(*, host: str, token: str, domain_id: str) -> None:
    dokploy_api.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.delete",
        method="POST",
        payload={"domainId": domain_id},
    )


def _dokploy_domain_matches_tracked_compose_web_route(
    *,
    provider_domain: dict[str, dokploy_api.JsonValue],
    compose_id: str,
    domain_host: str,
) -> bool:
    if str(provider_domain.get("host") or "").strip() != domain_host:
        return False
    provider_compose_id = str(provider_domain.get("composeId") or "").strip()
    if provider_compose_id and provider_compose_id != compose_id:
        return False
    domain_type = str(provider_domain.get("domainType") or "").strip()
    if domain_type and domain_type != "compose":
        return False
    service_name = str(provider_domain.get("serviceName") or "").strip()
    if service_name and service_name != "web":
        return False
    route_path = str(provider_domain.get("path") or "/").strip() or "/"
    if route_path != "/":
        return False
    internal_path = str(provider_domain.get("internalPath") or "/").strip() or "/"
    return internal_path == "/"


def fetch_dokploy_target_payload_for_setup(
    host: str,
    token: str,
    target_type: str,
    target_id: str,
) -> dict[str, dokploy_api.JsonValue]:
    return dokploy_api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )


def dokploy_target_setup_result_payload(result: BaseModel) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    if isinstance(result, DokployComposeDomainReconcileResult):
        payload.pop("route_domain_ids", None)
    record = payload.get("target_record")
    if isinstance(record, dict):
        env_payload = record.pop("env", None)
        if isinstance(env_payload, dict):
            record["env_keys"] = sorted(str(key) for key in env_payload)
            record["env_value_count"] = len(env_payload)
    return payload


def execute_dokploy_target_setup(
    *,
    control_plane_root_path: Path,
    record_store: PostgresRecordStore,
    request: DokployTargetSetupEnvelope,
) -> dict[str, object]:
    apply_changes = request.mode == "apply"
    if request.operation in {"adopt", "create-application", "create-compose"}:
        ensure_dokploy_target_setup_context_is_not_historical(
            record_store=record_store,
            context=request.context,
        )
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root_path)
    result: (
        DokployTargetAdoptionResult
        | DokployTargetCreateResult
        | DokployComposeTargetCreateResult
        | DokployComposeDomainReconcileResult
        | DokployComposeDomainPruneResult
    )
    if request.operation == "adopt":
        result = adopt_dokploy_target(
            record_store=record_store,
            host=host,
            token=token,
            context=request.context,
            instance=request.instance,
            target_type=request.target_type,
            target_id=request.target_id,
            project_name=request.project_name,
            target_name=request.target_name,
            source_git_ref=request.source_git_ref,
            healthcheck_path=request.healthcheck_path,
            domains=request.domains,
            deploy_timeout_seconds=request.deploy_timeout_seconds,
            expected_current_provider_target=request.expected_current_provider_target,
            source_label="service:dokploy-targets:setup:adopt",
            apply=apply_changes,
            fetch_target_payload=fetch_dokploy_target_payload_for_setup,
        )
    elif request.operation == "reconcile-compose-domain":
        result = _execute_dokploy_compose_domain_reconcile(
            record_store=record_store,
            request=request,
            host=host,
            token=token,
            apply_changes=apply_changes,
        )
    elif request.operation == "prune-compose-domain":
        result = _execute_dokploy_compose_domain_prune(
            record_store=record_store,
            request=request,
            host=host,
            token=token,
            apply_changes=apply_changes,
        )
    elif request.operation == "create-application":
        result = create_dokploy_application_target(
            record_store=record_store,
            host=host,
            token=token,
            context=request.context,
            instance=request.instance,
            target_name=request.target_name,
            project_id=request.project_id,
            project_name=request.project_name,
            project_description=request.project_description,
            environment_id=request.environment_id,
            environment_name=request.environment_name,
            environment_description=request.environment_description,
            server_id=request.server_id,
            app_name=request.app_name,
            application_description=request.description,
            source_git_ref=request.source_git_ref,
            healthcheck_path=request.healthcheck_path,
            domains=request.domains,
            deploy_timeout_seconds=request.deploy_timeout_seconds,
            expected_current_provider_target=request.expected_current_provider_target,
            source_label="service:dokploy-targets:setup:create-application",
            apply=apply_changes,
            mutate_provider=mutate_dokploy_payload_for_target_setup,
            fetch_target_payload=fetch_dokploy_target_payload_for_setup,
        )
    else:
        result = create_dokploy_compose_target(
            record_store=record_store,
            host=host,
            token=token,
            context=request.context,
            instance=request.instance,
            target_name=request.target_name,
            project_id=request.project_id,
            project_name=request.project_name,
            project_description=request.project_description,
            environment_id=request.environment_id,
            environment_name=request.environment_name,
            environment_description=request.environment_description,
            server_id=request.server_id,
            app_name=request.app_name,
            compose_description=request.description,
            source_git_ref=request.source_git_ref,
            source_type=request.source_type,
            compose_path=request.compose_path,
            healthcheck_path=request.healthcheck_path,
            domains=request.domains,
            deploy_timeout_seconds=request.deploy_timeout_seconds,
            expected_current_provider_target=request.expected_current_provider_target,
            source_label="service:dokploy-targets:setup:create-compose",
            apply=apply_changes,
            mutate_provider=mutate_dokploy_payload_for_target_setup,
            fetch_target_payload=fetch_dokploy_target_payload_for_setup,
        )
    route_domain_ids = (
        list(result.route_domain_ids)
        if isinstance(result, DokployComposeDomainReconcileResult)
        else []
    )
    if apply_changes and request.operation == "create-compose" and request.runtime_port:
        for domain in request.domains:
            route_domain_ids.append(
                dokploy_compose.ensure_compose_web_domain_route(
                    host=host,
                    token=token,
                    compose_id=result.target_id_record.target_id,
                    domain_host=domain,
                    runtime_port=request.runtime_port,
                )
            )
    return {
        "mode": request.mode,
        "operation": request.operation,
        "context": request.context,
        "instance": request.instance,
        "applied": apply_changes,
        "route_domain_ids": route_domain_ids,
        "setup": dokploy_target_setup_result_payload(result),
    }


def ensure_dokploy_target_setup_context_is_not_historical(
    *,
    record_store: PostgresRecordStore,
    context: str,
) -> None:
    historical_owners = sorted(
        profile.product
        for profile in record_store.list_product_profile_records()
        if context in profile.historical_contexts
    )
    if historical_owners:
        raise ValueError(
            "Dokploy target setup cannot write historical product context "
            f"{context}; owned as history by {', '.join(historical_owners)}."
        )


def _execute_dokploy_compose_domain_reconcile(
    *,
    record_store: PostgresRecordStore,
    request: DokployTargetSetupEnvelope,
    host: str,
    token: str,
    apply_changes: bool,
) -> DokployComposeDomainReconcileResult:
    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as error:
        raise ValueError(
            "Dokploy compose domain reconciliation requires tracked target records."
        ) from error
    if target_record.target_type != "compose":
        raise ValueError("Dokploy compose domain reconciliation requires a compose target.")
    runtime_port = request.runtime_port
    if runtime_port is None:
        raise ValueError("Dokploy compose domain reconciliation requires runtime_port.")
    requested_domains = tuple(dict.fromkeys(request.domains))
    route_domain_ids: list[str] = []
    if apply_changes:
        for domain in requested_domains:
            route_domain_ids.append(
                dokploy_compose.ensure_compose_web_domain_route(
                    host=host,
                    token=token,
                    compose_id=target_id_record.target_id,
                    domain_host=domain,
                    runtime_port=runtime_port,
                )
            )
        merged_domains = tuple(dict.fromkeys((*target_record.domains, *requested_domains)))
        if merged_domains != target_record.domains:
            target_record = target_record.model_copy(
                update={
                    "domains": merged_domains,
                    "updated_at": utc_now_timestamp(),
                    "source_label": ("service:dokploy-targets:setup:reconcile-compose-domain"),
                }
            )
            record_store.write_dokploy_target_record(target_record)
    return DokployComposeDomainReconcileResult(
        applied=apply_changes,
        target_record=target_record,
        target_id_record=target_id_record,
        domains=requested_domains,
        runtime_port=runtime_port,
        route_domain_ids=tuple(route_domain_ids),
        warnings=()
        if apply_changes
        else ("dry run only; Dokploy compose domain routes were not reconciled",),
    )


def _execute_dokploy_compose_domain_prune(
    *,
    record_store: PostgresRecordStore,
    request: DokployTargetSetupEnvelope,
    host: str,
    token: str,
    apply_changes: bool,
) -> DokployComposeDomainPruneResult:
    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as error:
        raise ValueError("Dokploy compose domain prune requires tracked target records.") from error
    if target_record.target_type != "compose":
        raise ValueError("Dokploy compose domain prune requires a compose target.")

    requested_domains = tuple(dict.fromkeys(request.domains))
    provider_domains = fetch_dokploy_compose_domains_for_target_setup(
        host=host,
        token=token,
        compose_id=target_id_record.target_id,
    )
    domains_by_host: dict[str, list[str]] = {domain: [] for domain in requested_domains}
    for provider_domain in provider_domains:
        matching_host = next(
            (
                domain
                for domain in requested_domains
                if _dokploy_domain_matches_tracked_compose_web_route(
                    provider_domain=provider_domain,
                    compose_id=target_id_record.target_id,
                    domain_host=domain,
                )
            ),
            "",
        )
        if not matching_host:
            continue
        domain_id = str(provider_domain.get("domainId") or "").strip()
        if not domain_id:
            raise ValueError(f"Dokploy domain {matching_host} is missing domainId.")
        domains_by_host[matching_host].append(domain_id)

    matched_domain_ids = tuple(
        domain_id for domain in requested_domains for domain_id in domains_by_host.get(domain, ())
    )
    missing_domains = tuple(
        domain for domain in requested_domains if not domains_by_host.get(domain)
    )
    deleted_domain_ids: list[str] = []
    if apply_changes:
        for domain_id in matched_domain_ids:
            delete_dokploy_domain_for_target_setup(host=host, token=token, domain_id=domain_id)
            deleted_domain_ids.append(domain_id)
        remaining_domains = tuple(
            domain for domain in target_record.domains if domain not in requested_domains
        )
        if remaining_domains != target_record.domains:
            target_record = target_record.model_copy(
                update={
                    "domains": remaining_domains,
                    "updated_at": utc_now_timestamp(),
                    "source_label": "service:dokploy-targets:setup:prune-compose-domain",
                }
            )
            record_store.write_dokploy_target_record(target_record)

    warnings: list[str] = []
    if not apply_changes:
        warnings.append("dry run only; Dokploy compose domain routes were not pruned")
    if missing_domains:
        warnings.append(
            "requested domains were not present on the tracked compose target: "
            + ", ".join(missing_domains)
        )
    return DokployComposeDomainPruneResult(
        applied=apply_changes,
        target_record=target_record,
        target_id_record=target_id_record,
        domains=requested_domains,
        matched_domain_ids=matched_domain_ids,
        deleted_domain_ids=tuple(deleted_domain_ids),
        missing_domains=missing_domains,
        warnings=tuple(warnings),
    )
