"""Publish one prepared testing hostname at its already-recorded public edge."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.public_hosts import retained_hostname_identity
from control_plane.contracts.disposable_lane import TESTING_PUBLIC_INGRESS_ROUTE
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.dokploy import api, source
from control_plane.npmplus import NpmplusCertificate, NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.odoo_testing_restore import generation_binding, read_current_testing_generation
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_response_payload,
)
from control_plane.route_binding_reconcile import (
    RouteBindingReconcileRequest,
    build_route_binding_candidate,
    provider_target_projection,
    route_binding_authority_projection,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.workflows.ship import utc_now_timestamp

ROUTE = TESTING_PUBLIC_INGRESS_ROUTE
_AUDIT_PREFIX = "testing-public-ingress-"


def _marker(digest: str) -> str:
    # A generated comment survives the provider's API; NPMplus discards legacy meta.
    return "# launchplane testing public ingress " + digest + "\n"


class TestingPublicIngressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class TestingPublicIngressClient(Protocol):
    def list_proxy_hosts(self) -> tuple[NpmplusProxyHost, ...]: ...

    def get_certificate(self, certificate_id: int) -> NpmplusCertificate: ...

    def list_other_host_domains(self) -> tuple[str, ...]: ...

    def get_proxy_host(self, host_id: int, *, public_only: bool = False) -> NpmplusProxyHost: ...

    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost: ...


def _covers(pattern: str, hostname: str) -> bool:
    pattern = pattern.lower().rstrip(".")
    return pattern == hostname or (
        pattern.startswith("*.")
        and hostname.endswith(pattern[1:])
        and hostname.count(".") == pattern.count(".")
    )


def _host_payload(host: NpmplusProxyHostPayload) -> dict[str, object]:
    return host.model_dump(mode="json", exclude={"id", "meta"})


def _certificate_current(certificate: NpmplusCertificate) -> bool:
    # NPMplus serializes its UTC SQL expiry without an offset on some versions.
    expiry = datetime.fromisoformat(certificate.expires_on.replace("Z", "+00:00"))
    return expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) > datetime.now(timezone.utc)


@dataclass
class TestingPublicIngressAdapter:
    root: Path
    store: PostgresRecordStore
    product: str
    request: TestingPublicIngressRequest
    trace_id: str
    client: TestingPublicIngressClient

    def target_key(self) -> str:
        return f"testing-public-ingress:{self.product}:{self.request.context}"

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {"operation": self.request.operation_id, "plan": self.request.reviewed_plan_sha256}
        )

    def _authority(self) -> tuple[dict[str, object], ProductAuthorityBundle]:
        generation = read_current_testing_generation(
            self.store, product=self.product, context=self.request.context, instance="testing"
        )
        if generation is None or generation.operation_id != self.request.operation_id:
            raise ValueError("The current testing generation does not match this operation.")
        profile = self.store.read_product_profile_record(self.product)
        lane = next(
            (
                lane
                for lane in profile.lanes
                if (lane.context, lane.instance) == (self.request.context, "testing")
            ),
            None,
        )
        if lane is None:
            raise ValueError("The current testing lane is not declared.")
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
            urlsplit(lane.base_url).scheme != "https"
            or urlsplit(lane.base_url).hostname != generation.domain_host
            or target.domains != (generation.domain_host,)
            or provider.target_id != generation.target_id
        ):
            raise ValueError("Testing hostname authority changed.")
        for other in self.store.list_dokploy_target_records():
            if (other.context, other.instance) != (target.context, target.instance) and (
                retained_hostname_identity(generation.domain_host)
                in {
                    retained_hostname_identity(host)
                    for host in (
                        *other.domains,
                        *other.public_hosts,
                        *(effect.host for effect in other.pending_public_host_effects),
                    )
                }
            ):
                raise ValueError("The testing hostname belongs to another target.")
        binding = self.store.read_route_binding_record(
            product=self.product, context_name=self.request.context, instance_name="testing"
        )
        if (
            binding.status != "active"
            or binding.source.source_kind != "service"
            or binding.ingress.provider != "npmplus"
            or binding.tls.owner != "provider"
            or binding.ingress.termination_kind != "edge"
        ):
            raise ValueError("Testing requires existing service-owned public edge authority.")
        endpoint = self.store.read_edge_endpoint_record(binding.ingress.endpoint_key)
        if endpoint.status != "active" or endpoint.provider != "dokploy":
            raise ValueError("Testing edge is not active.")
        # A fresh target on another server must never inherit the old edge's destination.
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        actual = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=generation.target_id
        )
        server_id = actual.get("serverId")
        if actual.get("composeId") != generation.target_id or not isinstance(server_id, str):
            raise ValueError("Testing placement is unavailable.")
        server = api.as_json_object(
            api.dokploy_request(
                host=host, token=token, path="/api/server.one", query={"serverId": server_id}
            )
        )
        if server is None or (
            server.get("serverId") != server_id
            or server.get("name") != endpoint.server_name
            or server.get("ipAddress") != endpoint.upstream_host
        ):
            raise ValueError("The recorded public edge does not reach this testing server.")
        material: dict[str, object] = {
            "product": self.product,
            "context": self.request.context,
            "generation": generation_binding(generation),
            "profile": profile.model_dump(mode="json", exclude_none=True),
            "target": target.model_dump(mode="json", exclude_none=True),
            "target_id": target_id.model_dump(mode="json", exclude_none=True),
            "provider": provider.model_dump(mode="json", exclude_none=True),
            "binding": binding.model_dump(mode="json", exclude_none=True),
            "endpoint": endpoint.model_dump(mode="json", exclude_none=True),
            "server_id": server_id,
        }
        bundle = ProductAuthorityBundle(
            dokploy_targets=(target,),
            expected_product_profiles=(profile,),
            required_context_owner=(self.product, self.request.context),
        )
        return material, bundle

    def _custody_binding(self, material: dict[str, object]) -> str:
        """Keep route/generation identity stable across harmless source metadata refreshes."""
        return canonical_json_sha256(
            {
                "generation": material["generation"],
                "binding": route_binding_authority_projection(
                    EnvironmentRouteBindingRecord.model_validate(material["binding"])
                ),
                "provider": provider_target_projection(
                    ProviderTargetRecord.model_validate(material["provider"])
                ),
                "endpoint": EdgeEndpointRecord.model_validate(material["endpoint"]).model_dump(
                    mode="json", exclude={"updated_at"}
                ),
                "server_id": material["server_id"],
                "desired": material["desired"],
            }
        )

    def _clean_template(
        self, template: NpmplusProxyHost, binding: EnvironmentRouteBindingRecord
    ) -> bool:
        if not template.advanced_config:
            return True
        record_id = binding.ingress.provider_evidence.get("audit_record", "")
        if not isinstance(record_id, str) or not record_id.startswith(_AUDIT_PREFIX):
            return False
        digest = record_id.removeprefix(_AUDIT_PREFIX)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return False
        if template.advanced_config != _marker(digest):
            return False
        audit = self.store.read_ingress_route_audit_record(record_id)
        return (
            audit.product == self.product
            and audit.context == self.request.context
            and audit.provider == "npmplus"
            and audit.mode == "apply"
            and audit.status == "applied"
            and audit.requested_domains == template.domain_names
            and audit.edge_endpoint_key == binding.ingress.endpoint_key
            and bool(audit.testing_generation_sha256)
            and bool(audit.testing_authority_sha256)
            and audit.provider_host_id == template.id
            and audit.provider_route_sha256 == canonical_json_sha256(_host_payload(template))
        )

    def _plan(
        self, custody: IngressRouteAuditRecord | None = None
    ) -> tuple[dict[str, object], NpmplusProxyHostPayload, ProductAuthorityBundle]:
        material, bundle = self._authority()
        generation = self.store.read_fresh_odoo_testing_generation(
            self.product, self.request.context
        )
        assert generation is not None
        binding = self.store.read_route_binding_record(
            product=self.product, context_name=self.request.context, instance_name="testing"
        )
        endpoint = self.store.read_edge_endpoint_record(binding.ingress.endpoint_key)
        hosts = self.client.list_proxy_hosts()
        old_domains = {entry.domain_name for entry in binding.domains}
        templates = [h for h in hosts if old_domains.issubset(set(h.domain_names))]
        if len(templates) != 1:
            raise ValueError("The existing edge route is unavailable or ambiguous.")
        template = self.client.get_proxy_host(templates[0].id, public_only=True)
        if (
            not old_domains.issubset(set(template.domain_names))
            or not template.enabled
            or type(template.certificate_id) is not int
            or template.certificate_id < 1
            or not template.ssl_forced
            or (template.forward_host, template.forward_port, template.forward_scheme)
            != (endpoint.upstream_host, endpoint.upstream_port, endpoint.upstream_scheme)
            or not self._clean_template(template, binding)
            or template.locations
            or template.access_list_id
            or template.npmplus_auth_request != "none"
        ):
            raise ValueError("The existing edge route cannot safely template public testing.")
        certificate = self.client.get_certificate(template.certificate_id)
        if (
            certificate.id != template.certificate_id
            or not _certificate_current(certificate)
            or not any(_covers(name, generation.domain_host) for name in certificate.domain_names)
        ):
            raise ValueError("The existing edge certificate does not cover this testing hostname.")
        desired = NpmplusProxyHostPayload.model_validate(
            {
                **_host_payload(template),
                "domain_names": (generation.domain_host,),
                "meta": {},
                "advanced_config": "",
            }
        )
        material.update(
            template=template.model_dump(mode="json"),
            certificate=certificate.model_dump(mode="json"),
            desired=desired.model_dump(mode="json"),
        )
        plan_sha = canonical_json_sha256(material)
        desired.advanced_config = _marker(
            self.request.reviewed_plan_sha256 if custody else plan_sha
        )
        # Validate current provider wire compatibility before crossing the effect checkpoint.
        desired.to_api_payload()
        if custody is not None and (
            custody.record_id != "testing-public-ingress-" + self.request.reviewed_plan_sha256
            or custody.product != self.product
            or custody.context != self.request.context
            or custody.mode != "apply"
            or custody.status not in {"pending", "applied"}
            or custody.requested_domains != desired.domain_names
            or custody.edge_endpoint_key != endpoint.endpoint_key
            or custody.testing_generation_sha256 != generation_binding(generation)
            or custody.testing_authority_sha256 != self._custody_binding(material)
            or custody.provider_route_sha256 != canonical_json_sha256(_host_payload(desired))
        ):
            raise ValueError("The pending edge operation no longer owns this route.")
        self._candidate(
            self._audit(None, "preflight", desired=desired).model_copy(update={"status": "applied"})
        )
        for h in hosts:
            for name in h.domain_names:
                # Nginx wildcard server names cover subtrees, unlike certificate SANs.
                matches = name == generation.domain_host or (
                    name.startswith("*.") and generation.domain_host.endswith(name[1:])
                )
                if name.startswith("~") or matches:
                    if _host_payload(h) != desired.model_dump(mode="json", exclude={"meta"}):
                        raise ValueError("The testing hostname already has foreign edge authority.")
        for name in self.client.list_other_host_domains():
            if (
                name == generation.domain_host
                or name.startswith("~")
                or (name.startswith("*.") and generation.domain_host.endswith(name[1:]))
            ):
                raise ValueError("Another provider host type owns the testing hostname.")
        return (
            {
                "plan_sha256": plan_sha,
                "material": material,
                "custody_sha256": self._custody_binding(material),
            },
            desired,
            bundle,
        )

    def plan(self) -> dict[str, object]:
        snapshot, _, _ = self._plan()
        return self._result(str(snapshot["plan_sha256"]), applied=False)

    def _result(self, digest: str, *, applied: bool) -> dict[str, object]:
        return {
            "product": self.product,
            "context": self.request.context,
            "instance": "testing",
            "operation_id": self.request.operation_id,
            "plan_sha256": digest,
            "applied": applied,
            "generation_preserved": True,
            "copy_preserved": True,
            "public_health_verified": False,
        }

    def _audit(
        self,
        host: NpmplusProxyHost | None,
        provider_key: str,
        *,
        desired: NpmplusProxyHostPayload | None = None,
        custody_sha256: str = "",
    ) -> IngressRouteAuditRecord:
        generation = self.store.read_fresh_odoo_testing_generation(
            self.product, self.request.context
        )
        assert generation is not None
        binding = self.store.read_route_binding_record(
            product=self.product, context_name=self.request.context, instance_name="testing"
        )
        payload = host or desired
        assert payload is not None
        return IngressRouteAuditRecord(
            record_id="testing-public-ingress-" + self.request.reviewed_plan_sha256,
            product=self.product,
            context=self.request.context,
            mode="apply",
            dry_run=False,
            status="applied" if host else "pending",
            requested_domains=(generation.domain_host,),
            edge_endpoint_key=binding.ingress.endpoint_key,
            tls_owner="provider",
            provider_certificate_ref=str(payload.certificate_id),
            provider_route_sha256=canonical_json_sha256(_host_payload(payload)),
            testing_generation_sha256=generation_binding(generation),
            testing_authority_sha256=custody_sha256,
            provider_host_id=host.id if host else None,
            operations=(
                IngressRouteAuditOperation(
                    action="create",
                    host_id=host.id if host else None,
                    domain_names=(generation.domain_host,),
                    requires_apply=host is None,
                ),
            ),
            trace_id=self.trace_id,
            idempotency_key=provider_key,
            reason=self.request.reason,
            recorded_at=utc_now_timestamp(),
        )

    def _candidate(self, audit: IngressRouteAuditRecord) -> EnvironmentRouteBindingRecord:
        candidate = build_route_binding_candidate(
            record_store=self.store,
            audit_record=audit,
            request=RouteBindingReconcileRequest(
                product=self.product,
                context=self.request.context,
                instance="testing",
                source_label="testing-public-ingress:" + self.request.reviewed_plan_sha256,
                evaluated_at=utc_now_timestamp(),
            ),
        ).record
        if candidate is None:
            raise ValueError("The new edge route has no complete source authority.")
        return candidate

    def _publish(
        self,
        host: NpmplusProxyHost,
        provider_key: str,
        expected: dict[str, object],
        custody_sha256: str,
    ) -> None:
        audit = self._audit(host, provider_key, custody_sha256=custody_sha256)
        self.store.write_ingress_route_audit_record(audit)
        candidate = self._candidate(audit)
        self.store.publish_testing_public_ingress_binding(
            expected=expected,
            replacement=candidate,
            audit=audit,
        )

    def readback(self) -> dict[str, object]:
        self._authority()
        generation = self.store.read_fresh_odoo_testing_generation(
            self.product, self.request.context
        )
        assert generation is not None
        binding = self.store.read_route_binding_record(
            product=self.product, context_name=self.request.context, instance_name="testing"
        )
        matches = [
            h for h in self.client.list_proxy_hosts() if generation.domain_host in h.domain_names
        ]
        if len(matches) != 1 or (
            matches[0].domain_names != (generation.domain_host,)
            or matches[0].advanced_config != _marker(self.request.reviewed_plan_sha256)
            or not matches[0].enabled
            or not matches[0].ssl_forced
            or tuple(d.domain_name for d in binding.domains) != (generation.domain_host,)
        ):
            raise ValueError("The testing edge route and binding are not verified.")
        endpoint = self.store.read_edge_endpoint_record(binding.ingress.endpoint_key)
        current = self.client.get_proxy_host(matches[0].id, public_only=True)
        audit = self.store.read_ingress_route_audit_record(
            "testing-public-ingress-" + self.request.reviewed_plan_sha256
        )
        if (
            audit.product != self.product
            or audit.context != self.request.context
            or audit.mode != "apply"
            or audit.status != "applied"
            or audit.provider_host_id != current.id
            or audit.requested_domains != (generation.domain_host,)
            or audit.edge_endpoint_key != endpoint.endpoint_key
            or audit.provider_certificate_ref != str(current.certificate_id)
            or audit.provider_route_sha256 != canonical_json_sha256(_host_payload(current))
            or audit.testing_generation_sha256 != generation_binding(generation)
        ):
            raise ValueError("The provider route differs from its original publication receipt.")
        if binding.model_dump(exclude={"source", "updated_at"}) != self._candidate(
            audit
        ).model_dump(exclude={"source", "updated_at"}):
            raise ValueError("The testing binding differs from its published route authority.")
        if (current.forward_host, current.forward_port, current.forward_scheme) != (
            endpoint.upstream_host,
            endpoint.upstream_port,
            endpoint.upstream_scheme,
        ):
            raise ValueError("The testing edge destination changed.")
        cert = self.client.get_certificate(int(current.certificate_id))
        if not _certificate_current(cert) or not any(
            _covers(name, generation.domain_host) for name in cert.domain_names
        ):
            raise ValueError("The testing edge certificate changed.")
        return self._result(self.request.reviewed_plan_sha256, applied=True)

    def _run(self, provider_key: str, checkpoint: Callable[[], None], *, create: bool) -> None:
        custody = None
        if not create:
            custody = self.store.read_ingress_route_audit_record(
                "testing-public-ingress-" + self.request.reviewed_plan_sha256
            )
            if custody.idempotency_key != provider_key:
                raise ValueError("The edge receipt belongs to another provider operation.")
        snapshot, desired, bundle = self._plan(custody)
        if create and snapshot["plan_sha256"] != self.request.reviewed_plan_sha256:
            raise ValueError("The reviewed testing edge plan changed.")
        target = bundle.dokploy_targets[0]
        target_id = self.store.read_dokploy_target_id_record(
            context_name=target.context, instance_name="testing"
        )
        provider = self.store.read_provider_target_record(
            context_name=target.context, instance_name="testing"
        )
        expected = self._authority()[0]
        result_host: list[NpmplusProxyHost] = []
        if create:
            self.store.write_ingress_route_audit_record(
                self._audit(
                    None,
                    provider_key,
                    desired=desired,
                    custody_sha256=str(snapshot["custody_sha256"]),
                )
            )

        def effect(_receipt: Callable[[str, str, int], None]) -> None:
            fresh, _, _ = self._plan(custody)
            if fresh != snapshot:
                raise ValueError("Testing edge authority changed before the effect.")
            matches = [
                h for h in self.client.list_proxy_hosts() if h.domain_names == desired.domain_names
            ]
            if not matches:
                if not create:
                    raise ValueError("An unknown create may not be repeated.")
                checkpoint()
                actual = self.client.create_proxy_host(desired)
            elif len(matches) == 1:
                actual = matches[0]
            else:
                raise ValueError("Testing edge authority is ambiguous.")
            actual = self.client.get_proxy_host(actual.id, public_only=True)
            if _host_payload(actual) != desired.model_dump(mode="json", exclude={"meta"}):
                raise ValueError("The provider did not preserve the exact testing route.")
            result_host.append(actual)

        self.store.write_product_public_hosts_bundle(
            bundle,
            expected_target=target,
            expected_target_id=target_id,
            expected_provider_target=provider,
            apply_provider=effect,
        )
        checkpoint()
        self._publish(result_host[0], provider_key, expected, str(snapshot["custody_sha256"]))

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        effect_started = False

        def checkpoint() -> None:
            nonlocal effect_started
            lease.checkpoint_effect("create_testing_public_route")
            effect_started = True

        try:
            with self.store.testing_replacement_execution_lock(self.request.operation_id) as check:
                if check is None:
                    raise ValueError("The testing executor is busy.")
                snapshot, _, _ = self._plan()
                if snapshot["plan_sha256"] != self.request.reviewed_plan_sha256:
                    raise ValueError("The reviewed testing edge plan changed.")
                check()
                lease.assert_current()
                self._run(
                    provider_operation_key,
                    checkpoint,
                    create=True,
                )
        except (
            ValueError,
            FileNotFoundError,
            RuntimeError,
            OSError,
            click.ClickException,
        ) as error:
            if effect_started:
                raise ProviderMutationUnknownError(
                    "Testing edge outcome requires original-key observation."
                ) from None
            raise ProviderMutationRejectedError(
                ValueError("Testing public ingress is unverified.")
            ) from error
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=provider_operation_response_payload(
                trace_id=self.trace_id,
                records={},
                result=self.readback(),
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
                with self.store.testing_replacement_execution_lock(
                    self.request.operation_id
                ) as check:
                    if check is None:
                        return ProviderObservation("unknown")
                    check()
                    self._run(provider_operation_key, check, create=False)
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
            return ProviderObservation("unknown")
