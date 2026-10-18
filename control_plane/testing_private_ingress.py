"""An application testing lane's own hostname at an existing protected edge."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import click
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from control_plane.authz_candidate_preparation import (
    is_exclusive_product_lane_context,
    product_context_owners,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import TESTING_PRIVATE_INGRESS_ROUTE
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
    TESTING_PRIVATE_INGRESS_AUDIT_PREFIX,
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
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
    generic_web_provider_target_key,
)
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = TESTING_PRIVATE_INGRESS_ROUTE
_PREFIX = TESTING_PRIVATE_INGRESS_AUDIT_PREFIX


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
        return f"testing-private-ingress:{self.product}:{self.request.context}"

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
        if self.request.operation == "disable":
            # Disabling the exact receipt-owned route must remain possible after
            # its application or edge placement has been replaced or removed.
            return {
                "profile": profile.model_dump(mode="json"),
                "endpoint": endpoint.model_dump(mode="json"),
                "hostname": hostname,
            }
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
        access_list = self.client.get_access_list(self.request.access_list_id)
        if access_list.id != self.request.access_list_id:
            raise ValueError("The existing access list does not match the request.")
        return {
            "profile": profile.model_dump(mode="json"),
            "endpoint": endpoint.model_dump(mode="json"),
            "edge": edge.model_dump(mode="json"),
            # Include the claim this operation commits, so a lost final response
            # has the same authority digest before and after that local commit.
            "target": target.model_copy(update={"domains": (hostname,)}).model_dump(
                mode="json", exclude={"env"}
            ),
            "target_id": target_id.model_dump(mode="json"),
            "provider": provider.model_dump(mode="json", exclude={"provider_evidence"}),
            "hostname": hostname,
            "application_id": target_id.target_id,
            "runtime_port": profile.runtime_port,
            "server_id": server_id,
            "certificate": certificate.model_dump(mode="json"),
            "access_list": access_list.model_dump(mode="json"),
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

    def _receipt_authority_sha256(self, authority: dict[str, object]) -> str:
        access_list = cast(dict[str, object], authority.get("access_list", {}))
        return canonical_json_sha256(
            {
                "product": self.product,
                "context": self.request.context,
                "instance": "testing",
                "endpoint_key": self.request.endpoint_key,
                "edge_endpoint_key": self.request.edge_endpoint_key,
                "hostname": authority["hostname"],
                "application_id": authority.get("application_id"),
                "runtime_port": authority.get("runtime_port"),
                "certificate_id": self.request.certificate_id,
                "access_list_id": self.request.access_list_id,
                "access_list_policy": {
                    key: access_list.get(key) for key in ("satisfy_any", "pass_auth")
                },
            }
        )

    def _pending_receipt(self, provider_key: str) -> IngressRouteAuditRecord | None:
        if not provider_key:
            return None
        try:
            audit = self.store.read_ingress_route_audit_record(
                _PREFIX + self.request.reviewed_plan_sha256
            )
        except FileNotFoundError:
            return None
        if (
            audit.product == self.product
            and audit.context == self.request.context
            and audit.status == "pending"
            and audit.idempotency_key == provider_key
        ):
            return audit
        return None

    def _pending_matches(
        self,
        provider_key: str,
        desired: NpmplusProxyHostPayload,
        authority: dict[str, object],
    ) -> bool:
        audit = self._pending_receipt(provider_key)
        return (
            audit is not None
            and audit.requested_domains == desired.domain_names
            and audit.edge_endpoint_key == self.request.edge_endpoint_key
            and audit.testing_authority_sha256 == self._receipt_authority_sha256(authority)
            and audit.provider_route_sha256 == canonical_json_sha256(_host_payload(desired))
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
                        and audit.record_id == _PREFIX + self.request.reviewed_plan_sha256
                        and self._pending_matches(recovery_key, desired, authority)
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
        hostname = str(authority["hostname"])
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
        if self.request.operation == "disable":
            if existing is None or existing.domain_names != (hostname,):
                raise ValueError("There is no owned private testing route to disable.")
            desired = NpmplusProxyHostPayload.model_validate(_host_payload(existing)).model_copy(
                update={"enabled": False}
            )
        else:
            desired = self._desired(authority)
        desired.to_api_payload()
        if existing is not None and not self._owned(existing, desired, authority, recovery_key):
            raise ValueError("The existing edge route has no matching private testing receipt.")
        for name in self.client.list_other_host_domains():
            if (
                name == hostname
                or name.startswith("~")
                or (name.startswith("*.") and hostname.endswith(name[1:]))
            ):
                raise ValueError("Another edge host type owns the testing hostname.")
        if existing is not None and _host_payload(existing) != _host_payload(
            desired.model_copy(update={"enabled": existing.enabled})
        ):
            raise ValueError("Private route configuration changed; do not overwrite it.")
        if self.request.operation == "disable":
            return (
                {
                    "authority": authority,
                    "desired": desired.model_dump(mode="json"),
                    "existing": _host_payload(existing) if existing else None,
                    "domain_present": True,
                },
                desired,
                existing,
            )
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
            or any(
                (domain.get(key) not in (None, "") if value is None else domain.get(key) != value)
                for key, value in domain_payload.items()
            )
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
                testing_authority_sha256=self._receipt_authority_sha256(authority),
                testing_application_id=str(authority.get("application_id", "")),
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
        # An uncertain edge operation retains its own reservation, without
        # holding ordinary deployments forever. Only active application-domain
        # mutation shares the testing event executor lock with deployments.
        prior_pending = self._pending_receipt(provider_operation_key)
        with ExitStack() as stack:
            locked_target_key = ""
            check: Callable[[], None] | None = None
            try:
                if self.request.operation == "create":
                    provider = self.store.read_provider_target_record(
                        context_name=self.request.context, instance_name="testing"
                    )
                    locked_target_key = dokploy_generic_web_provider_target_key(provider)
                    check = stack.enter_context(
                        self.store.testing_replacement_execution_lock(
                            testing_event_execution_lock_name(locked_target_key)
                        )
                    )
                    if check is None:
                        raise ValueError("The testing executor is busy.")
            except (ValueError, FileNotFoundError, RuntimeError, OSError, SQLAlchemyError) as error:
                if prior_pending:
                    raise ProviderMutationUnknownError(
                        "Keep the original private hostname reservation while retrying."
                    ) from error
                raise ProviderMutationRejectedError(
                    ValueError("The testing executor could not be acquired before effects.")
                ) from error

            def check_execution() -> None:
                if check is not None:
                    check()
                if self.request.operation == "create":
                    current = self.store.read_provider_target_record(
                        context_name=self.request.context, instance_name="testing"
                    )
                    if dokploy_generic_web_provider_target_key(current) != locked_target_key:
                        raise ValueError("The locked testing application changed.")

            return self._apply(
                provider_operation_key, lease, check_execution, prior_pending, locked_target_key
            )

    def _previous_effects_absent(
        self,
        audit: IngressRouteAuditRecord | None,
        *,
        locked_target_key: str = "",
        check_execution: Callable[[], None] | None = None,
    ) -> bool:
        """Read the original binding, even when current lane authority has changed.

        An empty inventory uses the same read-after-write assumption as missing-
        effect recovery. A busy executor, legacy receipt or failed read cannot
        establish absence. This never authorizes deleting an existing effect.
        """
        if (
            self.request.operation != "create"
            or audit is None
            or not audit.testing_application_id
            or len(audit.requested_domains) != 1
        ):
            return False
        hostname = audit.requested_domains[0]
        if not hostname or any(character in hostname for character in "*~:/"):
            return False
        original_target_key = generic_web_provider_target_key(
            provider_id="dokploy",
            context=audit.context,
            instance="testing",
            target_type="application",
            target_id=audit.testing_application_id,
        )
        with ExitStack() as stack:
            if original_target_key != locked_target_key:
                check_execution = stack.enter_context(
                    self.store.testing_replacement_execution_lock(
                        testing_event_execution_lock_name(original_target_key)
                    )
                )
            if check_execution is None:
                return False
            check_execution()

            def overlaps(name: str) -> bool:
                name = name.strip().lower()
                if not name:
                    raise ValueError("A provider hostname is unavailable.")
                return (
                    name == hostname
                    or name.startswith("~")
                    or (name.startswith("*.") and hostname.endswith(name[1:]))
                )

            if any(
                overlaps(name)
                for route in self.client.list_proxy_hosts()
                for name in route.domain_names
            ) or any(overlaps(name) for name in self.client.list_other_host_domains()):
                return False
            host, token = source.read_dokploy_config(control_plane_root=self.root)
            domains = api.fetch_dokploy_application_domains(
                host=host,
                token=token,
                application_id=audit.testing_application_id,
                strict=True,
            )
            for domain in domains:
                name = domain.get("host")
                if not isinstance(name, str) or overlaps(name):
                    return False
            check_execution()
            return True

    def _replan_outcome(self) -> ProviderMutationOutcome:
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=provider_operation_response_payload(
                trace_id=self.trace_id,
                records={},
                result={
                    **self._result(self.request.reviewed_plan_sha256, applied=False),
                    "replan_required": True,
                    "previous_effects_absent": True,
                },
            ),
            durable=True,
            provider_effect_performed=False,
        )

    def _write_application_claim(
        self, effect: Callable[[Callable[[str, str, int], None]], None]
    ) -> None:
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
        endpoint = self.store.read_private_health_endpoint_record(self.request.endpoint_key)
        hostname = urlsplit(endpoint.url).hostname
        if not hostname:
            raise ValueError("Private testing hostname is unavailable.")
        self.store.write_product_public_hosts_bundle(
            ProductAuthorityBundle(
                dokploy_targets=(target.model_copy(update={"domains": (hostname,)}),),
                expected_product_profiles=(profile,),
                required_context_owner=(self.product, self.request.context),
            ),
            expected_target=target,
            expected_target_id=target_id,
            expected_provider_target=provider,
            apply_provider=effect,
        )

    def _apply(
        self,
        provider_operation_key: str,
        lease: ProviderOperationLease,
        check_execution: Callable[[], None],
        prior_pending: IngressRouteAuditRecord | None,
        locked_target_key: str,
    ) -> ProviderMutationOutcome:
        started = False
        try:
            snapshot, desired, existing = self._snapshot(provider_operation_key)
            resuming_missing_effects = (
                self.request.operation == "create"
                and existing is None
                and self._pending_matches(
                    provider_operation_key, desired, cast(dict[str, object], snapshot["authority"])
                )
            )
            if (
                canonical_json_sha256(snapshot) != self.request.reviewed_plan_sha256
                and not resuming_missing_effects
            ):
                raise ValueError("The reviewed private hostname plan changed.")
            actual_hosts: list[NpmplusProxyHost] = []
            self._audit(
                None,
                provider_operation_key,
                desired=desired,
                authority=cast(dict[str, object], snapshot["authority"]),
            )

            def effect(checkpoint: Callable[[str, str, int], None]) -> None:
                nonlocal started
                check_execution()
                lease.assert_current()
                if self._snapshot()[0] != snapshot:
                    raise ValueError("Private hostname authority changed before apply.")
                if not snapshot["domain_present"]:
                    host, token = source.read_dokploy_config(control_plane_root=self.root)
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

            if self.request.operation == "disable":
                effect(lambda *_: None)
            else:
                self._write_application_claim(effect)
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
            SQLAlchemyError,
            click.ClickException,
        ) as error:
            if not started and prior_pending is not None:
                try:
                    lease.assert_current()
                    if self._previous_effects_absent(
                        prior_pending,
                        locked_target_key=locked_target_key,
                        check_execution=check_execution,
                    ):
                        lease.assert_current()
                        return self._replan_outcome()
                except (
                    ValueError,
                    FileNotFoundError,
                    RuntimeError,
                    OSError,
                    SQLAlchemyError,
                    click.ClickException,
                ):
                    pass
            if started or prior_pending:
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
                # Observation never performs provider effects. Exact completed
                # effects can be adopted; independently proven absence can let
                # the original request resume only its missing edge.
                snapshot, desired, existing = self._snapshot(provider_operation_key)
                if (
                    self.request.operation == "create"
                    and existing is None
                    and self._pending_matches(
                        provider_operation_key,
                        desired,
                        cast(dict[str, object], snapshot["authority"]),
                    )
                ):
                    # Independent readback proves no edge exists, and the
                    # application domain is either absent or exactly ours.
                    # Resume only this original request's missing effects.
                    return ProviderObservation("absent", retry_safe=True)
                if (
                    existing is None
                    or not snapshot["domain_present"]
                    or (_host_payload(existing) != _host_payload(desired))
                ):
                    if self._previous_effects_absent(self._pending_receipt(provider_operation_key)):
                        return ProviderObservation("absent", retry_safe=True)
                    return ProviderObservation("unknown")
                if self.request.operation == "create":

                    def verify_claim(_: Callable[[str, str, int], None]) -> None:
                        if self._snapshot(provider_operation_key)[0] != snapshot:
                            raise ValueError("Private hostname changed before claim adoption.")

                    self._write_application_claim(verify_claim)
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
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            SQLAlchemyError,
            click.ClickException,
        ):
            try:
                if self._previous_effects_absent(self._pending_receipt(provider_operation_key)):
                    # The acquired original-key retry rechecks this evidence
                    # under its lease before recording a durable replan result.
                    return ProviderObservation("absent", retry_safe=True)
            except (
                ValueError,
                FileNotFoundError,
                RuntimeError,
                OSError,
                SQLAlchemyError,
                click.ClickException,
            ):
                pass
            return ProviderObservation(
                "unknown" if provider_effect_phase else "absent",
                retry_safe=not provider_effect_phase,
            )
