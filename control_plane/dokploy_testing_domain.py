from collections.abc import Callable

from control_plane.contracts.public_hosts import retained_hostname_identity
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.dokploy import api, compose
from control_plane.dokploy.application_domains import require_exclusive_testing_application_hostname
from control_plane.dokploy.target_source_setup import require_empty_compose_source
from control_plane.dokploy_target_setup_http import (
    CREATE_COMPOSE_SOURCE_LABEL,
    DokployComposeDomainPartialError,
    DokployComposeDomainReconcileResult,
    DokployTargetSetupEnvelope,
    _lane_has_usage_or_data_commitments,
)
from control_plane.generic_web_testing_exclusion import _inventory, _target_reservations
from control_plane.private_testing_activation import _placement
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)
from control_plane.workflows.ship import utc_now_timestamp


def setup_empty_testing_domain(
    *,
    record_store: PostgresRecordStore,
    request: DokployTargetSetupEnvelope,
    host: str,
    token: str,
) -> DokployComposeDomainReconcileResult:
    from control_plane.dokploy_target_setup_http import (
        fetch_dokploy_target_payload_for_setup,
        fetch_dokploy_compose_domains_for_target_setup,
    )

    allowed = {
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
    if (
        request.model_dump(exclude_defaults=True).keys() - allowed
        or request.instance != "testing"
        or len(request.domains) != 1
        or request.mode == "apply"
        and not request.target_id
    ):
        raise ValueError(
            "First hostname setup requires one exact testing compose target and hostname."
        )
    profiles = record_store.list_product_profile_records()
    owners = product_context_owner_map(profiles)
    products = owners.get(request.context, frozenset())
    if len(products) != 1:
        raise ValueError("First hostname setup requires exclusive product ownership.")
    (product,) = products
    profile = record_store.read_product_profile_record(product)
    if (
        not profile.is_active
        or not is_exclusive_product_context(context=request.context, product=product, owners=owners)
        or not any(
            (lane.context, lane.instance) == (request.context, "testing") for lane in profile.lanes
        )
        or request.runtime_port != profile.runtime_port
    ):
        raise ValueError("First hostname setup requires an active owned lane and its runtime port.")
    target = record_store.read_dokploy_target_record(
        context_name=request.context, instance_name="testing"
    )
    target_id = record_store.read_dokploy_target_id_record(
        context_name=request.context, instance_name="testing"
    )
    provider = record_store.read_provider_target_record(
        context_name=request.context, instance_name="testing"
    )
    if (
        target.target_type != "compose"
        or target_id.source_label != CREATE_COMPOSE_SOURCE_LABEL
        or request.target_id
        and request.target_id != target_id.target_id
        or provider.to_deployed_target_reference()
        != ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        ).to_deployed_target_reference()
        or target.env
        or target.domains
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
    domain = request.domains[0]
    replacement = target.model_copy(
        update={
            "domains": request.domains,
            "updated_at": utc_now_timestamp(),
            "source_label": "service:dokploy-targets:setup:reconcile-compose-domain",
        }
    )
    target_key = dokploy_generic_web_provider_target_key(provider)
    route_ids: list[str] = []
    effect_started = False

    def require_unprotected_binding() -> None:
        evidence = record_store.read_disposable_lane_safety(product, request.context, "testing")
        if evidence is not None and (
            evidence.usage == "in_use"
            or evidence.data == "only_copy"
            or evidence.production_resources in {"shared", "separation_ready"}
        ):
            raise ValueError("Testing use, data custody or resource sharing protects this lane.")
        if (
            record_store.read_product_profile_record(product) != profile
            or record_store.read_dokploy_target_record(
                context_name=request.context, instance_name="testing"
            )
            != target
            or record_store.read_dokploy_target_id_record(
                context_name=request.context, instance_name="testing"
            )
            != target_id
            or record_store.read_provider_target_record(
                context_name=request.context, instance_name="testing"
            )
            != provider
        ):
            raise ValueError("Testing bindings changed during hostname qualification.")

    def checkpoint() -> None:
        from control_plane.application_state_volume import require_no_held_state_volume

        require_no_held_state_volume(record_store, request.context)
        if _lane_has_usage_or_data_commitments(record_store, request):
            raise ValueError(
                "First hostname setup cannot change a lane that has deployed or served."
            )
        require_unprotected_binding()
        if any(
            other.provider_id == provider.provider_id
            and other.target_id == provider.target_id
            and (other.context, other.instance) != (request.context, "testing")
            for other in record_store.list_physical_provider_target_records()
        ):
            raise ValueError("The testing compose is shared with another lane.")
        require_exclusive_testing_application_hostname(
            hostname=domain, target=target, targets=record_store.list_dokploy_target_records()
        )
        claims = {
            retained_hostname_identity(value)
            for other in record_store.list_product_profile_records()
            for lane in other.lanes
            if (other.product, lane.context, lane.instance) != (product, request.context, "testing")
            for value in (lane.base_url, lane.health_url)
            if value
        } | {
            binding_domain.domain_name
            for binding in record_store.list_route_binding_records()
            for binding_domain in binding.domains
        }
        if any(
            claim == domain or claim.startswith("*.") and domain.endswith(claim[1:])
            for claim in claims
        ):
            raise ValueError("The hostname belongs to existing ingress authority.")
        if _target_reservations(
            record_store,
            product=product,
            context=request.context,
            target_key=target_key,
            target_id=target_id.target_id,
            require_complete=True,
        ):
            raise ValueError("A provider operation still owns the testing target.")
        live = fetch_dokploy_target_payload_for_setup(host, token, "compose", target_id.target_id)
        require_empty_compose_source(live, target_id.target_id)
        _placement({**live, "autoDeploy": False}, target_id.target_id)
        if live.get("env") not in (None, ""):
            raise ValueError("First hostname setup requires empty provider runtime settings.")
        if fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=target_id.target_id
        ):
            raise ValueError("First hostname setup never rewrites existing provider routes.")
        inspections: tuple[tuple[str, str, dict[str, str | int]], ...] = (
            (
                "deployments",
                "/api/deployment.allByType",
                {"id": target_id.target_id, "type": "compose"},
            ),
            ("queue", "/api/deployment.queueList", {}),
            (
                "schedules",
                "/api/schedule.list",
                {"id": target_id.target_id, "scheduleType": "compose"},
            ),
        )
        for kind, path, query in inspections:
            rows = api.dokploy_request(host=host, token=token, path=path, query=query)
            facet = _inventory(lambda: rows, kind=kind, target_id=target_id.target_id)
            if facet.code != "proved" or kind == "deployments" and rows != []:
                raise ValueError("Provider testing work or serving history is present or unknown.")
        if (
            fetch_dokploy_target_payload_for_setup(host, token, "compose", target_id.target_id)
            != live
        ):
            raise ValueError("Provider placement changed during first hostname checks.")
        require_unprotected_binding()
        check()

    def before_write(existing: bool) -> None:
        nonlocal effect_started
        if existing:
            raise ValueError("First hostname setup cannot rewrite an existing route.")
        checkpoint()
        effect_started = True

    def apply_provider() -> None:

        route_ids.append(
            compose.ensure_compose_web_domain_route(
                host=host,
                token=token,
                compose_id=target_id.target_id,
                domain_host=domain,
                runtime_port=profile.runtime_port,
                before_write=before_write,
            )
        )
        routes = fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=target_id.target_id
        )
        if len(routes) != 1 or any(
            routes[0].get(key) != value
            for key, value in {
                "domainId": route_ids[0],
                "composeId": target_id.target_id,
                "host": domain,
                "serviceName": "web",
                "port": profile.runtime_port,
                "https": True,
                "path": "/",
                "internalPath": "/",
                "certificateType": "none",
            }.items()
        ):
            raise ValueError("First hostname provider readback does not match the exact route.")
        check()

    lock = record_store.testing_replacement_execution_lock(
        testing_event_execution_lock_name(target_key)
    )
    try:
        with lock as held_check:
            if held_check is None:
                raise ValueError("A testing executor for this target is still active.")
            check: Callable[[], None] = held_check
            checkpoint()
            if request.mode == "apply":
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
        if effect_started:
            raise DokployComposeDomainPartialError(
                request=request,
                compose_id=target_id.target_id,
                completed_domain_ids=route_ids,
                pending_domain=domain,
                stage="first-hostname",
            ) from error
        raise
    return DokployComposeDomainReconcileResult(
        applied=request.mode == "apply",
        target_record=replacement if request.mode == "apply" else target,
        target_id_record=target_id,
        domains=request.domains,
        runtime_port=profile.runtime_port,
        route_domain_ids=tuple(route_ids),
    )
