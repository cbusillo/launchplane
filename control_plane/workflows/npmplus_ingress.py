from typing import Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.npmplus import (
    NpmplusAuthRequest,
    NpmplusForwardScheme,
    NpmplusLocationPayload,
    NpmplusProxyHost,
    NpmplusProxyHostPayload,
)


type NpmplusIngressMode = Literal["dry-run", "apply"]
type NpmplusIngressOperationAction = Literal["create", "update", "enable", "disable", "no-op"]
type NpmplusIngressChangeCategory = Literal[
    "route",
    "upstream",
    "certificate",
    "tls",
    "access_list",
    "identity_access",
    "provider_options",
    "enabled",
]
type NpmplusIngressStatus = Literal["planned", "applied", "unchanged"]
type IngressIdentityAccessMode = Literal["none", "forward-auth"]
type IngressIdentityAccessProvider = Literal["none", "anubis", "tinyauth", "authelia", "authentik"]


_CHANGE_CATEGORY_ORDER: tuple[NpmplusIngressChangeCategory, ...] = (
    "route",
    "upstream",
    "certificate",
    "tls",
    "access_list",
    "identity_access",
    "provider_options",
    "enabled",
)
_CHANGE_CATEGORY_FIELDS: dict[NpmplusIngressChangeCategory, frozenset[str]] = {
    "route": frozenset({"domain_names"}),
    "upstream": frozenset({"forward_scheme", "forward_host", "forward_port"}),
    "certificate": frozenset({"certificate_id"}),
    "tls": frozenset(
        {
            "ssl_forced",
            "hsts_enabled",
            "hsts_subdomains",
            "trust_forwarded_proto",
            "http2_support",
            "npmplus_http3_support",
        }
    ),
    "access_list": frozenset({"access_list_id"}),
    "identity_access": frozenset({"npmplus_auth_request"}),
    "provider_options": frozenset(
        {
            "advanced_config",
            "locations",
            "npmplus_noindex",
            "npmplus_crowdsec_appsec",
            "npmplus_proxy_request_buffering",
            "npmplus_proxy_response_buffering",
            "npmplus_upstream_compression",
            "npmplus_fancyindex",
            "npmplus_x_frame_options",
        }
    ),
    "enabled": frozenset({"enabled"}),
}


class NpmplusIngressClient(Protocol):
    def list_proxy_hosts(self) -> tuple[NpmplusProxyHost, ...]: ...

    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost: ...

    def update_proxy_host(
        self, *, host_id: int, payload: NpmplusProxyHostPayload
    ) -> NpmplusProxyHost: ...

    def disable_proxy_host(self, host_id: int) -> NpmplusProxyHost: ...

    def enable_proxy_host(self, host_id: int) -> NpmplusProxyHost: ...


class IngressIdentityAccessBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mode: IngressIdentityAccessMode = "none"
    provider: IngressIdentityAccessProvider = "none"
    send_basic_auth: bool = False

    @model_validator(mode="after")
    def _validate_binding(self) -> "IngressIdentityAccessBinding":
        if self.schema_version != 1:
            raise ValueError("Unsupported ingress identity/access schema version")
        if self.mode == "none":
            if self.provider != "none" or self.send_basic_auth:
                raise ValueError("Disabled ingress identity/access binding must use provider=none")
        elif self.provider == "none":
            raise ValueError("Forward-auth ingress identity/access binding requires a provider")
        return self

    def to_npmplus_auth_request(self) -> NpmplusAuthRequest:
        if self.mode == "none":
            return "none"
        if self.provider == "authentik" and self.send_basic_auth:
            return "authentik-send-basic-auth"
        if self.provider in {"anubis", "tinyauth", "authelia", "authentik"}:
            return self.provider
        raise ValueError("Unsupported ingress identity/access provider")


def identity_access_from_npmplus_auth_request(
    auth_request: NpmplusAuthRequest,
) -> IngressIdentityAccessBinding:
    if auth_request == "none":
        return IngressIdentityAccessBinding()
    if auth_request == "authentik":
        return IngressIdentityAccessBinding(mode="forward-auth", provider="authentik")
    if auth_request == "authentik-send-basic-auth":
        return IngressIdentityAccessBinding(
            mode="forward-auth", provider="authentik", send_basic_auth=True
        )
    return IngressIdentityAccessBinding(mode="forward-auth", provider=auth_request)


class NpmplusIngressRouteDesiredState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_names: tuple[str, ...]
    forward_scheme: NpmplusForwardScheme
    forward_host: str
    forward_port: int | None = Field(default=None, ge=1, le=65535)
    certificate_id: int | Literal["new"] = 0
    ssl_forced: bool = True
    hsts_enabled: bool = False
    hsts_subdomains: bool = False
    trust_forwarded_proto: bool = False
    http2_support: bool = True
    npmplus_http3_support: bool = True
    access_list_id: int = Field(default=0, ge=0)
    npmplus_noindex: bool = False
    npmplus_crowdsec_appsec: bool = False
    npmplus_proxy_request_buffering: bool = False
    npmplus_proxy_response_buffering: bool = False
    npmplus_upstream_compression: bool = False
    npmplus_fancyindex: bool = False
    npmplus_x_frame_options: Literal["DENY", "SAMEORIGIN", "upstream", "none"] = "SAMEORIGIN"
    npmplus_auth_request: NpmplusAuthRequest = "none"
    identity_access: IngressIdentityAccessBinding | None = None
    advanced_config: str = ""
    enabled: bool = True
    locations: tuple[NpmplusLocationPayload, ...] = ()

    @model_validator(mode="after")
    def _validate_identity_access(self) -> "NpmplusIngressRouteDesiredState":
        if self.identity_access is None:
            return self
        mapped_auth_request = self.identity_access.to_npmplus_auth_request()
        if "npmplus_auth_request" in self.model_fields_set and self.npmplus_auth_request not in {
            "none",
            mapped_auth_request,
        }:
            raise ValueError("ingress identity_access conflicts with npmplus_auth_request")
        self.npmplus_auth_request = mapped_auth_request
        return self

    def to_proxy_host_payload(self) -> NpmplusProxyHostPayload:
        return NpmplusProxyHostPayload.model_validate(
            self.model_dump(mode="json", exclude={"identity_access"})
        )


class NpmplusIngressApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mode: NpmplusIngressMode = "dry-run"
    route: NpmplusIngressRouteDesiredState
    expected_host_id: int | None = Field(default=None, ge=1)
    require_exact_expected_host_domains: bool = False
    allow_create: bool = True
    allow_update: bool = True
    allow_enable_disable: bool = True
    reason: str = ""

    @model_validator(mode="after")
    def _validate_schema_version(self) -> "NpmplusIngressApplyRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported NPMplus ingress apply schema version")
        if not self.reason.strip():
            raise ValueError("NPMplus ingress apply requests require a reason")
        if self.require_exact_expected_host_domains and self.expected_host_id is None:
            raise ValueError(
                "NPMplus ingress exact expected-host domain checks require expected_host_id"
            )
        return self


class NpmplusIngressOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: NpmplusIngressOperationAction
    host_id: int | None = None
    domain_names: tuple[str, ...]
    requires_apply: bool
    change_categories: tuple[NpmplusIngressChangeCategory, ...] = ()


class NpmplusIngressApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: NpmplusIngressStatus
    dry_run: bool
    operations: tuple[NpmplusIngressOperation, ...]
    proxy_host: NpmplusProxyHost | None = None


def apply_npmplus_ingress_route(
    *,
    client: NpmplusIngressClient,
    request: NpmplusIngressApplyRequest,
) -> NpmplusIngressApplyResult:
    desired_payload = request.route.to_proxy_host_payload()
    existing_host = find_npmplus_proxy_host_by_domains(
        client.list_proxy_hosts(),
        domain_names=desired_payload.domain_names,
        expected_host_id=request.expected_host_id,
        require_exact_expected_host_domains=request.require_exact_expected_host_domains,
    )
    operations = _plan_operations(
        existing_host=existing_host,
        desired_payload=desired_payload,
        request=request,
    )

    if request.mode == "dry-run":
        return NpmplusIngressApplyResult(
            status="unchanged" if _is_no_op_plan(operations) else "planned",
            dry_run=True,
            operations=operations,
            proxy_host=existing_host,
        )

    final_host = existing_host
    for operation in operations:
        if operation.action == "create":
            final_host = client.create_proxy_host(desired_payload)
        elif operation.action == "update":
            if final_host is None:
                raise click.ClickException("Cannot update missing NPMplus proxy host.")
            final_host = client.update_proxy_host(
                host_id=final_host.id,
                payload=desired_payload,
            )
        elif operation.action == "enable":
            if final_host is None:
                raise click.ClickException("Cannot enable missing NPMplus proxy host.")
            if final_host.enabled == desired_payload.enabled:
                continue
            final_host = client.enable_proxy_host(final_host.id)
        elif operation.action == "disable":
            if final_host is None:
                raise click.ClickException("Cannot disable missing NPMplus proxy host.")
            if final_host.enabled == desired_payload.enabled:
                continue
            final_host = client.disable_proxy_host(final_host.id)

    return NpmplusIngressApplyResult(
        status="unchanged" if _is_no_op_plan(operations) else "applied",
        dry_run=False,
        operations=operations,
        proxy_host=final_host,
    )


def find_npmplus_proxy_host_by_domains(
    proxy_hosts: tuple[NpmplusProxyHost, ...],
    *,
    domain_names: tuple[str, ...],
    expected_host_id: int | None = None,
    require_exact_expected_host_domains: bool = False,
) -> NpmplusProxyHost | None:
    desired_domains = set(_normalize_domains(domain_names))
    domain_matches = tuple(
        host for host in proxy_hosts if desired_domains.intersection(host.domain_names)
    )
    if require_exact_expected_host_domains and expected_host_id is None:
        raise click.ClickException(
            "Exact NPMplus expected-host domain checks require expected_host_id."
        )
    if expected_host_id is not None:
        expected_matches = tuple(host for host in proxy_hosts if host.id == expected_host_id)
        if not expected_matches:
            raise click.ClickException(
                f"Expected NPMplus proxy host {expected_host_id} was not found."
            )
        expected_host = expected_matches[0]
        if not desired_domains.intersection(expected_host.domain_names):
            expected_domains = ", ".join(expected_host.domain_names)
            desired_domain_list = ", ".join(sorted(desired_domains))
            raise click.ClickException(
                f"Expected NPMplus proxy host {expected_host_id} domains "
                f"({expected_domains}) do not match requested domains ({desired_domain_list})."
            )
        if require_exact_expected_host_domains and desired_domains != set(
            expected_host.domain_names
        ):
            expected_domains = ", ".join(expected_host.domain_names)
            desired_domain_list = ", ".join(sorted(desired_domains))
            raise click.ClickException(
                f"Expected NPMplus proxy host {expected_host_id} domains "
                f"({expected_domains}) must exactly match requested domains "
                f"({desired_domain_list})."
            )
        mismatched_domain_matches = tuple(
            host for host in domain_matches if host.id != expected_host_id
        )
        if mismatched_domain_matches:
            match_ids = ", ".join(str(host.id) for host in mismatched_domain_matches)
            raise click.ClickException(
                "NPMplus route domains match proxy host(s) "
                f"{match_ids}, not expected host {expected_host_id}."
            )
        return expected_host
    if not domain_matches:
        return None
    if len(domain_matches) > 1:
        match_ids = ", ".join(str(host.id) for host in domain_matches)
        raise click.ClickException(
            f"NPMplus route domains match multiple proxy hosts: {match_ids}."
        )
    return domain_matches[0]


def _plan_operations(
    *,
    existing_host: NpmplusProxyHost | None,
    desired_payload: NpmplusProxyHostPayload,
    request: NpmplusIngressApplyRequest,
) -> tuple[NpmplusIngressOperation, ...]:
    operation_domains = desired_payload.domain_names
    if existing_host is None:
        if not request.allow_create:
            raise click.ClickException("NPMplus ingress create is not allowed for this request.")
        return (
            NpmplusIngressOperation(
                action="create",
                domain_names=operation_domains,
                requires_apply=True,
                change_categories=_create_change_categories(desired_payload),
            ),
        )

    operations: list[NpmplusIngressOperation] = []
    change_categories = _update_change_categories(existing_host, desired_payload)
    if change_categories:
        if not request.allow_update:
            raise click.ClickException("NPMplus ingress update is not allowed for this request.")
        operations.append(
            NpmplusIngressOperation(
                action="update",
                host_id=existing_host.id,
                domain_names=operation_domains,
                requires_apply=True,
                change_categories=change_categories,
            )
        )

    if existing_host.enabled != desired_payload.enabled:
        if not request.allow_enable_disable:
            raise click.ClickException(
                "NPMplus ingress enable/disable is not allowed for this request."
            )
        operations.append(
            NpmplusIngressOperation(
                action="enable" if desired_payload.enabled else "disable",
                host_id=existing_host.id,
                domain_names=operation_domains,
                requires_apply=True,
                change_categories=("enabled",),
            )
        )

    if operations:
        return tuple(operations)
    return (
        NpmplusIngressOperation(
            action="no-op",
            host_id=existing_host.id,
            domain_names=operation_domains,
            requires_apply=False,
        ),
    )


def _payload_matches(
    existing_host: NpmplusProxyHost,
    desired_payload: NpmplusProxyHostPayload,
) -> bool:
    return _comparable_payload(existing_host) == _comparable_payload(desired_payload)


def _create_change_categories(
    desired_payload: NpmplusProxyHostPayload,
) -> tuple[NpmplusIngressChangeCategory, ...]:
    categories: set[NpmplusIngressChangeCategory] = {
        "route",
        "upstream",
        "certificate",
        "tls",
        "provider_options",
    }
    if desired_payload.access_list_id != 0:
        categories.add("access_list")
    if desired_payload.npmplus_auth_request != "none":
        categories.add("identity_access")
    if not desired_payload.enabled:
        categories.add("enabled")
    return _ordered_change_categories(categories)


def _update_change_categories(
    existing_host: NpmplusProxyHost,
    desired_payload: NpmplusProxyHostPayload,
) -> tuple[NpmplusIngressChangeCategory, ...]:
    existing_comparison = _comparable_payload(existing_host)
    desired_comparison = _comparable_payload(desired_payload)
    categories = {
        category
        for category, fields in _CHANGE_CATEGORY_FIELDS.items()
        if any(existing_comparison.get(field) != desired_comparison.get(field) for field in fields)
    }
    if existing_comparison != desired_comparison and not categories:
        categories.add("provider_options")
    return _ordered_change_categories(categories)


def _ordered_change_categories(
    categories: set[NpmplusIngressChangeCategory],
) -> tuple[NpmplusIngressChangeCategory, ...]:
    return tuple(category for category in _CHANGE_CATEGORY_ORDER if category in categories)


def _comparable_payload(
    payload: NpmplusProxyHostPayload,
) -> dict[str, object]:
    comparable = payload.model_dump(mode="json", exclude={"enabled", "id", "meta"})
    locations = comparable.get("locations")
    if isinstance(locations, list):
        comparable["locations"] = [
            {key: value for key, value in location.items() if key != "id"}
            if isinstance(location, dict)
            else location
            for location in locations
        ]
    return comparable


def _is_no_op_plan(operations: tuple[NpmplusIngressOperation, ...]) -> bool:
    return len(operations) == 1 and operations[0].action == "no-op"


def _normalize_domains(domain_names: tuple[str, ...]) -> tuple[str, ...]:
    return NpmplusProxyHostPayload(
        domain_names=domain_names,
        forward_scheme="http",
        forward_host="127.0.0.1",
        forward_port=80,
    ).domain_names
