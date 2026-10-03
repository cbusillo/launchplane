from pathlib import Path
from typing import TYPE_CHECKING

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy.target_source_setup import (
    configure_empty_compose_source,
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

    def apply_provider() -> None:
        configure_empty_compose_source(
            host=host,
            token=token,
            compose_id=target_id.target_id,
            custom_git_url=url,
            branch=request.custom_git_branch,
            compose_path=request.compose_path,
            fetch_target_payload=fetch_dokploy_target_payload_for_setup,
            mutate_provider=mutate_dokploy_payload_for_target_setup,
        )

    if request.mode == "apply":
        record_store.complete_dokploy_compose_source(
            expected_profile=profile,
            expected_record=target,
            expected_target_id=target_id,
            expected_provider_target=provider,
            replacement_record=replacement,
            apply_provider=apply_provider,
        )
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
