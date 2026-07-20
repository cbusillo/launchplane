from __future__ import annotations

from functools import lru_cache


_NON_DESCRIPTOR_INSTANCE_SCOPED_AUTHZ_ACTIONS = frozenset(
    {
        "backup_gate.write",
        "deployment.read",
        "inventory.read",
        "promotion.write",
        "promotion.read",
        "target_logs.read",
    }
)
_DUAL_SCOPE_AUTHZ_ACTIONS = frozenset(
    {
        "driver.read",
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
