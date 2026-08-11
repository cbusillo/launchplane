from __future__ import annotations

from functools import lru_cache


_NON_DESCRIPTOR_INSTANCE_SCOPED_AUTHZ_ACTIONS = frozenset(
    {
        "backup_gate.write",
        "deployment.read",
        "inventory.read",
        "product_profile.health_monitoring.apply",
        "product_profile.health_monitoring.plan",
        "product_profile.prelaunch_rebuild.apply",
        "product_profile.prelaunch_rebuild.plan",
        "product_retirement.apply",
        "product_retirement.plan",
        "promotion.write",
        "promotion.read",
        "route_binding.external.apply",
        "route_binding.external.plan",
        "target_logs.read",
    }
)
_EXACT_INSTANCE_WORKFLOW_AUTHZ_ACTIONS = frozenset(
    {
        "product_profile.health_monitoring.apply",
        "product_profile.health_monitoring.plan",
        "product_profile.prelaunch_rebuild.apply",
        "product_profile.prelaunch_rebuild.plan",
        "product_retirement.apply",
        "product_retirement.plan",
        "route_binding.external.apply",
        "route_binding.external.plan",
    }
)
_INSTANCE_PINNED_WORKFLOW_AUTHZ_ACTIONS = frozenset(
    {
        "ingress_route.apply",
        "ingress_route.plan",
    }
)
_DUAL_SCOPE_AUTHZ_ACTIONS = frozenset(
    {
        "driver.read",
        "ingress_route.apply",
        "ingress_route.plan",
        "operations.read",
        "product_config.apply",
        "product_config.plan",
        "product_environment.read",
        "route_binding.apply",
        "route_binding.read",
        "secret.list",
        "secret.read",
    }
)


@lru_cache(maxsize=1)
def instance_scoped_authz_actions() -> frozenset[str]:
    from control_plane.drivers.registry import instance_scoped_driver_authz_actions

    return (
        instance_scoped_driver_authz_actions()
        | _NON_DESCRIPTOR_INSTANCE_SCOPED_AUTHZ_ACTIONS
        | _DUAL_SCOPE_AUTHZ_ACTIONS
    )


@lru_cache(maxsize=1)
def exclusively_instance_scoped_authz_actions() -> frozenset[str]:
    return instance_scoped_authz_actions() - _DUAL_SCOPE_AUTHZ_ACTIONS


@lru_cache(maxsize=1)
def exact_instance_workflow_authz_actions() -> frozenset[str]:
    return _EXACT_INSTANCE_WORKFLOW_AUTHZ_ACTIONS


@lru_cache(maxsize=1)
def instance_pinned_workflow_authz_actions() -> frozenset[str]:
    return _INSTANCE_PINNED_WORKFLOW_AUTHZ_ACTIONS


@lru_cache(maxsize=1)
def operational_readiness_authz_actions() -> frozenset[str]:
    from control_plane.drivers.registry import operational_readiness_driver_authz_actions

    return operational_readiness_driver_authz_actions()
