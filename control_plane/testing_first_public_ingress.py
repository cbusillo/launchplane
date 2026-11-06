from urllib.parse import urlsplit

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.disposable_lanes import require_lane_target_ownership
from control_plane.dokploy import api, compose, source
from control_plane.dokploy.application_domains import (
    _domain_value_matches,
    require_exclusive_testing_application_hostname,
)
from control_plane.dokploy_target_setup_http import (
    CREATE_COMPOSE_SOURCE_LABEL,
    DokployTargetSetupEnvelope,
    _lane_has_usage_or_data_commitments,
)
from control_plane.generic_web_testing_exclusion import (
    _target_reservations,
    require_quiescent_testing_provider_work,
)
from control_plane.npmplus import NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.route_binding_reconcile import provider_target_projection
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.testing_public_ingress import (
    TestingPublicIngressAdapter,
    _certificate_current,
    _covers,
)
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)


class FirstTestingPublicIngressAdapter(TestingPublicIngressAdapter):
    def _execution_lock_name(self) -> str:
        provider = self.store.read_provider_target_record(
            context_name=self.request.context, instance_name="testing"
        )
        return testing_event_execution_lock_name(dokploy_generic_web_provider_target_key(provider))

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
        require_exclusive_testing_application_hostname(
            hostname=domain, target=target, targets=self.store.list_dokploy_target_records()
        )
        require_lane_target_ownership(
            self.store.list_dokploy_target_id_records(),
            context=self.request.context,
            instance="testing",
            target_id=identity.target_id,
        )
        safety = self.store.read_disposable_lane_safety(
            self.product, self.request.context, "testing"
        )
        if safety is not None and (
            safety.usage == "in_use"
            or safety.data == "only_copy"
            or safety.production_resources in {"shared", "separation_ready"}
        ):
            raise ValueError(
                "Testing use, data custody or shared production resources protects this lane."
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
        for value in (lane.base_url, lane.health_url):
            if value and (
                binding is None
                or urlsplit(value).scheme != "https"
                or urlsplit(value).hostname != domain
            ):
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
            if _lane_has_usage_or_data_commitments(self.store, scope) or _target_reservations(
                self.store,
                product=self.product,
                context=self.request.context,
                target_key=dokploy_generic_web_provider_target_key(provider),
                target_id=identity.target_id,
                require_complete=True,
            ):
                raise ValueError("Testing work or data commitments protect this target.")
        host, token = source.read_dokploy_config(control_plane_root=self.root)
        live = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=identity.target_id
        )
        if live.get("composeId") != identity.target_id or not isinstance(live.get("serverId"), str):
            raise ValueError("Testing placement is unavailable.")
        routes = api.dokploy_request(
            host=host,
            token=token,
            path="/api/domain.byComposeId",
            query={"composeId": identity.target_id},
        )
        expected = compose.compose_web_domain_route_payload(
            compose_id=identity.target_id, domain_host=domain, runtime_port=profile.runtime_port
        )
        if (
            not isinstance(routes, list)
            or len(routes) != 1
            or not isinstance(routes[0], dict)
            or not isinstance(routes[0].get("domainId"), str)
            or not routes[0]["domainId"]
            or any(
                not _domain_value_matches(routes[0].get(key), value)
                for key, value in expected.items()
            )
            or any(
                routes[0].get(key, default) != default
                for key, default in {
                    "enabled": True,
                    "middlewares": [],
                    "forwardAuthEnabled": False,
                    "customEntrypoint": None,
                }.items()
            )
        ):
            raise ValueError(
                "The approved origin hostname must exist unchanged before edge publication."
            )
        server = api.as_json_object(
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/server.one",
                query={"serverId": str(live["serverId"])},
            )
        )
        if server is None or server.get("serverId") != live["serverId"]:
            raise ValueError("Testing server identity is unavailable.")
        endpoints = self.store.list_edge_endpoint_records(status="active", limit=101)
        if len(endpoints) > 100:
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
        claims = {
            urlsplit(value).hostname or ""
            for other in self.store.list_product_profile_records()
            for other_lane in other.lanes
            if (other.product, other_lane.context, other_lane.instance)
            != (self.product, self.request.context, "testing")
            for value in (other_lane.base_url, other_lane.health_url)
            if value
        } | {
            entry.domain_name
            for other in self.store.list_route_binding_records()
            if (other.product, other.context, other.instance)
            != (self.product, self.request.context, "testing")
            for entry in other.domains
        }
        if any(
            claim == domain or claim.startswith("*.") and domain.endswith(claim[1:])
            for claim in claims
        ):
            raise ValueError("Another lane already claims this public hostname.")
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
                    "origin_domain_id": routes[0]["domainId"],
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

    def _route_identity(self) -> tuple[str, str, EdgeEndpointRecord]:
        material, _ = self._authority()
        return (
            str(material["domain_host"]),
            str(material["first_use"]),
            EdgeEndpointRecord.model_validate(material["endpoint"]),
        )

    def _custody_binding(self, material: dict[str, object]) -> str:
        profile = LaunchplaneProductProfileRecord.model_validate(material["profile"])
        normalized = profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"base_url": "", "health_url": ""})
                    if (lane.context, lane.instance) == (self.request.context, "testing")
                    else lane
                    for lane in profile.lanes
                )
            }
        )
        return canonical_json_sha256(
            {
                "generation": material["first_use"],
                "profile": normalized.model_dump(mode="json", exclude={"updated_at"}),
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
            str(material["first_use"]),
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
        audit = self._audit(host, provider_key, custody_sha256=custody_sha256)
        self.store.write_ingress_route_audit_record(audit)
        self.store.publish_first_testing_public_ingress_binding(
            expected=expected, replacement=self._candidate(audit), audit=audit
        )
