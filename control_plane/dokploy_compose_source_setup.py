from pathlib import Path
from contextlib import nullcontext
from collections.abc import Callable
import re
from sqlalchemy.exc import SQLAlchemyError
from typing import TYPE_CHECKING

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy.api import JsonObject
from control_plane.dokploy.target_source_setup import (
    configure_empty_compose_source,
    DokployComposeSourcePartialError,
    repository_source_url,
    require_empty_compose_source,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp

if TYPE_CHECKING:
    from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope


def resolve_compose_source_profile(
    store: PostgresRecordStore,
    context: str,
) -> LaunchplaneProductProfileRecord:
    profiles = store.list_product_profile_records()
    owners = product_context_owner_map(profiles)
    products = owners.get(context, frozenset())
    if len(products) != 1:
        raise ValueError("Compose source requires exclusive product context ownership.")
    (product,) = products
    if not is_exclusive_product_context(context=context, product=product, owners=owners):
        raise ValueError("Compose source requires a canonical product context.")
    profile = next(profile for profile in profiles if profile.product == product)
    if profile.driver_id != "generic-web":
        raise ValueError("Repository compose source setup requires a generic-web product.")
    return profile


def complete_compose_source(
    *,
    control_plane_root_path: Path,
    record_store: PostgresRecordStore,
    request: "DokployTargetSetupEnvelope",
    director_agent: bool = False,
) -> dict[str, object]:
    from control_plane.dokploy_target_setup_http import (
        fetch_dokploy_target_payload_for_setup,
        mutate_dokploy_payload_for_target_setup,
    )

    profile = resolve_compose_source_profile(record_store, request.context)
    if not any(
        lane.context == request.context and lane.instance == request.instance
        for lane in profile.lanes
    ):
        raise ValueError("Source completion requires an existing product testing lane.")
    try:
        target = record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        target_id = record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        provider = record_store.read_provider_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as error:
        raise ValueError(
            "Source completion requires tracked target, target-id and provider records."
        ) from error
    if (
        target.target_type != "compose"
        or target.custom_git_url
        or target.custom_git_branch
        or target.git_branch
    ):
        raise ValueError("Source completion requires an empty tracked compose target.")
    if target.source_type not in ("", "git", "github", "raw"):
        raise ValueError("Tracked compose has a different source type.")
    projection = ProviderTargetRecord.from_dokploy_records(
        target_record=target, target_id_record=target_id
    )
    if provider.to_deployed_target_reference() != projection.to_deployed_target_reference():
        raise ValueError("Source completion requires matching tracked provider binding.")
    if any(
        other.provider_id == provider.provider_id
        and other.target_id == provider.target_id
        and (other.context, other.instance) != (target.context, target.instance)
        for other in record_store.list_physical_provider_target_records()
    ):
        raise ValueError("Compose source target is shared with another lane.")
    url = repository_source_url(profile.repository)
    host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root_path)
    live = fetch_dokploy_target_payload_for_setup(host, token, "compose", target_id.target_id)
    require_empty_compose_source(live, target_id.target_id)
    if director_agent:
        from control_plane.private_testing_activation import _placement

        if not profile.is_active or not re.fullmatch(r"[a-f0-9]{40}", request.custom_git_branch):
            raise ValueError(
                "Director source completion requires active ownership and an immutable commit."
            )
        if target.domains or target.public_hosts or target.pending_public_host_effects:
            raise ValueError("Director source completion requires an unpublished testing target.")
        _placement({**live, "autoDeploy": False}, target_id.target_id)
    director_apply = director_agent and request.mode == "apply"
    replacement = target.model_copy(
        update={
            "source_type": "git",
            "custom_git_url": url,
            "custom_git_branch": request.custom_git_branch,
            "compose_path": request.compose_path,
            "updated_at": utc_now_timestamp(),
            "source_label": "service:complete-compose-source",
        }
    )

    def checkpoint(before: JsonObject) -> None:
        if not director_apply:
            return
        from control_plane.disposable_lanes import require_disposable_lane
        from control_plane.generic_web_testing_exclusion import _inventory, _target_reservations
        from control_plane.private_testing_activation import _placement
        from control_plane.dokploy import api

        # An empty source cannot auto-deploy. The single source update disables
        # that provider default atomically, and its readback requires False.
        _placement({**before, "autoDeploy": False}, target_id.target_id)
        if before != live:
            raise ValueError("Provider source or placement changed after completion review.")
        safety_binding = require_disposable_lane(
            record_store,
            product=profile.product,
            context=request.context,
            instance=request.instance,
        )
        if _target_reservations(
            record_store,
            product=profile.product,
            context=request.context,
            target_key=target_key,
            target_id=target_id.target_id,
        ):
            raise ValueError("Testing target has an unresolved provider operation.")
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
            facet = _inventory(
                lambda: api.dokploy_request(host=host, token=token, path=path, query=query),
                kind=kind,
                target_id=target_id.target_id,
            )
            if facet.code != "proved":
                raise ValueError("Provider testing work is active or unknown.")
        if (
            fetch_dokploy_target_payload_for_setup(host, token, "compose", target_id.target_id)
            != before
        ):
            raise ValueError("Provider changed during source exclusion reads.")
        if (
            require_disposable_lane(
                record_store,
                product=profile.product,
                context=request.context,
                instance=request.instance,
            )
            != safety_binding
        ):
            raise ValueError("Testing safety changed during source exclusion reads.")
        if _target_reservations(
            record_store,
            product=profile.product,
            context=request.context,
            target_key=target_key,
            target_id=target_id.target_id,
        ):
            raise ValueError("Provider ownership changed during source exclusion reads.")
        check()

    effect_started = False

    def mutate_source(host: str, token: str, path: str, payload: JsonObject) -> JsonObject:
        nonlocal effect_started
        effect_started = True
        return mutate_dokploy_payload_for_target_setup(host, token, path, payload)

    def apply_provider() -> None:
        configure_empty_compose_source(
            host=host,
            token=token,
            compose_id=target_id.target_id,
            custom_git_url=url,
            branch=request.custom_git_branch,
            compose_path=request.compose_path,
            fetch_target_payload=fetch_dokploy_target_payload_for_setup,
            mutate_provider=mutate_source,
            before_update=checkpoint if director_apply else None,
        )

    from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
    from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name

    target_key = generic_web_provider_target_key(
        provider_id=provider.provider_id,
        context=request.context,
        instance=request.instance,
        target_type="compose",
        target_id=target_id.target_id,
    )
    lock = (
        record_store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(target_key)
        )
        if director_apply
        else nullcontext(lambda: None)
    )
    try:
        with lock as held_check:
            if held_check is None:
                raise ValueError("Testing executor is still active.")
            check: Callable[[], None] = held_check
            if director_apply:
                checkpoint(live)
            if request.mode == "apply":
                record_store.complete_dokploy_compose_source(
                    expected_profile=profile,
                    expected_record=target,
                    expected_target_id=target_id,
                    expected_provider_target=provider,
                    replacement_record=replacement,
                    apply_provider=apply_provider,
                )
    except (RuntimeError, SQLAlchemyError) as error:
        if effect_started:
            raise DokployComposeSourcePartialError(
                "Source effects began before exclusion failed; preserve the original key and reconcile."
            ) from error
        raise ValueError("Source exclusion unavailable before provider effects.") from error
    return {
        "mode": request.mode,
        "operation": request.operation,
        "context": request.context,
        "instance": request.instance,
        "applied": request.mode == "apply",
        "route_domain_ids": [],
        "setup": {
            "applied": request.mode == "apply",
            "source": {
                "repository": profile.repository,
                "custom_git_url": url,
                "custom_git_branch": request.custom_git_branch,
                "compose_path": request.compose_path,
            },
            "target_id_record": target_id.model_dump(mode="json"),
            "target_record": replacement.model_dump(mode="json", exclude={"env"}),
            "provider_target_record": provider.model_dump(mode="json"),
        },
    }
