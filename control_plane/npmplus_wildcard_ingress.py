from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import click

from control_plane import secrets
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.public_hosts import normalize_public_hosts
from control_plane.contracts.product_profile_record import (
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.contracts.secret_record import SecretRecord
from control_plane.npmplus import (
    certificate_covers_hostname as _covers,
    certificate_is_current as _certificate_current,
    PREVIEW_DNS_BINDING,
    PREVIEW_DNS_INTEGRATION,
    NpmplusClient,
    NpmplusProxyHost,
    NpmplusProxyHostPayload,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressOperation,
    apply_npmplus_ingress_route,
)


def preview_dns_secret_record(store: PostgresRecordStore) -> SecretRecord | None:
    record = secrets.resolve_context_secret_record_from_store(
        record_store=store,
        integration=PREVIEW_DNS_INTEGRATION,
        context_name="launchplane",
        binding_key=PREVIEW_DNS_BINDING,
    )
    return record if record is not None and record.name == PREVIEW_DNS_BINDING else None


@dataclass
class NpmplusWildcardIngressOperation:
    store: PostgresRecordStore
    client: NpmplusClient
    product: str
    context: str
    request: NpmplusIngressApplyRequest
    finish: Callable[[NpmplusIngressApplyResult, str], dict[str, object]]
    pending: Callable[[], None]

    def target_key(self) -> str:
        return "npmplus-wildcard:" + self.request.route.domain_names[0].lower()

    def reconciliation_key(self) -> str:
        return canonical_json_sha256(
            {
                "product": self.product,
                "context": self.context,
                "request": self.request.model_dump(mode="json"),
            }
        )

    def plan(self) -> NpmplusIngressApplyResult:
        profile = self.store.read_product_profile_record(self.product)
        if (
            not profile.is_active
            or not profile.preview.enabled
            or profile.preview.context != self.context
            or profile.preview.domain_certificate_type != "none"
            or not is_exclusive_product_context(
                product=self.product,
                context=self.context,
                owners=product_context_owner_map(
                    self.store.list_product_profile_records(), config_scopes=True
                ),
            )
        ):
            raise ValueError("Wildcard issuance requires the product's exclusive preview context.")
        runtime = self.store.list_runtime_environment_records(
            scope="context", context_name=self.context
        )
        bases = tuple(
            record.env["LAUNCHPLANE_PREVIEW_BASE_URL"]
            for record in runtime
            if "LAUNCHPLANE_PREVIEW_BASE_URL" in record.env
        )
        if len(bases) != 1 or not isinstance(bases[0], str):
            raise ValueError("The product preview namespace is unavailable.")
        base = urlsplit(bases[0])
        namespace = normalize_public_hosts((base.hostname or "",))[0]
        if base.scheme != "https" or self.request.route.domain_names != ("*." + namespace,):
            raise ValueError("Wildcard issuance must use the recorded product preview namespace.")
        for other in self.store.list_product_profile_records():
            if other.product == self.product or not other.preview.enabled:
                continue
            for entry in self.store.list_runtime_environment_records(
                scope="context", context_name=other.preview.context
            ):
                other_base = entry.env.get("LAUNCHPLANE_PREVIEW_BASE_URL")
                if isinstance(other_base, str) and urlsplit(other_base).hostname == namespace:
                    raise ValueError("Another product owns this preview namespace.")
        edge = self.store.read_edge_endpoint_record(self.request.route.edge_endpoint_key)
        if edge.status != "active" or (
            self.request.route.forward_host,
            self.request.route.forward_port,
            self.request.route.forward_scheme,
        ) != (edge.upstream_host, edge.upstream_port, edge.upstream_scheme):
            raise ValueError("The reviewed edge endpoint changed.")
        result = apply_npmplus_ingress_route(
            client=self.client, request=self.request.model_copy(update={"mode": "dry-run"})
        )
        for name in (
            *(name for host in self.client.list_proxy_hosts() for name in host.domain_names),
            *self.client.list_other_host_domains(),
        ):
            if name.startswith("~") or any(
                _covers(name, domain) or _covers(domain, name)
                for domain in self.request.route.domain_names
            ):
                raise ValueError("An existing provider hostname overlaps this wildcard.")
        return result

    def _marked_request(self, provider_key: str) -> NpmplusIngressApplyRequest:
        return self.request.model_copy(
            update={
                "route": self.request.route.model_copy(
                    update={
                        "advanced_config": "# launchplane wildcard "
                        + provider_key
                        + "\n"
                        + self.request.route.advanced_config,
                    }
                )
            }
        )

    def _readback(self, provider_key: str) -> NpmplusIngressApplyResult | None:
        desired = self._marked_request(provider_key).route.to_proxy_host_payload()
        claims = [
            host
            for host in self.client.list_proxy_hosts()
            if set(host.domain_names) == set(desired.domain_names)
        ]
        if len(claims) != 1:
            return None
        host = self.client.get_proxy_host(claims[0].id, public_only=True)
        if type(host.certificate_id) is not int or host.certificate_id < 1:
            return None
        if host.model_dump(exclude={"id", "meta", "certificate_id"}) != desired.model_dump(
            exclude={"meta", "certificate_id"}
        ):
            return None
        certificate = self.client.get_certificate(host.certificate_id)
        if set(certificate.domain_names) != set(desired.domain_names) or not _certificate_current(
            certificate
        ):
            return None
        return NpmplusIngressApplyResult(
            status="applied",
            dry_run=False,
            proxy_host=host,
            operations=(
                NpmplusIngressOperation(
                    action="create",
                    host_id=host.id,
                    domain_names=host.domain_names,
                    requires_apply=True,
                ),
            ),
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        dispatched = False
        try:
            self.plan()
            record = preview_dns_secret_record(self.store)
            if record is None or record.current_version_id != self.request.dns_secret_version_id:
                raise ValueError("The reviewed DNS credential is unavailable or changed.")

            def create_host(payload: NpmplusProxyHostPayload) -> NpmplusProxyHost:
                nonlocal dispatched
                if preview_dns_secret_record(self.store) != record:
                    raise ValueError("The reviewed DNS credential changed.")
                token = secrets.resolve_context_secret_value_from_store(
                    record_store=self.store,
                    integration=PREVIEW_DNS_INTEGRATION,
                    context_name="launchplane",
                    binding_key=PREVIEW_DNS_BINDING,
                    expected_version_id=record.current_version_id,
                )
                if not token:
                    raise ValueError("The DNS credential is unavailable.")

                def before_dispatch() -> None:
                    nonlocal dispatched
                    self.pending()
                    lease.checkpoint_effect("create_wildcard_certificate_and_host")
                    dispatched = True

                return self.client.create_dns_proxy_host(
                    payload, token=token, before_dispatch=before_dispatch
                )

            lease.assert_current()
            apply_npmplus_ingress_route(
                client=self.client,
                request=self._marked_request(provider_operation_key),
                create_host=create_host,
            )
            result = self._readback(provider_operation_key)
            if result is None:
                raise ValueError("The wildcard route readback is unverified.")
            return ProviderMutationOutcome(202, self.finish(result, provider_operation_key))
        except (ValueError, LookupError, OSError, click.ClickException):
            if dispatched:
                raise ProviderMutationUnknownError(
                    "Wildcard provider effect requires reconciliation."
                ) from None
            raise ProviderMutationRejectedError(
                ValueError("Wildcard prerequisites are unverified.")
            ) from None

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        if reconciliation_key != self.reconciliation_key():
            return ProviderObservation("unknown")
        if not provider_effect_phase:
            return ProviderObservation("absent", retry_safe=True)
        try:
            result = self._readback(provider_operation_key)
            if result is not None:
                return ProviderObservation(
                    "present", response_payload=self.finish(result, provider_operation_key)
                )
        except (ValueError, LookupError, OSError, click.ClickException):
            return ProviderObservation("unknown")
        return ProviderObservation("unknown")
