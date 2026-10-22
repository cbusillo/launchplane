from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from control_plane import secrets
from control_plane.contracts.read_coverage import ReadCoverage, read_coverage
from control_plane.contracts.secret_record import SecretScope
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_secret_copy import ProductSecretBindingMetadata


class LaneBindingSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    bindings: tuple[ProductSecretBindingMetadata, ...]
    coverage: ReadCoverage


class ProductBindingResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str
    selection_scopes: tuple[SecretScope, ...]
    lanes: tuple[LaneBindingSelection, ...]
    coverage: ReadCoverage


def product_binding_resolution(
    store: secrets.SecretBindingSelectionStore,
    *,
    profile: LaunchplaneProductProfileRecord,
    integration: str,
    scopes: frozenset[SecretScope],
    scope_read_allowed: Callable[[SecretScope, str, str], bool],
) -> ProductBindingResolution:
    lanes = []
    authorization_complete = True
    for lane in profile.lanes:
        lane_authorized = all(
            scope_read_allowed(scope, lane.context, lane.instance) for scope in scopes
        )
        selected = secrets.resolve_effective_secret_bindings_from_store(
            record_store=store,
            integration=integration,
            context_name=lane.context,
            instance_name=lane.instance,
            scopes=scopes,
        )
        visible = []
        if not lane_authorized:
            selected = {}
        for key, (binding, record) in sorted(selected.items()):
            visible.append(
                ProductSecretBindingMetadata(
                    binding_key=key,
                    name=record.name,
                    scope=record.scope,
                    context=record.context,
                    instance=record.instance,
                    secret_class=binding.declared_secret_class,
                    sharing_reason=binding.sharing_reason,
                    version_id=record.current_version_id,
                )
            )
        authorization_complete = authorization_complete and lane_authorized
        lanes.append(
            LaneBindingSelection(
                context=lane.context,
                instance=lane.instance,
                bindings=tuple(visible),
                coverage=read_coverage(
                    store,
                    collections=("product_profiles", "secret_records", "secret_bindings"),
                    population="configured_binding_selection_for_requested_integration_and_scopes",
                    authorization_complete=lane_authorized,
                    exclusions=("runtime_delivery_and_value_validation",),
                ),
            )
        )
    return ProductBindingResolution(
        integration=integration,
        selection_scopes=tuple(
            scope for scope in ("global", "context", "context_instance") if scope in scopes
        ),
        lanes=tuple(lanes),
        coverage=read_coverage(
            store,
            collections=("product_profiles", "secret_records", "secret_bindings"),
            population="declared_product_lanes_for_requested_integration_and_scopes",
            authorization_complete=authorization_complete,
            exclusions=(
                "historical_and_undeclared_lanes",
                "other_integrations",
                "runtime_delivery",
            ),
        ),
    )
