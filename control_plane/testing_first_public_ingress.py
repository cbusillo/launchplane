from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.dokploy import api, compose, source
from control_plane.dokploy_target_setup_http import (
    CREATE_COMPOSE_SOURCE_LABEL,
    DokployTargetSetupEnvelope,
)
from control_plane.generic_web_testing_exclusion import require_quiescent_testing_provider_work
from control_plane.dokploy_testing_domain import (
    require_first_testing_target_unprotected,
    require_first_testing_hostname_available,
    require_first_testing_idle,
    match_first_testing_origin,
)
from control_plane.provider_operations import (
    ProviderOperationLease,
    ProviderMutationOutcome,
    ProviderObservation,
)
from control_plane.npmplus import NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.route_binding_reconcile import provider_target_projection, _EVIDENCE_SCAN_LIMIT
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.testing_public_ingress import (
    TestingPublicIngressAdapter,
    _certificate_current,
    _covers,
    _host_payload,
    _marker,
)


class FirstTestingPublicIngressAdapter(TestingPublicIngressAdapter):
    current_provider_key: str = ""

    def provider_target_origin(self) -> ProviderTargetOrigin:
        provider = self.store.read_provider_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        return ProviderTargetOrigin(
            product=self.product,
            context=self.request.context,
            instance="testing",
            provider_id=provider.provider_id,
            target_type=provider.provider_target_type,
            target_id=provider.target_id,
        )

    def target_key(self) -> str:
        return self.provider_target_origin().target_key()

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        self.current_provider_key = provider_operation_key
        return super().apply(provider_operation_key, lease)

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        self.current_provider_key = provider_operation_key
        return super().observe(provider_operation_key, provider_effect_phase, reconciliation_key)

    def _execution_lock_name(self) -> str:
        return testing_event_execution_lock_name(self.target_key())

    def _authority(self) -> tuple[dict[str, object], ProductAuthorityBundle]:
        profile = self.store.read_product_profile_record(self.product)
        lane = next(
            (
                lane
                for lane in profile.lanes
                if (lane.context, lane.instance) == (self.request.context, "testing")
            ),
            None,
        )
        if (
            not profile.is_active
            or profile.driver_id != "odoo"
            or lane is None
            or not is_exclusive_product_context(
                context=self.request.context,
                product=self.product,
                owners=product_context_owner_map(self.store.list_product_profile_records()),
            )
        ):
            raise ValueError(
                "First testing ingress requires an active exclusively owned Odoo testing lane."
            )
        if (
            self.store.read_fresh_odoo_testing_generation(self.product, self.request.context)
            is not None
        ):
            raise ValueError("Use the original generation continuation for restored testing.")
        target = self.store.read_dokploy_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        identity = self.store.read_dokploy_target_id_record(
            context_name=self.request.context, instance_name="testing"
        )
        provider = self.store.read_provider_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        if (
            target.target_type != "compose"
            or identity.source_label != CREATE_COMPOSE_SOURCE_LABEL
            or len(target.domains) != 1
            or target.pending_public_host_effects
            or target.policies.staff_testing_hold is not None
            or provider_target_projection(provider)
            != provider_target_projection(
                ProviderTargetRecord.from_dokploy_records(
                    target_record=target, target_id_record=identity
                )
            )
        ):
            raise ValueError("The initial testing target identity or hostname is unqualified.")
        domain = target.domains[0]
        safety = require_first_testing_target_unprotected(
            self.store, product=self.product, context=self.request.context, provider=provider
        )
        try:
            binding = self.store.read_route_binding_record(
                product=self.product, context_name=self.request.context, instance_name="testing"
            )
        except FileNotFoundError:
            binding = None
        if binding is not None and (
            not self.request.reviewed_plan_sha256
            or binding.ingress.provider_evidence.get("audit_record")
            != "testing-public-ingress-" + self.request.reviewed_plan_sha256
            or binding.status != "active"
            or binding.source.source_kind != "service"
            or tuple(d.domain_name for d in binding.domains) != (domain,)
        ):
            raise ValueError("An existing binding already owns this testing lane.")
        if binding is not None:
            audit = self.store.read_ingress_route_audit_record(
                str(binding.ingress.provider_evidence["audit_record"])
            )
            if binding.model_dump(exclude={"source", "updated_at"}) != self._candidate(
                audit
            ).model_dump(exclude={"source", "updated_at"}):
                raise ValueError("The published testing route authority has changed.")
        for value in (lane.base_url, lane.health_url):
            if value and (urlsplit(value).scheme != "https" or urlsplit(value).hostname != domain):
                raise ValueError("First testing ingress cannot replace an existing public URL.")
        if binding is None:
            if (
                lane.odoo_data_policy.data_authority != "resettable"
                or "empty" not in lane.odoo_data_policy.allowed_rebuild_sources
            ):
                raise ValueError("First testing ingress requires a never-used empty-data lane.")
            scope = DokployTargetSetupEnvelope(
                operation="reconcile-compose-domain",
                context=self.request.context,
                instance="testing",
                target_id=identity.target_id,
                domains=target.domains,
                runtime_port=profile.runtime_port,
                reason=self.request.reason,
            )
            require_first_testing_idle(
                self.store,
                product=self.product,
                request=scope,
                provider=provider,
                current_provider_key=self.current_provider_key,
            )
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        live, server = self._placement(host=host, token=token, target_id=identity.target_id)
        routes = api.dokploy_request(
            host=host,
            token=token,
            path="/api/domain.byComposeId",
            query={"composeId": identity.target_id},
        )
        expected = compose.compose_web_domain_route_payload(
            compose_id=identity.target_id, domain_host=domain, runtime_port=profile.runtime_port
        )
        origin_id = match_first_testing_origin(
            routes, target_id=identity.target_id, domain=domain, runtime_port=profile.runtime_port
        )
        if not origin_id:
            raise ValueError("The approved origin hostname must exist before edge publication.")
        endpoints = self.store.list_edge_endpoint_records(
            provider="dokploy", status="active", limit=_EVIDENCE_SCAN_LIMIT + 1
        )
        if len(endpoints) > _EVIDENCE_SCAN_LIMIT:
            raise ValueError("The active edge inventory is incomplete.")
        matches = [
            endpoint
            for endpoint in endpoints
            if endpoint.provider == "dokploy"
            and endpoint.server_name == server.get("name")
            and endpoint.upstream_host == server.get("ipAddress")
            and endpoint.upstream_scheme == ("https" if expected["https"] else "http")
            and endpoint.upstream_port == (443 if expected["https"] else 80)
        ]
        if len(matches) != 1:
            raise ValueError("The origin must match one active recorded edge endpoint.")
        endpoint = matches[0]
        if binding is not None and binding.ingress.endpoint_key != endpoint.endpoint_key:
            raise ValueError("The published edge endpoint changed.")
        if binding is None:
            require_quiescent_testing_provider_work(
                host=host, token=token, target_id=identity.target_id, never_deployed=True
            )
        require_first_testing_hostname_available(
            product=self.product,
            target=target,
            domain=domain,
            targets=self.store.list_dokploy_target_records(),
            profiles=self.store.list_product_profile_records(),
            bindings=self.store.list_route_binding_records(),
            allow_current_binding=binding is not None,
        )
        material: dict[str, object] = {
            "product": self.product,
            "context": self.request.context,
            "domain_host": domain,
            "first_use": canonical_json_sha256(
                {
                    "product": self.product,
                    "context": self.request.context,
                    "target": provider_target_projection(provider),
                    "domain": domain,
                    "origin_domain_id": origin_id,
                }
            ),
            "profile": profile.model_dump(mode="json", exclude_none=True),
            "target": target.model_dump(mode="json", exclude_none=True),
            "target_id": identity.model_dump(mode="json", exclude_none=True),
            "provider": provider.model_dump(mode="json", exclude_none=True),
            "binding": binding.model_dump(mode="json", exclude_none=True) if binding else None,
            "endpoint": endpoint.model_dump(mode="json", exclude_none=True),
            "server_id": live["serverId"],
            "safety": safety.model_dump(mode="json", exclude_none=True) if safety else None,
        }
        return material, ProductAuthorityBundle(
            dokploy_targets=(target,),
            expected_product_profiles=(profile,),
            required_context_owner=(self.product, self.request.context),
        )

    def _authority_witness(self, material: dict[str, object]) -> str:
        return ""

    def _verify_readback_authority(
        self, material: dict[str, object], audit: IngressRouteAuditRecord, current: NpmplusProxyHost
    ) -> None:
        desired = _host_payload(current)
        marker = _marker(self.request.reviewed_plan_sha256)
        if current.advanced_config != marker:
            raise ValueError("First testing ingress cannot adopt changed route configuration.")
        desired["advanced_config"] = ""
        desired["meta"] = {}
        if (
            self._custody_binding({**material, "desired": desired})
            != audit.testing_authority_sha256
        ):
            raise ValueError("The first-use target differs from its published authority witness.")

    def _custody_binding(self, material: dict[str, object]) -> str:
        profile = LaunchplaneProductProfileRecord.model_validate(material["profile"])
        lane = next(
            lane
            for lane in profile.lanes
            if (lane.context, lane.instance) == (self.request.context, "testing")
        )
        return canonical_json_sha256(
            {
                "first_use": material["first_use"],
                "product": profile.product,
                "driver_id": profile.driver_id,
                "is_active": profile.is_active,
                "runtime_port": profile.runtime_port,
                "health_path": profile.health_path,
                "lane": lane.model_dump(mode="json", exclude={"base_url", "health_url"}),
                "provider": provider_target_projection(
                    ProviderTargetRecord.model_validate(material["provider"])
                ),
                "endpoint": EdgeEndpointRecord.model_validate(material["endpoint"]).model_dump(
                    mode="json", exclude={"updated_at"}
                ),
                "desired": material["desired"],
            }
        )

    def _plan(
        self, custody: IngressRouteAuditRecord | None = None
    ) -> tuple[dict[str, object], NpmplusProxyHostPayload, ProductAuthorityBundle]:
        material, bundle = self._authority()
        endpoint = EdgeEndpointRecord.model_validate(material["endpoint"])
        domain = str(material["domain_host"])
        certificates = [
            certificate
            for certificate in self.client.list_certificates()
            if _certificate_current(certificate)
            and any(_covers(name, domain) for name in certificate.domain_names)
        ]
        if len(certificates) != 1:
            raise ValueError("An unambiguous current covering certificate must already exist.")
        certificate = certificates[0]
        desired = NpmplusProxyHostPayload(
            domain_names=(domain,),
            forward_host=endpoint.upstream_host,
            forward_scheme=endpoint.upstream_scheme,
            forward_port=endpoint.upstream_port,
            certificate_id=certificate.id,
        )
        material.update(
            certificate=certificate.model_dump(mode="json"), desired=desired.model_dump(mode="json")
        )
        return self._finish_plan(
            material,
            bundle,
            desired,
            endpoint,
            domain,
            "",
            self.client.list_proxy_hosts(),
            custody,
        )

    def _publish(
        self,
        host: NpmplusProxyHost,
        provider_key: str,
        expected: dict[str, object],
        custody_sha256: str,
    ) -> None:
        audit = self._audit(host, provider_key, custody_sha256=custody_sha256, authority=expected)
        self.store.write_ingress_route_audit_record(audit)
        self.store.publish_first_testing_public_ingress_binding(
            expected=expected, replacement=self._candidate(audit), audit=audit
        )
