"""An application testing lane's own hostname at an existing protected edge."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane.authz_candidate_preparation import (
    is_exclusive_product_lane_context,
    product_context_owners,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import TESTING_PRIVATE_INGRESS_ROUTE
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.dokploy import api, source
from control_plane.ingress_route_scope import validate_ingress_route_instance_scope
from control_plane.npmplus import NpmplusClient, NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.testing_public_ingress import _certificate_current, _covers, _host_payload
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = TESTING_PRIVATE_INGRESS_ROUTE
_PREFIX = "testing-private-ingress-"


class TestingPrivateIngressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    endpoint_key: str = Field(min_length=1)
    edge_endpoint_key: str = Field(min_length=1)
    access_list_id: int = Field(gt=0)
    certificate_id: int = Field(gt=0)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    operation: Literal["create", "disable"] = "create"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    reason: str = Field(min_length=1)


@dataclass
class TestingPrivateIngressAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: TestingPrivateIngressRequest
    trace_id: str
    client: NpmplusClient

    def target_key(self) -> str:
        return dokploy_generic_web_provider_target_key(
            self.store.read_provider_target_record(
                context_name=self.request.context, instance_name="testing"
            )
        )

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {"product": self.product, **self.request.model_dump(exclude={"mode", "reason"})}
        )

    def _authority(self) -> dict[str, object]:
        profile = self.store.read_product_profile_record(self.product)
        if (
            profile.lifecycle_state != "active"
            or profile.public_website != "none"
            or not is_exclusive_product_lane_context(
                context=self.request.context,
                product=self.product,
                owners=product_context_owners(self.store),
            )
        ):
            raise ValueError("Private ingress requires an active exclusive no-website lane.")
        endpoint = self.store.read_private_health_endpoint_record(self.request.endpoint_key)
        url = urlsplit(endpoint.url)
        hostname = url.hostname or ""
        if (
            (endpoint.product, endpoint.context, endpoint.instance, endpoint.status)
            != (self.product, self.request.context, "testing", "active")
            or url.scheme != "https"
            or url.port not in {None, 443}
            or url.path != "/health"
            or url.query
            or url.fragment
            or url.username is not None
            or url.password is not None
            or not hostname
            or any(character in hostname for character in "*~:/")
            or endpoint.tls_ca_pem
        ):
            raise ValueError("Private ingress requires the lane's active HTTPS health endpoint.")
        validate_ingress_route_instance_scope(
            profile=profile,
            context=self.request.context,
            instance="testing",
            requested_domains=(hostname,),
            private_endpoints=self.store.list_private_health_endpoint_records(),
            access_list_id=self.request.access_list_id,
        )
        target = self.store.read_dokploy_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.request.context, instance_name="testing"
        )
        provider = self.store.read_provider_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        if (
            target.target_type != "application"
            or provider.provider_id != "dokploy"
            or provider.target_id != target_id.target_id
            or provider.target_category != "application"
            or target.public_hosts
            or target.pending_public_host_effects
            or set(target.domains) - {hostname}
        ):
            raise ValueError("Private ingress requires an exclusive application testing target.")
        for other in self.store.list_provider_target_records():
            if other.target_id == provider.target_id and (other.context, other.instance) != (
                provider.context,
                provider.instance,
            ):
                raise ValueError("The testing application is shared with another lane.")
        for other_target in self.store.list_dokploy_target_records():
            if (other_target.context, other_target.instance) != (
                target.context,
                target.instance,
            ) and hostname in (
                *other_target.domains,
                *other_target.public_hosts,
                *(effect.host for effect in other_target.pending_public_host_effects),
            ):
                raise ValueError("The hostname belongs to another lane.")
        edge = self.store.read_edge_endpoint_record(self.request.edge_endpoint_key)
        if edge.status != "active" or edge.upstream_scheme != "http":
            raise ValueError("Private testing requires an active HTTP application edge.")
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        actual = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=target_id.target_id
        )
        server_id = actual.get("serverId")
        if actual.get("applicationId") != target_id.target_id or not isinstance(server_id, str):
            raise ValueError("Testing application placement is unavailable.")
        server = api.as_json_object(
            api.dokploy_request(
                host=host, token=token, path="/api/server.one", query={"serverId": server_id}
            )
        )
        if server is None or (
            server.get("serverId"),
            server.get("name"),
            server.get("ipAddress"),
        ) != (server_id, edge.server_name, edge.upstream_host):
            raise ValueError("The edge does not reach this application's actual server.")
        certificate = self.client.get_certificate(self.request.certificate_id)
        if (
            certificate.id != self.request.certificate_id
            or not _certificate_current(certificate)
            or not any(
                name.startswith("*.") and _covers(name, hostname)
                for name in certificate.domain_names
            )
        ):
            raise ValueError("Use an existing current wildcard certificate covering this hostname.")
        return {
            "profile": profile.model_dump(mode="json"),
            "endpoint": endpoint.model_dump(mode="json"),
            "edge": edge.model_dump(mode="json"),
            "target": target.model_dump(mode="json", exclude={"env"}),
            "target_id": target_id.model_dump(mode="json"),
            "provider": provider.model_dump(mode="json", exclude={"provider_evidence"}),
            "hostname": hostname,
            "application_id": target_id.target_id,
            "runtime_port": profile.runtime_port,
            "server_id": server_id,
            "certificate": certificate.model_dump(mode="json"),
        }

    def _desired(self, authority: dict[str, object]) -> NpmplusProxyHostPayload:
        edge = self.store.read_edge_endpoint_record(self.request.edge_endpoint_key)
        return NpmplusProxyHostPayload(
            domain_names=(str(authority["hostname"]),),
            forward_host=edge.upstream_host,
            forward_scheme=edge.upstream_scheme,
            forward_port=edge.upstream_port,
            certificate_id=self.request.certificate_id,
            access_list_id=self.request.access_list_id,
            ssl_forced=True,
            advanced_config="# launchplane private testing "
            + canonical_json_sha256(
                {
                    "product": self.product,
                    "context": self.request.context,
                    "endpoint": self.request.endpoint_key,
                    "edge": self.request.edge_endpoint_key,
                    "application": authority["application_id"],
                }
            )
            + "\n",
            enabled=self.request.operation == "create",
        )

    def _owned(
        self,
        candidate: NpmplusProxyHost,
        desired: NpmplusProxyHostPayload,
        authority: dict[str, object],
        recovery_key: str = "",
    ) -> bool:
        """Only a previous successful receipt can authorize disabling a route."""
        for audit in self.store.list_ingress_route_audit_records(
            product=self.product, context_name=self.request.context
        ):
            if (
                audit.record_id.startswith(_PREFIX)
                and (
                    audit.status == "applied"
                    or (
                        audit.status == "pending"
                        and bool(recovery_key)
                        and audit.idempotency_key == recovery_key
                        and audit.record_id == _PREFIX + self.request.reviewed_plan_sha256
                        and audit.testing_authority_sha256 == canonical_json_sha256(authority)
                    )
                )
                and audit.provider_host_id
                in ({None, candidate.id} if recovery_key else {candidate.id})
                and audit.requested_domains == desired.domain_names
                and audit.edge_endpoint_key == self.request.edge_endpoint_key
                and audit.provider_route_sha256 == canonical_json_sha256(_host_payload(candidate))
            ):
                return True
        return False

    def _snapshot(
        self,
        recovery_key: str = "",
    ) -> tuple[dict[str, object], NpmplusProxyHostPayload, NpmplusProxyHost | None]:
        authority = self._authority()
        desired = self._desired(authority)
        desired.to_api_payload()
        hostname = desired.domain_names[0]
        hosts = []
        for candidate in self.client.list_proxy_hosts():
            for name in candidate.domain_names:
                if (
                    name.startswith("~")
                    or name == hostname
                    or (name.startswith("*.") and hostname.endswith(name[1:]))
                ):
                    hosts.append(candidate)
                    break
        if len(hosts) > 1:
            raise ValueError("The testing edge hostname is ambiguous.")
        existing = self.client.get_proxy_host(hosts[0].id, public_only=True) if hosts else None
        if existing is not None and not self._owned(existing, desired, authority, recovery_key):
            raise ValueError("The existing edge route has no matching private testing receipt.")
        for name in self.client.list_other_host_domains():
            if (
                name == hostname
                or name.startswith("~")
                or (name.startswith("*.") and hostname.endswith(name[1:]))
            ):
                raise ValueError("Another edge host type owns the testing hostname.")
        if self.request.operation == "disable" and existing is None:
            raise ValueError("There is no owned private testing route to disable.")
        if existing is not None and _host_payload(existing) != _host_payload(
            desired.model_copy(update={"enabled": existing.enabled})
        ):
            raise ValueError("Private route configuration changed; do not overwrite it.")
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        domains = api.fetch_dokploy_application_domains(
            host=host, token=token, application_id=str(authority["application_id"]), strict=True
        )
        domain_payload: api.JsonObject = {
            "host": hostname,
            "path": "/",
            "internalPath": "/",
            "port": int(str(authority["runtime_port"])),
            "https": False,
            "applicationId": str(authority["application_id"]),
            "certificateType": "none",
            "customCertResolver": None,
            "composeId": None,
            "serviceName": None,
            "domainType": "application",
            "previewDeploymentId": None,
            "stripPath": False,
        }
        if len(domains) > 1 or any(
            not domain.get("domainId")
            or any(domain.get(key) != value for key, value in domain_payload.items())
            for domain in domains
        ):
            raise ValueError("The application's hostname authority is foreign or changed.")
        return (
            {
                "authority": authority,
                "desired": desired.model_dump(mode="json"),
                "existing": _host_payload(existing) if existing else None,
                "domain_payload": domain_payload,
                "domain_present": bool(domains),
            },
            desired,
            existing,
        )

    def plan(self) -> dict[str, object]:
        snapshot, _, _ = self._snapshot()
        return self._result(canonical_json_sha256(snapshot), applied=False)

    def _result(self, digest: str, *, applied: bool) -> dict[str, object]:
        return {
            "product": self.product,
            "context": self.request.context,
            "instance": "testing",
            "endpoint_key": self.request.endpoint_key,
            "operation": self.request.operation,
            "plan_sha256": digest,
            "applied": applied,
            "deployment_performed": False,
            "transport_verified": False,
        }

    def _audit(
        self,
        actual: NpmplusProxyHost | None,
        provider_key: str,
        *,
        desired: NpmplusProxyHostPayload,
        authority: dict[str, object],
    ) -> None:
        payload = actual or desired
        self.store.write_ingress_route_audit_record(
            IngressRouteAuditRecord(
                record_id=_PREFIX + self.request.reviewed_plan_sha256,
                product=self.product,
                context=self.request.context,
                provider="npmplus",
                mode="apply",
                status="applied" if actual else "pending",
                dry_run=False,
                requested_domains=payload.domain_names,
                edge_endpoint_key=self.request.edge_endpoint_key,
                tls_owner="provider",
                provider_certificate_ref=str(payload.certificate_id),
                provider_route_sha256=canonical_json_sha256(_host_payload(payload)),
                testing_authority_sha256=canonical_json_sha256(authority),
                provider_host_id=actual.id if actual else None,
                operations=(
                    IngressRouteAuditOperation(
                        action=self.request.operation,
                        host_id=actual.id if actual else None,
                        domain_names=payload.domain_names,
                        requires_apply=False,
                    ),
                ),
                trace_id=self.trace_id,
                idempotency_key=provider_key,
                reason=self.request.reason,
                recorded_at=utc_now_timestamp(),
            )
        )

    def readback(self) -> dict[str, object]:
        snapshot, desired, existing = self._snapshot()
        if (
            existing is None
            or not snapshot["domain_present"]
            or (_host_payload(existing) != _host_payload(desired))
        ):
            raise ValueError("The private hostname is not independently verified.")
        audit = self.store.read_ingress_route_audit_record(
            _PREFIX + self.request.reviewed_plan_sha256
        )
        if (
            audit.product != self.product
            or audit.context != self.request.context
            or audit.status != "applied"
            or audit.provider_host_id != existing.id
            or audit.requested_domains != desired.domain_names
            or audit.edge_endpoint_key != self.request.edge_endpoint_key
            or audit.provider_route_sha256 != canonical_json_sha256(_host_payload(existing))
        ):
            raise ValueError("Keep the exact completed private hostname receipt.")
        return self._result(self.request.reviewed_plan_sha256, applied=True)

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        started = False
        try:
            snapshot, desired, existing = self._snapshot()
            if canonical_json_sha256(snapshot) != self.request.reviewed_plan_sha256:
                raise ValueError("The reviewed private hostname plan changed.")
            target = self.store.read_dokploy_target_record(
                context_name=self.request.context, instance_name="testing"
            )
            target_id = self.store.read_dokploy_target_id_record(
                context_name=self.request.context, instance_name="testing"
            )
            provider = self.store.read_provider_target_record(
                context_name=self.request.context, instance_name="testing"
            )
            profile = self.store.read_product_profile_record(self.product)
            host, token = source.read_dokploy_config(control_plane_root=self.root)

            actual_hosts: list[NpmplusProxyHost] = []
            self._audit(
                None,
                provider_operation_key,
                desired=desired,
                authority=cast(dict[str, object], snapshot["authority"]),
            )

            def effect(checkpoint: Callable[[str, str, int], None]) -> None:
                nonlocal started
                lease.assert_current()
                if self._snapshot()[0] != snapshot:
                    raise ValueError("Private hostname authority changed before apply.")
                if not snapshot["domain_present"]:
                    lease.checkpoint_effect("create_private_testing_application_domain")
                    started = True
                    api.dokploy_request(
                        host=host,
                        token=token,
                        path="/api/domain.create",
                        method="POST",
                        payload=cast(api.JsonObject, snapshot["domain_payload"]),
                    )
                lease.checkpoint_effect("apply_private_testing_edge")
                started = True
                if existing is None:
                    actual = self.client.create_proxy_host(desired)
                elif desired.enabled != existing.enabled:
                    actual = (
                        self.client.enable_proxy_host(existing.id)
                        if desired.enabled
                        else self.client.disable_proxy_host(existing.id)
                    )
                else:
                    actual = existing
                actual = self.client.get_proxy_host(actual.id, public_only=True)
                if _host_payload(actual) != _host_payload(desired):
                    raise ValueError("The provider did not retain the exact protected route.")
                actual_hosts.append(actual)

            self.store.write_product_public_hosts_bundle(
                ProductAuthorityBundle(
                    dokploy_targets=(target,),
                    expected_product_profiles=(profile,),
                    required_context_owner=(self.product, self.request.context),
                ),
                expected_target=target,
                expected_target_id=target_id,
                expected_provider_target=provider,
                apply_provider=effect,
            )
            self._audit(
                actual_hosts[0],
                provider_operation_key,
                desired=desired,
                authority=cast(dict[str, object], snapshot["authority"]),
            )
            result = self.readback()
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            if started:
                raise ProviderMutationUnknownError(
                    "Retain the original private hostname key."
                ) from None
            raise ProviderMutationRejectedError(
                ValueError("Private hostname authority is unverified.")
            ) from error
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=provider_operation_response_payload(
                trace_id=self.trace_id,
                records={},
                result=result,
            ),
        )

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        try:
            try:
                result = self.readback()
            except (ValueError, FileNotFoundError):
                if not provider_effect_phase:
                    return ProviderObservation("absent", retry_safe=True)
                # Recover a lost final response only from the original pending
                # receipt and an exact independent match of both provider effects.
                # Missing or partial effects never trigger another provider write.
                snapshot, desired, existing = self._snapshot(provider_operation_key)
                if (
                    existing is None
                    or not snapshot["domain_present"]
                    or (_host_payload(existing) != _host_payload(desired))
                ):
                    return ProviderObservation("unknown")
                self._audit(
                    existing,
                    provider_operation_key,
                    desired=desired,
                    authority=cast(dict[str, object], snapshot["authority"]),
                )
                result = self.readback()
            return ProviderObservation(
                "present",
                response_payload=provider_operation_response_payload(
                    trace_id=self.trace_id,
                    records={},
                    result=result,
                ),
            )
        except (ValueError, FileNotFoundError, RuntimeError, OSError, click.ClickException):
            return ProviderObservation(
                "unknown" if provider_effect_phase else "absent",
                retry_safe=not provider_effect_phase,
            )
