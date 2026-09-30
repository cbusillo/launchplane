"""Launchplane's own grant for the work it starts from source-control events.

Launchplane needs no caller grant to verify a build and deploy it to that
product's testing lane and PR previews (DIRECTION.md). The reconciler is the only
code that builds this grant, and it covers only those two operations; no HTTP
route builds or accepts it, and every other worker path refuses it.
"""

from __future__ import annotations

from typing import Literal

from control_plane.contracts.durable_operation_authorization import (
    LAUNCHPLANE_RECONCILE_SUBJECT,
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_product_driver_http import product_profile_uses_odoo_driver

LaunchplaneReconcileOperation = Literal["odoo_testing_target_replacement", "odoo_preview_apply"]
_RECONCILE_ACTIONS: dict[LaunchplaneReconcileOperation, str] = {
    "odoo_testing_target_replacement": "odoo_target_replacement_apply.execute",
    "odoo_preview_apply": "odoo_preview_apply.execute",
}
TESTING_INSTANCE = "testing"


def build_launchplane_reconcile_authorization(
    *,
    operation: LaunchplaneReconcileOperation,
    product: str,
    context: str,
    instances: tuple[str, ...],
    authorized_at: str,
) -> DurableOperationAuthorization:
    return DurableOperationAuthorization(
        grant="launchplane_reconcile",
        action=_RECONCILE_ACTIONS[operation],
        product=product,
        context=context,
        instances=instances,
        authorized_at=authorized_at,
        caller=DurableOperationCallerIdentity(
            identity_type="launchplane_reconcile", subject=LAUNCHPLANE_RECONCILE_SUBJECT
        ),
    )


def launchplane_reconcile_authorization_allows(
    *,
    authorization: DurableOperationAuthorization,
    operation: LaunchplaneReconcileOperation,
    product: str,
    context: str,
    instances: tuple[str, ...],
    record_store: object,
) -> bool:
    """Accept the grant only for its operation and the product's own destination, read now."""
    if (
        authorization.grant != "launchplane_reconcile"
        or authorization.caller.identity_type != "launchplane_reconcile"
        or authorization.action != _RECONCILE_ACTIONS[operation]
        or authorization.product != product.strip()
        or authorization.context != context.strip().lower()
        or authorization.instances != tuple(value.strip().lower() for value in instances)
    ):
        return False
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        return False
    try:
        profile = read_profile(authorization.product)
    except FileNotFoundError:
        return False
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        return False
    if (
        not profile.is_active
        or not profile.repository_id
        or not product_profile_uses_odoo_driver(profile)
    ):
        return False
    if operation == "odoo_testing_target_replacement":
        return authorization.instances == (TESTING_INSTANCE,) and any(
            lane.instance.strip().lower() == TESTING_INSTANCE
            and lane.context.strip().lower() == authorization.context
            for lane in profile.lanes
        )
    return (
        profile.preview.enabled
        and profile.preview.context.strip().lower() == authorization.context
        and len(authorization.instances) == 1
        and authorization.instances[0]
        not in {lane.instance.strip().lower() for lane in profile.lanes}
    )
