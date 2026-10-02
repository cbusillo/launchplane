"""Check the integration keys a generic-web deploy delivered to its lane.

Odoo lanes read their integration settings back from the database after a deploy
(``integration_readback``). A generic-web lane has no such database: its
integrations are the managed secrets Launchplane delivers as environment
variables. This read-back runs the lane's runtime key-safety rules over those
keys after the deploy, so a mislabelled key or a shared production key with no
recorded reason shows up on the deployment record, not only at write time.

It reports and never refuses: the deploy has already happened, and a promotion
would roll a live site back on a refusal (launchplane#2768).
"""

from __future__ import annotations

import logging
from typing import cast

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deployment_record import (
    IntegrationKeyReadbackEvidence,
    IntegrationKeyReadbackFinding,
    IntegrationKeyReadbackStatus,
)
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyTarget
from control_plane.runtime_key_safety import (
    RuntimeKeySafetyPolicyReadStore,
    evaluate_runtime_key_safety_from_store,
    is_integration_runtime_key,
    latest_active_runtime_key_safety_policy,
    runtime_key_safety_environment_class,
    runtime_secret_binding_matches_target,
)

_LOGGER = logging.getLogger(__name__)
_REQUIRED_STORE_METHODS = ("list_runtime_key_safety_policy_records", "list_secret_bindings")


def integration_key_readback(
    *, record_store: object, context_name: str, instance_name: str
) -> IntegrationKeyReadbackEvidence:
    # Advisory and after the provider deploy: a failed read, such as a dropped
    # database connection, is recorded as unavailable and never interrupts the
    # deploy's completion or its deployment record.
    try:
        return _integration_key_readback(
            record_store=record_store, context_name=context_name, instance_name=instance_name
        )
    except Exception:
        _LOGGER.warning(
            "Integration key read-back unavailable for %s/%s.",
            context_name,
            instance_name,
            exc_info=True,
        )
        return IntegrationKeyReadbackEvidence(status="unavailable")


def _integration_key_readback(
    *, record_store: object, context_name: str, instance_name: str
) -> IntegrationKeyReadbackEvidence:
    if not all(callable(getattr(record_store, name, None)) for name in _REQUIRED_STORE_METHODS):
        return IntegrationKeyReadbackEvidence(status="unavailable")
    store = cast(RuntimeKeySafetyPolicyReadStore, record_store)
    try:
        policy_record = latest_active_runtime_key_safety_policy(store)
    except ValueError:
        return IntegrationKeyReadbackEvidence(status="unavailable")
    target = RuntimeKeySafetyTarget(
        context=context_name,
        instance=instance_name,
        environment_class=runtime_key_safety_environment_class(instance_name),
    )
    delivered_integration_keys = tuple(
        dict.fromkeys(
            binding.binding_key
            for binding in store.list_secret_bindings(
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                limit=None,
            )
            if binding.status == "configured"
            and runtime_secret_binding_matches_target(binding=binding, target=target)
            and is_integration_runtime_key(
                binding.binding_key, extra_markers=policy_record.integration_key_markers
            )
        )
    )
    if not delivered_integration_keys:
        return IntegrationKeyReadbackEvidence(status="skipped")
    evaluation = evaluate_runtime_key_safety_from_store(
        record_store=store,
        policy_record=policy_record,
        target=target,
        required_binding_keys=delivered_integration_keys,
        unreasoned_shared_integration_keys="report",
    )
    status: IntegrationKeyReadbackStatus = (
        "fail" if evaluation.findings else "reported" if evaluation.reported else "pass"
    )
    return IntegrationKeyReadbackEvidence(
        status=status,
        checked_binding_keys=evaluation.checked_binding_keys,
        findings=tuple(
            IntegrationKeyReadbackFinding(binding_key=finding.binding_key, code=finding.code)
            for finding in (*evaluation.findings, *evaluation.reported)
        ),
    )
