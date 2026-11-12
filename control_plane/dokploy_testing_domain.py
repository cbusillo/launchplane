from collections.abc import Iterable
from contextlib import ExitStack

from control_plane.disposable_lanes import recorded_lane_protection
from control_plane.contracts.public_hosts import normalize_public_hosts, retained_hostname_identity
from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.provider_operations import build_provider_operation_key
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.disposable_lanes import require_lane_target_ownership
from control_plane.dokploy import api, compose
from control_plane.dokploy.application_domains import (
    _domain_value_matches,
    require_exclusive_testing_application_hostname,
)
from control_plane.dokploy.target_source_setup import require_empty_compose_source
from control_plane.dokploy_target_setup_http import (
    CREATE_COMPOSE_SOURCE_LABEL,
    DokployComposeDomainPartialError,
    DokployComposeDomainReconcileResult,
    DokployTargetSetupEnvelope,
    _lane_has_usage_or_data_commitments,
)
from control_plane.generic_web_testing_exclusion import (
    _target_reservations,
    require_quiescent_testing_provider_work,
)
from control_plane.private_testing_activation import _placement
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)
from control_plane.workflows.ship import utc_now_timestamp


def require_first_testing_target_unprotected(
    store: PostgresRecordStore,
    *,
    product: str,
    context: str,
    provider: ProviderTargetRecord,
    instance: str = "testing",
) -> DisposableLaneSafety | None:
    from control_plane.application_state_volume import require_no_held_state_volume

    if instance == "testing":
        require_no_held_state_volume(store, context)
    require_lane_target_ownership(
        store.list_dokploy_target_id_records(),
        context=context,
        instance=instance,
        target_id=provider.target_id,
    )
    if any(
        other.provider_id == provider.provider_id
        and other.target_id == provider.target_id
        and (other.context, other.instance) != (context, instance)
        for other in store.list_physical_provider_target_records()
    ):
        raise ValueError("The compose is shared with another lane.")
    evidence = store.read_disposable_lane_safety(product, context, instance)
    try:
        target = store.read_dokploy_target_record(context_name=context, instance_name=instance)
    except FileNotFoundError:
        target = None
    protection = recorded_lane_protection(target, evidence)
    if (
        evidence is not None
        and evidence.current_usage() == "in_use"
        or protection.only_copy
        or protection.resources_protected
    ):
        raise ValueError("Recorded use, data custody or resource sharing protects this lane.")
    return evidence


def require_first_testing_hostname_available(
    *,
    product: str,
    target: DokployTargetRecord,
    domain: str,
    targets: Iterable[DokployTargetRecord],
    profiles: Iterable[LaunchplaneProductProfileRecord],
    bindings: Iterable[EnvironmentRouteBindingRecord],
    allow_current_binding: bool = False,
) -> None:
    require_exclusive_testing_application_hostname(hostname=domain, target=target, targets=())
    require_testing_hostname_claims_available(
        product=product,
        target=target,
        domain=domain,
        targets=targets,
        profiles=profiles,
        bindings=bindings,
        allow_current_binding=allow_current_binding,
    )


def require_testing_hostname_claims_available(
    *,
    product: str,
    target: DokployTargetRecord,
    domain: str,
    targets: Iterable[DokployTargetRecord],
    profiles: Iterable[LaunchplaneProductProfileRecord],
    bindings: Iterable[EnvironmentRouteBindingRecord],
    allow_current_binding: bool = False,
) -> None:
    claims = {
        retained_hostname_identity(value)
        for other in targets
        if (other.context, other.instance) != (target.context, target.instance)
        for value in other.hostname_claims
    } | {
        entry.domain_name
        for binding in bindings
        if not (
            allow_current_binding
            and (binding.product, binding.context, binding.instance)
            == (product, target.context, target.instance)
        )
        for entry in binding.domains
    }
    profile_claims = {
        retained_hostname_identity(value)
        for other in profiles
        for lane in other.lanes
        if (other.product, lane.context, lane.instance)
        != (product, target.context, target.instance)
        for value in (lane.base_url, lane.health_url)
        if retained_hostname_identity(value)
    }
    if any(not claim or claim.startswith("~") for claim in claims) or any(
        claim == domain or claim.startswith("*.") and domain.endswith(claim[1:])
        for claim in claims | profile_claims
    ):
        raise ValueError("The hostname belongs to existing ingress authority.")


def require_first_testing_idle(
    store: PostgresRecordStore,
    *,
    product: str,
    request: DokployTargetSetupEnvelope,
    provider: ProviderTargetRecord,
    current_provider_key: str = "",
) -> None:
    if _lane_has_usage_or_data_commitments(store, request):
        raise ValueError("First setup cannot change a lane that has deployed or served.")
    reservations = _target_reservations(
        store,
        product=product,
        context=request.context,
        target_key=dokploy_generic_web_provider_target_key(provider),
        target_id=provider.target_id,
        require_complete=True,
    )
    if any(
        build_provider_operation_key(
            scope=row.scope,
            route_path=row.route_path,
            idempotency_key=row.idempotency_key,
            request_fingerprint=row.request_fingerprint,
            reconciliation_key=row.reconciliation_key,
        )
        != current_provider_key
        for row in reservations
    ):
        raise ValueError("A provider operation still owns the target.")


def match_first_testing_origin(
    routes: object, *, target_id: str, domain: str, runtime_port: int
) -> str:
    if not isinstance(routes, list) or len(routes) > 1:
        raise ValueError("First hostname setup requires zero routes or one exact reviewed route.")
    if not routes:
        return ""
    route = routes[0]
    expected = compose.compose_web_domain_route_payload(
        compose_id=target_id, domain_host=domain, runtime_port=runtime_port
    )
    defaults: api.JsonObject = {
        "customEntrypoint": None,
        "middlewares": [],
        "forwardAuthEnabled": False,
        "enabled": True,
    }
    if (
        not isinstance(route, dict)
        or not isinstance(route.get("domainId"), str)
        or not route["domainId"].strip()
        or any(not _domain_value_matches(route.get(key), value) for key, value in expected.items())
        or any(
            not _domain_value_matches(route.get(key, value), value)
            for key, value in defaults.items()
        )
    ):
        raise ValueError(
            "An existing provider route differs from the exact reviewed first hostname."
        )
    return str(route["domainId"])


EMPTY_DOMAIN_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "operation",
        "product",
        "context",
        "instance",
        "target_id",
        "domains",
        "runtime_port",
        "confirmation",
        "reason",
    }
)


def _requests_reviewed_hostnames(request: DokployTargetSetupEnvelope) -> bool:
    if request.instance == "testing":
        return len(request.domains) == 1
    return request.instance == "prod" and bool(request.domains)


def _lane_accepts_first_hostnames(
    profile: LaunchplaneProductProfileRecord, request: DokployTargetSetupEnvelope
) -> bool:
    return (
        profile.is_active
        and any(
            (lane.context, lane.instance) == (request.context, request.instance)
            for lane in profile.lanes
        )
        and request.runtime_port == profile.runtime_port
        and not (request.instance == "prod" and profile.production_use == "live")
    )


def match_first_origins(
    routes: object, *, target_id: str, domains: tuple[str, ...], runtime_port: int
) -> dict[str, str]:
    if not isinstance(routes, list):
        raise ValueError("First hostname setup requires a readable provider route list.")
    matched: dict[str, str] = {}
    for route in routes:
        host = route.get("host") if isinstance(route, dict) else None
        if not isinstance(host, str) or host not in domains or host in matched:
            raise ValueError("An existing provider route is outside the exact reviewed hostnames.")
        matched[host] = match_first_testing_origin(
            [route], target_id=target_id, domain=host, runtime_port=runtime_port
        )
    return matched


def _lane_lock_names(
    request: DokployTargetSetupEnvelope, provider: ProviderTargetRecord
) -> list[str]:
    from control_plane.dokploy_target_setup_http import first_production_target_lock_name

    names = [testing_event_execution_lock_name(dokploy_generic_web_provider_target_key(provider))]
    if request.instance == "prod":
        names.insert(0, first_production_target_lock_name(request.context))
    return names


def setup_empty_lane_domain(
    *,
    record_store: PostgresRecordStore,
    request: DokployTargetSetupEnvelope,
    host: str,
    token: str,
) -> DokployComposeDomainReconcileResult:
    from control_plane.dokploy_target_setup_http import (
        fetch_dokploy_target_payload_for_setup,
        require_no_unresolved_production_setup,
    )

    if (
        request.model_dump(exclude_defaults=True).keys() - EMPTY_DOMAIN_FIELDS
        or not _requests_reviewed_hostnames(request)
        or normalize_public_hosts(request.domains) != request.domains
        or request.mode != "apply"
        or not request.target_id
    ):
        raise ValueError(
            "First hostname setup requires one exact owned compose target and its hostnames."
        )
    instance = request.instance
    profiles = record_store.list_product_profile_records()
    owners = product_context_owner_map(profiles)
    products = owners.get(request.context, frozenset())
    if len(products) != 1:
        raise ValueError("First hostname setup requires exclusive product ownership.")
    (product,) = products
    profile = record_store.read_product_profile_record(product)
    if not is_exclusive_product_context(
        context=request.context, product=product, owners=owners
    ) or not _lane_accepts_first_hostnames(profile, request):
        raise ValueError(
            "First hostname setup requires an active owned lane that is not Live, and its runtime port."
        )
    target = record_store.read_dokploy_target_record(
        context_name=request.context, instance_name=instance
    )
    target_id = record_store.read_dokploy_target_id_record(
        context_name=request.context, instance_name=instance
    )
    provider = record_store.read_provider_target_record(
        context_name=request.context, instance_name=instance
    )
    if (
        target.target_type != "compose"
        or target_id.source_label != CREATE_COMPOSE_SOURCE_LABEL
        or request.target_id != target_id.target_id
        or provider.to_deployed_target_reference()
        != ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        ).to_deployed_target_reference()
        or target.env
        or target.domains not in ((), request.domains)
        or target.public_hosts
        or target.pending_public_host_effects
        or target.custom_git_url
        or target.custom_git_branch
        or target.git_branch
        or target.policies.staff_testing_hold is not None
    ):
        raise ValueError(
            "First hostname setup requires an unchanged service-created empty compose."
        )
    replacement = target.model_copy(
        update={
            "domains": request.domains,
            "updated_at": utc_now_timestamp(),
            "source_label": "service:dokploy-targets:setup:reconcile-compose-domain",
        }
    )
    route_ids: list[str] = []
    effect_started = False
    provider_verified = False

    def require_unprotected_binding() -> None:
        require_first_testing_target_unprotected(
            record_store,
            product=product,
            context=request.context,
            provider=provider,
            instance=instance,
        )
        if (
            record_store.read_product_profile_record(product) != profile
            or record_store.read_dokploy_target_record(
                context_name=request.context, instance_name=instance
            )
            != target
            or record_store.read_dokploy_target_id_record(
                context_name=request.context, instance_name=instance
            )
            != target_id
            or record_store.read_provider_target_record(
                context_name=request.context, instance_name=instance
            )
            != provider
        ):
            raise ValueError("Lane bindings changed during hostname qualification.")

    def matching_routes(routes: object) -> dict[str, str]:
        return match_first_origins(
            routes,
            target_id=target_id.target_id,
            domains=request.domains,
            runtime_port=profile.runtime_port,
        )

    def read_routes() -> dict[str, str]:
        return matching_routes(
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/domain.byComposeId",
                query={"composeId": target_id.target_id},
            )
        )

    def checkpoint() -> dict[str, str]:
        require_first_testing_idle(
            record_store, product=product, request=request, provider=provider
        )
        if instance == "prod":
            require_no_unresolved_production_setup(
                record_store, context=request.context, target_id=target_id.target_id
            )
        require_unprotected_binding()
        for domain in request.domains:
            require_testing_hostname_claims_available(
                product=product,
                target=target,
                domain=domain,
                targets=record_store.list_dokploy_target_records(),
                profiles=record_store.list_product_profile_records(),
                bindings=record_store.list_route_binding_records(),
            )
        live = fetch_dokploy_target_payload_for_setup(host, token, "compose", target_id.target_id)
        require_empty_compose_source(live, target_id.target_id)
        matching_routes(live.get("domains"))
        _placement({**live, "autoDeploy": False, "domains": []}, target_id.target_id)
        if live.get("env") not in (None, ""):
            raise ValueError("First hostname setup requires empty provider runtime settings.")
        existing_routes = read_routes()
        require_quiescent_testing_provider_work(
            host=host,
            token=token,
            target_id=target_id.target_id,
            never_deployed=True,
        )
        if (
            fetch_dokploy_target_payload_for_setup(host, token, "compose", target_id.target_id)
            != live
        ):
            raise ValueError("Provider placement changed during first hostname checks.")
        require_unprotected_binding()
        check()
        return existing_routes

    def create_route(domain: str) -> str:
        created = api.as_json_object(
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/domain.create",
                method="POST",
                payload=compose.compose_web_domain_route_payload(
                    compose_id=target_id.target_id,
                    domain_host=domain,
                    runtime_port=profile.runtime_port,
                ),
            )
        )
        domain_id = (created or {}).get("domainId")
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise ValueError("First hostname creation returned no provider route identity.")
        return domain_id

    def apply_provider() -> None:
        nonlocal effect_started, provider_verified
        existing_routes = checkpoint()
        for domain in request.domains:
            if domain in existing_routes:
                route_ids.append(existing_routes[domain])
                continue
            effect_started = True
            route_ids.append(create_route(domain))
        if read_routes() != dict(zip(request.domains, route_ids, strict=True)):
            raise ValueError("First hostname provider readback does not match the exact routes.")
        check()
        provider_verified = True

    try:
        with ExitStack() as locks:
            held_checks = [
                locks.enter_context(record_store.testing_replacement_execution_lock(name))
                for name in _lane_lock_names(request, provider)
            ]
            lane_checks = [held for held in held_checks if held is not None]
            if len(lane_checks) != len(held_checks):
                raise ValueError("Another executor for this target is still active.")

            def check() -> None:
                for lane_check in lane_checks:
                    lane_check()

            record_store.complete_dokploy_compose_setup(
                expected_profile=profile,
                expected_record=target,
                expected_target_id=target_id,
                expected_provider_target=provider,
                replacement_record=replacement,
                apply_provider=apply_provider,
                setup_kind="domain",
            )
    except Exception as error:
        if effect_started or provider_verified:
            raise DokployComposeDomainPartialError(
                request=request,
                compose_id=target_id.target_id,
                completed_domain_ids=route_ids,
                pending_domain=request.domains[len(route_ids)]
                if len(route_ids) < len(request.domains)
                else "",
                stage="first-hostname",
            ) from error
        raise
    return DokployComposeDomainReconcileResult(
        applied=True,
        target_record=replacement,
        target_id_record=target_id,
        domains=request.domains,
        runtime_port=profile.runtime_port,
        route_domain_ids=tuple(route_ids),
    )
