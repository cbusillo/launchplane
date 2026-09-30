"""Launchplane's own grant for the work it starts from source-control events.

Launchplane needs no caller grant to verify a build and deploy it to that
product's testing lane and PR previews (DIRECTION.md). The reconciler is the only
code that builds this grant. It is stored only on the testing lane's stable target
replacement, which the worker re-checks before running; no HTTP route builds or
accepts it, and every other worker path refuses it. A preview runs in-process,
so the reconciler checks its destination directly instead.
"""

from __future__ import annotations

from control_plane.contracts.durable_operation_authorization import (
    LAUNCHPLANE_RECONCILE_SUBJECT,
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_product_driver_http import product_profile_uses_odoo_driver
from control_plane.product_repository_identity import (
    ProductRepositoryIdentityRefusal,
    resolve_product_repository_identity,
)

TESTING_TARGET_REPLACEMENT_ACTION = "odoo_target_replacement_apply.execute"
TESTING_INSTANCE = "testing"


def build_launchplane_reconcile_authorization(
    *, product: str, context: str, authorized_at: str
) -> DurableOperationAuthorization:
    """The grant on a testing-lane stable target replacement the reconciler queues."""
    return DurableOperationAuthorization(
        grant="launchplane_reconcile",
        action=TESTING_TARGET_REPLACEMENT_ACTION,
        product=product,
        context=context,
        instances=(TESTING_INSTANCE,),
        authorized_at=authorized_at,
        caller=DurableOperationCallerIdentity(
            identity_type="launchplane_reconcile", subject=LAUNCHPLANE_RECONCILE_SUBJECT
        ),
    )


def launchplane_reconcile_authorization_allows(
    *,
    authorization: DurableOperationAuthorization,
    product: str,
    context: str,
    instances: tuple[str, ...],
    record_store: object,
) -> bool:
    """Accept the grant only for the product's own testing lane, read from its profile now.

    The product must also have a repository identity in Launchplane's repository
    inventory that any ids stored on its profile agree with.
    """
    if (
        authorization.grant != "launchplane_reconcile"
        or authorization.caller.identity_type != "launchplane_reconcile"
        or authorization.action != TESTING_TARGET_REPLACEMENT_ACTION
        or authorization.product != product.strip()
        or authorization.context != context.strip().lower()
        or authorization.instances != tuple(value.strip().lower() for value in instances)
        or authorization.instances != (TESTING_INSTANCE,)
    ):
        return False
    profile = _reconcilable_profile(record_store, authorization.product)
    return profile is not None and any(
        lane.instance.strip().lower() == TESTING_INSTANCE
        and lane.context.strip().lower() == authorization.context
        for lane in profile.lanes
    )


def launchplane_reconcile_preview_destination_allowed(
    *, record_store: object, product: str, context: str, preview_slug: str
) -> bool:
    """A reconcile may change only the product's own previews, in its preview context."""
    profile = _reconcilable_profile(record_store, product.strip())
    slug = preview_slug.strip().lower()
    return (
        profile is not None
        and profile.preview.enabled
        and bool(slug)
        and profile.preview.context.strip().lower() == context.strip().lower()
        and slug not in {lane.instance.strip().lower() for lane in profile.lanes}
    )


def _reconcilable_profile(
    record_store: object, product: str
) -> LaunchplaneProductProfileRecord | None:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        return None
    try:
        profile = read_profile(product)
    except FileNotFoundError:
        return None
    if (
        not isinstance(profile, LaunchplaneProductProfileRecord)
        or not profile.is_active
        or not product_profile_uses_odoo_driver(profile)
    ):
        return None
    try:
        # The repository inventory, not the profile, is the authority for the identity.
        resolve_product_repository_identity(record_store, profile)
    except ProductRepositoryIdentityRefusal:
        return None
    return profile
