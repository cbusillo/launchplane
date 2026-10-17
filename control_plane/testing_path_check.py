"""Read-only Odoo copy and task prerequisites, using the execution validators."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_testing_restore import (
    TestingCredentialsOverlap,
    TestingCredentialVersionsMissing,
    require_distinct_testing_credentials,
)
from control_plane.odoo_walkthrough import WalkthroughPrerequisite, read_walkthrough_prerequisites
from control_plane.product_path_check import ActionAllowed, PathCheckStep
from control_plane.contracts.odoo_walkthrough import (
    OdooWalkthroughRequest,
    WALKTHROUGH_ACTION,
    WALKTHROUGH_PLAN_ACTION,
)
from control_plane.testing_replacement_recovery import service_testing_operation
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.odoo_testing_usage import read_odoo_testing_usage
from control_plane.lane_usage import evaluate_lane_usage
from control_plane.testing_replacement_bootstrap import (
    TestingBootstrapPrerequisiteError,
    require_testing_restore_policy,
    require_testing_credential_safety,
    testing_credential_omissions,
    testing_restore_source_binding,
)

_WALKTHROUGH_DESCRIPTIONS = {
    WalkthroughPrerequisite.IDENTITY: "Verify the testing runtime identity and its separate HTTPS origin.",
    WalkthroughPrerequisite.GENERATION: "Verify the current generation still owns its independent target and resources.",
    WalkthroughPrerequisite.GENERATION_MISSING: "Create a fresh sanitized testing generation before provisioning a task account.",
    WalkthroughPrerequisite.GENERATION_INCOMPLETE: "Wait for the testing generation to complete its sanitized copy.",
    WalkthroughPrerequisite.USE: "Resolve the testing hold, protected use or sole-copy custody before provisioning.",
    WalkthroughPrerequisite.PHYSICAL_COPY: "Verify the physical testing copy, storage, network and image.",
    WalkthroughPrerequisite.RUNTIME: "Verify the running testing target matches the generation.",
    WalkthroughPrerequisite.ORIGIN: "Verify the testing HTTPS origin belongs to this target.",
    WalkthroughPrerequisite.DATABASE: "Verify the recorded testing database matches the running copy.",
    WalkthroughPrerequisite.ISOLATION_OR_USAGE: "Verify connection isolation and the absence of protected staff sessions.",
    WalkthroughPrerequisite.CREDENTIAL: "Supply the task id to verify its account credential custody.",
}


def read_testing_prerequisites(
    *,
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    root: Path | None,
    source_mode: Literal["ssh", "container-export"],
    task_id: str | None,
    operation_id: str | None,
    action_allowed: ActionAllowed,
    caller_can_provision: bool,
    caller_can_generate: bool,
) -> tuple[PathCheckStep, ...]:
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    context = lane.context
    steps: list[PathCheckStep] = []

    def check(step_id: str, code: str, description: str, read: Callable[[], object]) -> None:
        state: Literal["clear", "blocked", "unknown"] = "clear"
        try:
            read()
        except TestingCredentialsOverlap:
            state, code = "blocked", "testing_credentials_overlap"
            description = "Generate independent testing-local credentials; an existing value equals production's."
        except TestingCredentialVersionsMissing:
            state, code = "blocked", "testing_credential_versions_missing"
            description = "Configure the missing managed local credentials before comparing testing and production."
        except TestingBootstrapPrerequisiteError as error:
            state, code = "blocked", error.code
            description = str(error)  # Fixed code-owned text, never the underlying exception.
        except ValueError:
            state = "blocked"
        except Exception:  # noqa: BLE001 - private provider/storage text never leaves this read.
            state = "unknown"
        steps.append(
            PathCheckStep(
                step_id=step_id,
                state=state,
                code=code if state != "clear" else step_id + "_clear",
                description=description if state != "clear" else "This prerequisite passed.",
                fix="none" if state == "clear" else "wait" if state == "unknown" else "by_hand",
            )
        )

    def provision_authority() -> None:
        # Evaluate both plan and execute even when one is refused.
        allowed = [
            action_allowed(action, context, ("testing",))
            for action in (WALKTHROUGH_PLAN_ACTION, WALKTHROUGH_ACTION)
        ]
        if not caller_can_provision or not all(allowed):
            raise ValueError("The caller cannot provision testing accounts.")

    check(
        "testing_provision_authority",
        "testing_provision_not_allowed",
        "Use an identity permitted to plan and provision this product's testing task accounts.",
        provision_authority,
    )
    steps[-1] = (
        steps[-1].model_copy(update={"fix": "grant"}) if steps[-1].state == "blocked" else steps[-1]
    )
    steps.append(
        PathCheckStep(
            step_id="testing_generation_authority",
            state="clear" if caller_can_generate else "blocked",
            code="testing_generation_allowed"
            if caller_can_generate
            else "testing_generation_needs_director_agent",
            description="Initial generation and replacement resource preparation require the Director agent.",
            fix="none" if caller_can_generate else "by_hand",
        )
    )

    check(
        "testing_restore_policy",
        "testing_restore_policy_unconfigured",
        "Configure a testing-only restore policy pointing at this product's production lane.",
        lambda: require_testing_restore_policy(profile, context),
    )
    # Independent of policy, generation and resource records: this is the OPW overlap gate.
    check(
        "testing_credentials",
        "testing_credentials_unverified",
        "Verify testing-local managed credentials are distinct from production.",
        lambda: require_distinct_testing_credentials(store, profile, context),
    )
    check(
        "testing_credential_safety",
        "testing_credentials_unsafe",
        "Verify only lane-local credentials can reach the testing recipient.",
        lambda: require_testing_credential_safety(
            store, context, testing_credential_omissions(store, context, None)
        ),
    )

    def source() -> None:
        target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
        # An independent replacement does not inherit env from the retained target.
        testing_restore_source_binding(
            store, profile, context, target.model_copy(update={"env": {}}), None, source_mode
        )

    check(
        "testing_restore_source",
        "testing_restore_source_unconfigured",
        "Configure the selected production-copy source through its managed records.",
        source,
    )

    def resources() -> None:
        target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
        target_id = store.read_dokploy_target_id_record(
            context_name=context, instance_name="testing"
        )
        provider = store.read_provider_target_record(context_name=context, instance_name="testing")
        if target.target_type != "compose" or provider.target_id != target_id.target_id:
            raise ValueError("Testing resource authority disagrees.")
        if target.pending_public_host_effects:
            raise ValueError("Testing has pending route effects.")
        if any(
            item.target_id == target_id.target_id
            and (item.context, item.instance) != (context, "testing")
            for item in store.list_dokploy_target_id_records()
        ):
            raise ValueError("Testing target aliases another lane.")
        if any(
            item.provider_id == "dokploy"
            and item.target_id == target_id.target_id
            and (item.context, item.instance) != (context, "testing")
            for item in store.list_physical_provider_target_records()
        ):
            raise ValueError("Testing provider target aliases another lane.")

    check(
        "testing_resources",
        "testing_resources_refused",
        "Reconcile testing target bindings, aliases and pending route effects before preparing resources.",
        resources,
    )

    def allocation() -> None:
        assert operation_id is not None
        preparation = store.read_disposable_resource_preparation(operation_id)
        if preparation is None:
            raise ValueError("Retained resource preparation is missing.")
        if (preparation.product, preparation.context) != (profile.product, context):
            raise ValueError("Retained resource preparation belongs to another lane.")
        if preparation.initial_request_sha256:
            store.require_initial_testing_lane_available(preparation, allow_bootstrap_resume=True)
        else:
            operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
            if (
                not service_testing_operation(operation)
                or operation.status != "reconciliation_required"
            ):
                raise ValueError("The operation is not eligible for testing replacement.")
        if not preparation.replacement_target_id or not preparation.resources_binding_sha256:
            raise ValueError("Independent resources are not qualified yet.")

    if operation_id is None:
        steps.append(
            PathCheckStep(
                step_id="testing_resource_allocation",
                state="unknown",
                code="testing_operation_unselected",
                description="Supply the original preparation or replacement operation id to check resource qualification.",
                fix="by_hand",
            )
        )
    else:
        check(
            "testing_resource_allocation",
            "testing_resources_refused",
            "Prepare and qualify independent resources using the original eligible testing operation.",
            allocation,
        )

    def current_use() -> None:
        if root is None:
            raise RuntimeError("Testing usage needs the service runtime.")
        target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
        target_id = store.read_dokploy_target_id_record(
            context_name=context, instance_name="testing"
        )
        usage = read_odoo_testing_usage(
            target=target,
            target_id=target_id,
            control_plane_root=root,
            database_url=store.database_url,
        )
        evidence = store.read_disposable_lane_safety(profile.product, context, "testing")
        known_use = evidence is not None and evidence.usage == "in_use"
        decision = evaluate_lane_usage(
            store,
            profile=profile,
            target=target,
            target_id=target_id,
            instance="testing",
            usage="in_use" if known_use else usage.usage,
        )
        if decision.protected:
            if (
                known_use
                or usage.usage == "in_use"
                or target.policies.staff_testing_hold is not None
            ):
                raise ValueError("Testing use is protected.")
            raise RuntimeError("Current testing use could not be verified.")

    check(
        "testing_generation_use",
        "testing_initial_usage_unverified",
        "Verify current testing use before replacing or creating a generation; preserve staff holds.",
        current_use,
    )
    if root is None:
        steps.append(
            PathCheckStep(
                step_id="testing_provision",
                state="unknown",
                code="testing_probe_unavailable",
                description="The service runtime is required to inspect testing provision prerequisites.",
                fix="wait",
            )
        )
        return tuple(steps)
    request = (
        OdooWalkthroughRequest(
            product=profile.product, context=context, task_id=task_id, operation="provision"
        )
        if task_id is not None
        else None
    )
    for code, state in read_walkthrough_prerequisites(
        store, root, profile.product, context, request
    ):
        steps.append(
            PathCheckStep(
                step_id=code.value,
                state=state,
                code=code.value,
                description=(
                    "This prerequisite passed."
                    if state == "clear"
                    else _WALKTHROUGH_DESCRIPTIONS[code]
                ),
                fix="none" if state == "clear" else "wait" if state == "unknown" else "by_hand",
            )
        )
    return tuple(steps)
