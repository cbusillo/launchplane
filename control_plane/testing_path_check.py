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
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.product_path_check import ActionAllowed, PathCheckStep
from control_plane.contracts.odoo_walkthrough import (
    OdooWalkthroughRequest,
    WALKTHROUGH_ACTION,
    WALKTHROUGH_PLAN_ACTION,
)
from control_plane.testing_replacement_resources import (
    resource_context,
    TestingReplacementResourcesRequest,
)
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.testing_initial_generation import (
    require_initial_usage,
    require_testing_generation_usage,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_replacement_bootstrap import (
    TestingBootstrapPrerequisiteError,
    BootstrapPrerequisiteCode,
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
    WalkthroughPrerequisite.ISOLATION_OR_USAGE: "The native isolation and protected sign-in read was unavailable; repeat the read-only check.",
    WalkthroughPrerequisite.RUNNER_UNSELECTED: "The testing target did not have exactly one script runner; verify the running testing target.",
    WalkthroughPrerequisite.RUNNER_CHANGED: "The testing runner identity, image or database differs from the selected runtime.",
    WalkthroughPrerequisite.COPY_WITNESS_CHANGED: "The running database does not carry this generation's copy witness; preserve the copy.",
    WalkthroughPrerequisite.ISOLATION_REFUSED: "Connection isolation readback refused; use the supported isolation repair and readback.",
    WalkthroughPrerequisite.PROTECTED_SIGNIN: "A protected sign-in was observed since preparation; preserve that use.",
    WalkthroughPrerequisite.CREDENTIAL: "Supply the task id to verify its account access and custody.",
}


def read_testing_prerequisites(
    *,
    intent: Literal["provision", "generation", "testing-replacement"],
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
            state, code = "blocked", BootstrapPrerequisiteCode.CREDENTIALS_OVERLAP
            description = "Generate independent testing-local passwords; an existing value equals production's."
        except TestingCredentialVersionsMissing:
            state, code = "blocked", BootstrapPrerequisiteCode.CREDENTIAL_VERSIONS_MISSING
            description = "Configure the missing managed local passwords before comparing testing and production."
        except TestingBootstrapPrerequisiteError as error:
            state, code = "unknown" if error.unavailable else "blocked", error.code
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

    if intent == "provision":
        check(
            "testing_provision_authority",
            "testing_provision_not_allowed",
            "Use an identity permitted to plan and provision this product's testing task accounts.",
            provision_authority,
        )
        if steps[-1].state == "blocked" and caller_can_provision:
            steps[-1] = steps[-1].model_copy(update={"fix": "grant"})
    if intent != "provision":
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

    if intent == "provision":
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
        else:
            request = (
                OdooWalkthroughRequest(
                    product=profile.product, context=context, task_id=task_id, operation="provision"
                )
                if task_id is not None
                else None
            )
            for checkpoint, code, state in read_walkthrough_prerequisites(
                store, root, profile.product, context, request
            ):
                steps.append(
                    PathCheckStep(
                        step_id=checkpoint.value,
                        state=state,
                        code=code.value,
                        description=(
                            "This prerequisite passed."
                            if state == "clear"
                            else (
                                "Physical-copy predicate "
                                + code.value
                                + (
                                    " was not satisfied."
                                    if state == "blocked"
                                    else " could not be observed."
                                )
                                + " Preserve the original copy and inspect the selected runtime."
                                if isinstance(code, ReadbackPredicate)
                                else _WALKTHROUGH_DESCRIPTIONS[code]
                            )
                        ),
                        fix="none"
                        if state == "clear"
                        else "wait"
                        if state == "unknown"
                        else "by_hand",
                    )
                )
        if root is not None and any(
            step.step_id == WalkthroughPrerequisite.GENERATION.value and step.state == "clear"
            for step in steps
        ):
            return tuple(steps)
    snapshot_unread = False
    try:
        generation = store.read_fresh_odoo_testing_generation(profile.product, context)
    except Exception:
        snapshot_unread = True
        generation = None
        steps.append(
            PathCheckStep(
                step_id="testing_generation_snapshot",
                state="unknown",
                code="testing_generation_unread",
                description="The current generation could not be read; reviewed copy intent is unknown.",
                fix="wait",
            )
        )
    if generation is not None and generation.operation_id != operation_id:
        generation = None
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
        "Verify testing-local managed passwords are distinct from production.",
        lambda: require_distinct_testing_credentials(store, profile, context),
    )
    check(
        "testing_credential_safety",
        "testing_credentials_unsafe",
        "Verify only lane-local passwords can reach the testing recipient.",
        lambda: require_testing_credential_safety(
            store, context, testing_credential_omissions(store, context, generation)
        ),
    )

    def source() -> None:
        target = store.read_dokploy_target_record(context_name=context, instance_name="testing")
        # An independent replacement does not inherit env from the retained target.
        testing_restore_source_binding(
            store, profile, context, target.model_copy(update={"env": {}}), generation, source_mode
        )

    check(
        "testing_restore_source",
        "testing_restore_source_unconfigured",
        "Configure the selected production-copy source through its managed records.",
        source,
    )

    if snapshot_unread:
        steps = [
            step.model_copy(
                update={
                    "state": "unknown",
                    "code": "testing_generation_unread",
                    "fix": "wait",
                    "description": "The reviewed generation could not be read; its copy prerequisites remain unknown.",
                }
            )
            if step.state == "clear"
            and step.step_id in {"testing_credential_safety", "testing_restore_source"}
            else step
            for step in steps
        ]

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
        "testing_resource_binding_unverified",
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
        request = TestingReplacementResourcesRequest(
            context=context,
            operation_id=operation_id,
            initial_generation=bool(preparation.initial_request_sha256),
            source_mode=source_mode,
            reason="Read current testing prerequisites",
        )
        _, _, allocation, _, target_id, _ = resource_context(
            store, product=profile.product, request=request, allow_bootstrap_resume=True
        )
        resources = OdooTestingResources(
            product=profile.product,
            context=context,
            operation_id=operation_id,
            target_id=allocation.replacement_target_id,
            namespace=allocation.resource_namespace,
        )
        if (
            target_id.target_id != resources.target_id
            or allocation.resources_binding_sha256 != resources.binding_sha256
        ):
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
        preparation = (
            store.read_disposable_resource_preparation(operation_id) if operation_id else None
        )
        if preparation is not None:
            if (preparation.product, preparation.context) != (profile.product, context):
                raise ValueError("Preparation belongs to another lane.")
            require_initial_usage(
                root,
                store,
                profile.product,
                TestingReplacementResourcesRequest(
                    context=context,
                    operation_id=preparation.operation_id,
                    source_mode=source_mode,
                    reason="Read current testing prerequisites",
                ),
            )
        else:
            require_testing_generation_usage(
                root, store, profile.product, context, target, target_id
            )

    try:
        preparation = (
            store.read_disposable_resource_preparation(operation_id) if operation_id else None
        )
    except Exception:
        preparation = None
        steps.append(
            PathCheckStep(
                step_id="testing_generation_use",
                state="unknown",
                code="testing_preparation_unread",
                description="Retained testing use could not be selected from its preparation.",
                fix="wait",
            )
        )
    if (
        intent == "generation"
        or (preparation is not None and preparation.initial_request_sha256)
        or (intent == "provision" and preparation is None)
    ):
        check(
            "testing_generation_use",
            "testing_initial_usage_unverified",
            "Verify current testing use before replacing or creating a generation; preserve staff holds.",
            current_use,
        )
    return tuple(steps)
