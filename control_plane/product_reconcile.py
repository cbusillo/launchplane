"""Make a product's testing lane and PR previews run its newest verified builds.

A reconcile reads GitHub's record of the product's builds and Launchplane's own
records, decides what should change, and does it under Launchplane's own
reconcile grant (``launchplane_reconcile_authorization``): it queues the testing
lane's stable target replacement, and applies or destroys the PR's preview. It
records what it decided and did as the plan on its request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import quote, urlencode
from uuid import uuid4

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane import secrets
from control_plane.build_provenance import (
    BUILD_WORKFLOW_PATH,
    BuildProvenanceError,
    BuildProvenanceTransport,
    GitHubBuildProvenanceTransport,
    VerifiedArtifactStore,
    VerifiedBuildArtifact,
    first_parent_history,
    record_verified_build_artifact,
    verify_build_artifact,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.product_reconcile import (
    ProductReconcileRequestRecord,
    ProductReconcileTarget,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    ODOO_STABLE_TARGET_REPLACEMENT_TERMINAL_OPERATION_STATUSES,
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppIdentityError,
    mint_build_provenance_installation_token,
)
from control_plane.launchplane_reconcile_authorization import (
    TESTING_INSTANCE,
    build_launchplane_reconcile_authorization,
    launchplane_reconcile_preview_destination_allowed,
)
from control_plane.merge_train_github_token import MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.odoo_preview_apply_execution import (
    ExecuteOdooPreviewApply,
    ObserveOdooPreviewApply,
    run_odoo_preview_apply_operation,
)
from control_plane.odoo_preview_apply_http import (
    OdooPreviewApplyConfigError,
    OdooPreviewApplyEnvelope,
    OdooPreviewPlanProvenanceError,
    build_odoo_preview_apply_inputs_result,
    build_odoo_preview_plan_id,
    execute_odoo_preview_apply_result,
    issue_odoo_preview_apply_plan,
    observe_odoo_preview_apply_result,
    validate_odoo_preview_issued_plan,
    validate_odoo_preview_lifecycle_response_current,
    validate_odoo_preview_profile_authority,
)
from control_plane.odoo_product_driver_http import product_profile_uses_odoo_driver
from control_plane.odoo_target_replacement_apply_http import (
    OdooTargetReplacementApplyEnvelope,
    OdooTargetReplacementApplyLaneBusyError,
    OdooTargetReplacementApplyOperationActiveError,
    OdooTargetReplacementApplyProductMismatchError,
    OdooTargetReplacementApplyRouteDependencyError,
    enqueue_odoo_target_replacement_apply_operation,
    find_odoo_target_replacement_apply_operation_by_idempotency_key,
    odoo_target_replacement_apply_operation_store,
    resolve_odoo_target_replacement_apply_lane,
)
from control_plane.product_repository_identity import (
    ProductRepositoryIdentity,
    ProductRepositoryIdentityRefusal,
    product_repository_identity_from_inventory,
    resolve_product_repository_identity,
)
from control_plane.provider_operations import DurableProviderOperationStore
from control_plane.testing_lane_hold import (
    STAFF_TESTING_HOLD_REASON,
    is_staff_testing_hold_cancellation,
    read_staff_testing_hold,
)
from control_plane.workflows.launchplane import PreviewMutationRecordStore, find_preview_record
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewApplyInputsRequest,
    OdooPreviewDokployApplyRequest,
)

PRODUCT_RECONCILE_SWEEP_SECONDS = 30 * 60
# Long enough for a preview apply that waits for its deploy; a crashed worker's
# request is reclaimed after this, well inside one sweep.
PRODUCT_RECONCILE_LEASE_SECONDS = 20 * 60
RECONCILE_SOURCE = "launchplane-reconcile"
TESTING_DEPLOY_MAX_FAILED_ATTEMPTS = 3
TESTING_DEPLOY_MAX_ATTEMPT_CHAIN = 20
PREVIEW_APPLY_TIMEOUT_SECONDS = 600
TESTING_BUILD_RUN_PAGE_SIZE = 50
TESTING_VERIFY_LIMIT = 3
_ENDED_PREVIEW_STATES = frozenset({"destroyed", "teardown_pending"})
_LOGGER = logging.getLogger(__name__)


class ProductReconcileError(Exception):
    """The reconcile cannot decide; the request is recorded as failed with this message."""


class ProductReconcileStore(Protocol):
    def claim_next_product_reconcile_request(
        self, lease_owner: str, lease_seconds: int
    ) -> ProductReconcileRequestRecord | None: ...

    def complete_product_reconcile_request(
        self,
        target_key: str,
        lease_owner: str,
        outcome: Literal["done", "failed"],
        plan: dict[str, object],
        error: str = "",
    ) -> ProductReconcileRequestRecord: ...

    def request_product_reconcile(
        self, target: ProductReconcileTarget, requested_at: str
    ) -> ProductReconcileRequestRecord: ...

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]: ...

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...


TransportFactory = Callable[[object, LaunchplaneProductProfileRecord], BuildProvenanceTransport]


@dataclass(frozen=True)
class PreviewProviderHooks:
    """The provider-facing steps of a preview apply; tests replace them with fakes."""

    build_inputs: Callable[..., dict[str, object]] = build_odoo_preview_apply_inputs_result
    execute_apply: ExecuteOdooPreviewApply = execute_odoo_preview_apply_result
    observe_apply: ObserveOdooPreviewApply = observe_odoo_preview_apply_result


@dataclass(frozen=True)
class ReconcileOutcome:
    """What a reconcile decided and did.

    ``error`` records the request as failed with this plan; ``deferred`` returns it
    to pending so it runs again (a busy lane or a PR that moved).
    """

    plan: dict[str, object]
    error: str = ""
    deferred: bool = False


@dataclass(frozen=True)
class _PreviewDecision:
    plan: dict[str, object]
    verified: VerifiedBuildArtifact | None = None
    lifecycle_token: str = "none"
    observed: dict[str, object] = field(default_factory=dict)


def resolve_build_provenance_transport(
    record_store: object, profile: LaunchplaneProductProfileRecord
) -> BuildProvenanceTransport:
    """Mint the product's read-only build-provenance token from its merge-train App."""
    try:
        policy_record = resolve_merge_train_policy_record(record_store)
    except MergeTrainPolicyStoreMissingError as error:
        raise ProductReconcileError(f"No build-provenance token: {error}.") from error
    try:
        repository_policy = policy_record.policy.find_repository_policy(
            repository=profile.repository, base_branch=profile.default_branch
        )
    except ValueError as error:
        raise ProductReconcileError(f"No build-provenance token: {error}.") from error
    app = repository_policy.github_token.github_app
    if app is None:
        raise ProductReconcileError(
            f"No build-provenance token: the merge train policy for {profile.repository} "
            "has no GitHub App."
        )
    identity = product_repository_identity(record_store, profile)
    if str(app.repository_id) != identity.repository_id:
        raise ProductReconcileError(
            "No build-provenance token: the merge train App's repository id is not the "
            "product's repository id in Launchplane's repository inventory."
        )
    try:
        private_key = secrets.resolve_context_secret_value(
            integration=MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION,
            context_name=app.private_key_context,
            binding_key="private_key",
        )
    except (click.ClickException, SQLAlchemyError, OSError, TypeError, ValueError) as error:
        raise ProductReconcileError(
            f"No build-provenance token: the merge train App key could not be read "
            f"({type(error).__name__})."
        ) from error
    if not private_key:
        raise ProductReconcileError(
            "No build-provenance token: the merge train App key is not recorded."
        )
    try:
        token = mint_build_provenance_installation_token(
            identity=GitHubAppIdentity(app_id=app.app_id, private_key=private_key),
            repository=profile.repository,
            repository_id=identity.repository_id,
        )
    except (GitHubAppIdentityError, click.ClickException, OSError, ValueError) as error:
        raise ProductReconcileError(
            f"No build-provenance token: minting failed ({type(error).__name__}: {error})."
        ) from error
    return GitHubBuildProvenanceTransport(token=token.token)


def product_repository_identity(
    record_store: object, profile: LaunchplaneProductProfileRecord
) -> ProductRepositoryIdentity:
    """The product's immutable repository identity from Launchplane's repository inventory."""
    try:
        return resolve_product_repository_identity(record_store, profile)
    except ProductRepositoryIdentityRefusal as error:
        raise ProductReconcileError(
            f"Product {profile.product} has no usable repository identity in Launchplane's "
            f"repository inventory ({error.code}): {error}"
        ) from error


def reconcile_testing_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    repository_id: str,
    transport: BuildProvenanceTransport,
) -> ReconcileOutcome:
    """Queue the testing lane's deploy of its desired artifact when it runs anything else."""
    plan, desired = _plan_testing_target(
        record_store=record_store,
        profile=profile,
        repository_id=repository_id,
        transport=transport,
    )
    if desired is None or plan["action"] != "deploy":
        return ReconcileOutcome(plan)
    try:
        lane = resolve_odoo_target_replacement_apply_lane(
            record_store=record_store, product=profile.product, instance=TESTING_INSTANCE
        )
    except (
        OdooTargetReplacementApplyProductMismatchError,
        OdooTargetReplacementApplyRouteDependencyError,
    ):
        # Only the Odoo testing deploy runs on Launchplane's reconcile grant today.
        plan.update(held=True, reason="no_reconcile_deploy_for_driver")
        return ReconcileOutcome(plan)
    hold = read_staff_testing_hold(record_store=record_store, context=lane.context)
    if hold is not None:
        # Site staff are testing: nothing is recorded or queued until the hold is lifted.
        plan.update(
            action="wait",
            held=True,
            reason=STAFF_TESTING_HOLD_REASON,
            hold_reason=hold.reason,
            hold_recorded_by=hold.recorded_by,
            hold_recorded_at=hold.recorded_at,
        )
        return ReconcileOutcome(plan)
    manifest = record_verified_build_artifact(
        record_store=cast(VerifiedArtifactStore, record_store), verified=desired
    )
    # The request the product's testing-deploy workflow sent before Launchplane took over.
    envelope = OdooTargetReplacementApplyEnvelope(
        product=profile.product,
        replacement=OdooStableTargetReplacementApplyRequest(
            product=profile.product,
            instance=TESTING_INSTANCE,
            strategy="recreate-in-place",
            allow_empty_data=True,
            data_source_mode="existing",
            artifact_id=manifest.artifact_id,
            source_git_ref=manifest.source_commit,
        ),
    )
    idempotency_scope = reconcile_reservation_scope(profile.product)

    def testing_runs_desired() -> bool:
        # Re-read now: the worker may have published this release since the plan read it.
        current_artifact_id, current_digest = _current_testing_release(
            record_store=record_store, profile=profile, lane=lane
        )
        plan.update(current_artifact_id=current_artifact_id, current_image_digest=current_digest)
        return (
            manifest.artifact_id == current_artifact_id
            or manifest.image.digest.lower() == current_digest
        )

    attempt = _next_testing_attempt(
        record_store=record_store,
        testing_runs_desired=testing_runs_desired,
        base_key=(
            f"{RECONCILE_SOURCE}:{profile.product}:{lane.context}:{TESTING_INSTANCE}:"
            f"{manifest.artifact_id}"
        ),
        idempotency_scope=idempotency_scope,
    )
    if attempt.last_failed_operation_id:
        plan["last_failed_operation_id"] = attempt.last_failed_operation_id
    if attempt.deployed_operation_id:
        plan.update(
            action="none",
            reason="already_deployed",
            held=False,
            deployed_operation_id=attempt.deployed_operation_id,
        )
        return ReconcileOutcome(plan)
    if attempt.active is not None:
        # Includes an operation awaiting provider reconciliation: it is never bypassed.
        plan.update(
            held=False,
            queued_operation_id=attempt.active.operation_id,
            queued_operation_status=attempt.active.status,
        )
        return ReconcileOutcome(plan)
    if attempt.failed_attempts >= TESTING_DEPLOY_MAX_FAILED_ATTEMPTS:
        plan["held"] = False
        return ReconcileOutcome(
            plan,
            error=(
                f"The testing deploy of {manifest.artifact_id} failed "
                f"{attempt.failed_attempts} times (last {attempt.last_failed_operation_id}); "
                "Launchplane stops retrying it until a newer build."
            ),
        )
    created_at = _utc_now()
    idempotency_key = attempt.idempotency_key
    try:
        _records, operation = enqueue_odoo_target_replacement_apply_operation(
            record_store=record_store,
            request=envelope,
            context=lane.context,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
            request_fingerprint=_fingerprint(envelope.model_dump(mode="json")),
            created_at=created_at,
            authorization=build_launchplane_reconcile_authorization(
                product=profile.product, context=lane.context, authorized_at=created_at
            ),
        )
    except OdooTargetReplacementApplyOperationActiveError as error:
        plan.update(
            held=False, deferred="lane_busy", active_operation_id=error.operation.operation_id
        )
        return ReconcileOutcome(plan, deferred=True)
    except OdooTargetReplacementApplyLaneBusyError:
        plan.update(held=False, deferred="lane_busy")
        return ReconcileOutcome(plan, deferred=True)
    except (ValueError, click.ClickException) as error:
        raise ProductReconcileError(f"The testing deploy was not queued: {error}") from error
    plan.update(
        held=False,
        queued_operation_id=str(operation.get("operation_id") or ""),
        queued_operation_status=str(operation.get("status") or ""),
    )
    return ReconcileOutcome(plan)


@dataclass(frozen=True)
class _TestingAttempt:
    idempotency_key: str
    active: OdooStableTargetReplacementOperationRecord | None = None
    failed_attempts: int = 0
    last_failed_operation_id: str = ""
    deployed_operation_id: str = ""


def _next_testing_attempt(
    *,
    record_store: object,
    testing_runs_desired: Callable[[], bool],
    base_key: str,
    idempotency_scope: str,
) -> _TestingAttempt:
    """Follow this artifact's earlier attempts to the active one or the next key.

    Each retry's key names the terminal attempt before it, so a crashed or
    repeated reconcile finds the same attempt instead of queueing another.
    """
    operation_store = odoo_target_replacement_apply_operation_store(record_store)
    key = base_key
    failed_attempts = 0
    last_failed_operation_id = ""
    for _ in range(TESTING_DEPLOY_MAX_ATTEMPT_CHAIN):
        existing = find_odoo_target_replacement_apply_operation_by_idempotency_key(
            operation_store=operation_store,
            idempotency_key=key,
            idempotency_scope=idempotency_scope,
        )
        if existing is None:
            return _TestingAttempt(
                idempotency_key=key,
                failed_attempts=failed_attempts,
                last_failed_operation_id=last_failed_operation_id,
            )
        if existing.status not in ODOO_STABLE_TARGET_REPLACEMENT_TERMINAL_OPERATION_STATUSES:
            return _TestingAttempt(
                idempotency_key=key,
                active=existing,
                failed_attempts=failed_attempts,
                last_failed_operation_id=last_failed_operation_id,
            )
        if existing.status == "pass":
            if testing_runs_desired():
                # It finished after the plan read the release; nothing to redeploy.
                return _TestingAttempt(
                    idempotency_key=key, deployed_operation_id=existing.operation_id
                )
        elif not is_staff_testing_hold_cancellation(existing):
            failed_attempts += 1
            last_failed_operation_id = existing.operation_id
        # A passed attempt that testing no longer runs was rolled back: deploy again.
        key = f"{base_key}:after-{existing.operation_id}"
    raise ProductReconcileError(
        f"The testing deploy has more than {TESTING_DEPLOY_MAX_ATTEMPT_CHAIN} attempts."
    )


def _plan_testing_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    repository_id: str,
    transport: BuildProvenanceTransport,
) -> tuple[dict[str, object], VerifiedBuildArtifact | None]:
    """Desired: the newest first-parent default-branch commit with a verified release build."""
    lane = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
    if lane is None:
        raise ProductReconcileError(f"Product {profile.product} has no testing lane.")
    desired, rejected = _desired_release(
        transport=transport, profile=profile, repository_id=repository_id, lane=lane
    )
    current_artifact_id, current_digest = _current_testing_release(
        record_store=record_store, profile=profile, lane=lane
    )
    plan: dict[str, object] = {
        "target": "testing",
        "context": lane.context,
        "desired_artifact_id": "",
        "desired_commit": "",
        "desired_image_digest": "",
        "current_artifact_id": current_artifact_id,
        "current_image_digest": current_digest,
    }
    if rejected:
        plan["rejected_builds"] = rejected
    if desired is None:
        plan.update(action="none", reason="no_verified_build", held=False)
        return plan, None
    manifest = desired.manifest
    desired_digest = manifest.image.digest.lower()
    plan.update(
        desired_artifact_id=manifest.artifact_id,
        desired_commit=manifest.source_commit,
        desired_image_digest=desired_digest,
    )
    if manifest.artifact_id == current_artifact_id or desired_digest == current_digest:
        plan.update(action="none", reason="already_deployed", held=False)
    else:
        plan.update(action="deploy")
    return plan, desired


def reconcile_preview_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    repository_id: str,
    transport: BuildProvenanceTransport,
    pull_request_number: int,
    control_plane_root: Path | None,
    preview_hooks: PreviewProviderHooks,
) -> ReconcileOutcome:
    """Apply or destroy the PR's preview so it matches what the PR asks for now."""
    decision = _plan_preview_target(
        record_store=record_store,
        profile=profile,
        repository_id=repository_id,
        transport=transport,
        pull_request_number=pull_request_number,
    )
    plan = decision.plan
    if plan["action"] not in {"apply", "destroy"}:
        return ReconcileOutcome(plan)
    if not product_profile_uses_odoo_driver(profile):
        plan.update(held=True, reason="no_reconcile_preview_for_driver")
        return ReconcileOutcome(plan)
    if control_plane_root is None:
        raise ProductReconcileError("A preview reconcile needs the control-plane root.")
    return _run_preview_operation(
        record_store=record_store,
        profile=profile,
        transport=transport,
        decision=decision,
        pull_request_number=pull_request_number,
        control_plane_root=control_plane_root,
        preview_hooks=preview_hooks,
    )


def _plan_preview_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    repository_id: str,
    transport: BuildProvenanceTransport,
    pull_request_number: int,
) -> _PreviewDecision:
    """Desired: a preview of the PR head's verified build while open and labeled."""
    preview_context = profile.preview.context.strip()
    plan: dict[str, object] = {
        "target": "preview",
        "pull_request_number": pull_request_number,
        "context": preview_context,
    }
    current, lifecycle_token = _current_preview(
        record_store=record_store,
        profile=profile,
        preview_context=preview_context,
        pull_request_number=pull_request_number,
    )
    plan.update(current)
    live = bool(current["current_preview_id"])
    observed: dict[str, object] = {}

    def without_preview(reason: str) -> _PreviewDecision:
        plan["reason"] = reason
        plan.update(action="destroy" if live else "none", held=False)
        return _PreviewDecision(plan=plan, lifecycle_token=lifecycle_token, observed=observed)

    if not profile.preview.enabled or not preview_context:
        return without_preview("preview_not_configured")
    pull_request = _object(
        transport.get_json(f"/repos/{_repository_path(profile)}/pulls/{pull_request_number}"),
        "pull request",
    )
    head_sha = str(_object(pull_request.get("head"), "pull request head").get("sha") or "")
    if not head_sha:
        raise ProductReconcileError(f"GitHub returned no head for PR {pull_request_number}.")
    plan["head_sha"] = head_sha.lower()
    observed.update(
        head_sha=head_sha.lower(),
        eligible=_preview_eligible(pull_request, profile.preview.enable_label),
    )
    if pull_request.get("state") != "open":
        return without_preview("pull_request_not_open")
    if profile.preview.enable_label not in _labels(pull_request):
        return without_preview("preview_label_missing")
    try:
        verified = verify_build_artifact(
            transport=transport,
            repository=profile.repository,
            repository_id=repository_id,
            commit=head_sha,
            purpose="preview",
            context=preview_context,
            image_repository=profile.image.repository,
            pull_request_number=pull_request_number,
        )
    except BuildProvenanceError as error:
        # A PR's build is untrusted input: an unprovable or malformed one is not built yet.
        plan.update(action="wait", reason="no_verified_build", detail=str(error), held=False)
        return _PreviewDecision(plan=plan, lifecycle_token=lifecycle_token, observed=observed)
    manifest = verified.manifest
    desired_digest = manifest.image.digest.lower()
    plan.update(desired_commit=manifest.source_commit, desired_image_digest=desired_digest)
    if live and desired_digest == current["current_image_digest"]:
        plan.update(action="none", reason="already_serving", held=False)
    else:
        plan.update(action="apply", held=False)
    return _PreviewDecision(
        plan=plan, verified=verified, lifecycle_token=lifecycle_token, observed=observed
    )


class _PullRequestMovedError(Exception):
    """The PR changed after the plan; nothing reached the provider."""


def _run_preview_operation(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    transport: BuildProvenanceTransport,
    decision: _PreviewDecision,
    pull_request_number: int,
    control_plane_root: Path,
    preview_hooks: PreviewProviderHooks,
) -> ReconcileOutcome:
    """Issue the preview plan the inputs route would, then run it as the apply route does."""
    plan = decision.plan
    operation: Literal["refresh", "destroy"] = "refresh" if plan["action"] == "apply" else "destroy"
    manifest = decision.verified.manifest if decision.verified is not None else None
    if operation == "refresh":
        if manifest is None or manifest.source_build is None:
            raise ProductReconcileError("A preview apply needs its verified build.")
        build_token = f"run-{manifest.source_build.run_id}-{manifest.source_build.run_attempt}"
    else:
        build_token = "destroy"
    # The same build against the same preview state is the same operation, so a
    # repeated or crashed reconcile replays it instead of running it twice.
    operation_key = (
        f"{RECONCILE_SOURCE}:{profile.product}:pr-{pull_request_number}:{build_token}:"
        f"{hashlib.sha256(decision.lifecycle_token.encode()).hexdigest()[:16]}"
    )
    reservation_scope = reconcile_reservation_scope(profile.product)
    plan_id = build_odoo_preview_plan_id(scope=reservation_scope, idempotency_key=operation_key)
    plan.update(preview_operation_key=operation_key, preview_plan_id=plan_id)
    database_url = getattr(record_store, "database_url", None)

    def pre_mutation_guard() -> None:
        # Holding the reservation, just before the provider apply: a PR that closed,
        # lost its label, or moved its head since the plan releases with no effect.
        if decision.observed and _pull_request_moved(
            transport=transport,
            profile=profile,
            pull_request_number=pull_request_number,
            observed=decision.observed,
        ):
            raise _PullRequestMovedError

    try:
        inputs = preview_hooks.build_inputs(
            control_plane_root=control_plane_root,
            record_store=record_store,
            profile=profile,
            request=OdooPreviewApplyInputsRequest(
                product=profile.product,
                operation=operation,
                pr_number=pull_request_number,
                manifest=manifest if operation == "refresh" else None,
                source_git_ref=manifest.source_commit if manifest is not None else "",
                source=RECONCILE_SOURCE,
            ),
            database_url=database_url,
        )
        issued_plan = issue_odoo_preview_apply_plan(result=inputs, plan_id=plan_id)
        if issued_plan.status != "ready":
            plan["preview_result_status"] = "blocked"
            return ReconcileOutcome(
                plan,
                error=f"The preview {operation} plan is blocked: {issued_plan.error_message}",
            )
        apply_request = validate_odoo_preview_issued_plan(
            plan_id=plan_id,
            issued_plan=issued_plan,
            request=OdooPreviewApplyEnvelope(
                product=profile.product,
                apply=OdooPreviewDokployApplyRequest(
                    dry_run_plan=issued_plan.dry_run_plan,
                    manifest=issued_plan.plan_request.manifest,
                    image_reference=issued_plan.plan_request.image_reference,
                    timeout_seconds=PREVIEW_APPLY_TIMEOUT_SECONDS,
                ),
            ),
        )
        validate_odoo_preview_profile_authority(profile=profile, issued_plan=issued_plan)
        if not launchplane_reconcile_preview_destination_allowed(
            record_store=record_store,
            product=profile.product,
            context=issued_plan.context,
            preview_slug=issued_plan.preview_slug,
        ):
            raise ProductReconcileError(
                "Launchplane's reconcile may not change this preview destination."
            )
        plan.update(preview_slug=issued_plan.preview_slug, preview_url=issued_plan.preview_url)
        result = run_odoo_preview_apply_operation(
            store=cast(DurableProviderOperationStore, record_store),
            control_plane_root=control_plane_root,
            record_store=record_store,
            profile=profile,
            apply_request=apply_request,
            issued_plan=issued_plan,
            reservation_scope=reservation_scope,
            idempotency_key=plan_id,
            request_fingerprint=_fingerprint({"operation_key": operation_key}),
            trace_id=f"{RECONCILE_SOURCE}-{uuid4().hex}",
            execute_apply=preview_hooks.execute_apply,
            observe_apply=preview_hooks.observe_apply,
            pre_mutation_guard=pre_mutation_guard,
        )
        plan["preview_operation_status"] = result.status
        if result.status in {"in_progress", "target_busy"}:
            plan["deferred"] = "preview_operation_busy"
            return ReconcileOutcome(plan, deferred=True)
        driver_result = result.response_payload.get("result")
        driver_result = driver_result if isinstance(driver_result, dict) else {}
        result_status = str(driver_result.get("status") or "")
        plan["preview_result_status"] = result_status
        if result.status in {"conflict", "reconcile_required"} or result_status != "pass":
            message = str(driver_result.get("error_message") or "").strip()
            return ReconcileOutcome(
                plan,
                error=message or f"The preview {operation} ended {result.status}/{result_status}.",
            )
        records = result.response_payload.get("records")
        validate_odoo_preview_lifecycle_response_current(
            record_store=record_store,
            profile=profile,
            issued_plan=issued_plan,
            records=records if isinstance(records, dict) else {},
        )
    except _PullRequestMovedError:
        plan["deferred"] = "pull_request_moved"
        return ReconcileOutcome(plan, deferred=True)
    except (OdooPreviewPlanProvenanceError, OdooPreviewApplyConfigError) as error:
        return ReconcileOutcome(plan, error=f"The preview {operation} was refused: {error}")
    except (FileNotFoundError, ValueError, click.ClickException) as error:
        raise ProductReconcileError(f"The preview {operation} failed: {error}") from error
    return ReconcileOutcome(plan)


def reconcile_product_request(
    *,
    record_store: ProductReconcileStore,
    request: ProductReconcileRequestRecord,
    transport_factory: TransportFactory = resolve_build_provenance_transport,
    control_plane_root: Path | None = None,
    preview_hooks: PreviewProviderHooks = PreviewProviderHooks(),
) -> ReconcileOutcome:
    try:
        profile = record_store.read_product_profile_record(request.product)
    except FileNotFoundError as error:
        raise ProductReconcileError(f"Product profile {request.product} is missing.") from error
    if not profile.is_active:
        return ReconcileOutcome(
            {"target": request.target_kind, "action": "none", "reason": "product_inactive"}
        )
    identity = product_repository_identity(record_store, profile)
    transport = transport_factory(record_store, profile)
    if request.target_kind == "testing":
        return reconcile_testing_target(
            record_store=record_store,
            profile=profile,
            repository_id=identity.repository_id,
            transport=transport,
        )
    assert request.pull_request_number is not None
    return reconcile_preview_target(
        record_store=record_store,
        profile=profile,
        repository_id=identity.repository_id,
        transport=transport,
        pull_request_number=request.pull_request_number,
        control_plane_root=control_plane_root,
        preview_hooks=preview_hooks,
    )


def run_product_reconcile_once(
    *,
    record_store: ProductReconcileStore,
    lease_owner: str,
    lease_seconds: int = PRODUCT_RECONCILE_LEASE_SECONDS,
    transport_factory: TransportFactory = resolve_build_provenance_transport,
    control_plane_root: Path | None = None,
    preview_hooks: PreviewProviderHooks = PreviewProviderHooks(),
) -> ProductReconcileRequestRecord | None:
    """Claim one request, reconcile it, and record the plan; one bad target never stops the worker."""
    request = record_store.claim_next_product_reconcile_request(lease_owner, lease_seconds)
    if request is None:
        return None
    try:
        outcome = reconcile_product_request(
            record_store=record_store,
            request=request,
            transport_factory=transport_factory,
            control_plane_root=control_plane_root,
            preview_hooks=preview_hooks,
        )
    except Exception as error:
        _LOGGER.warning("Product reconcile of %s failed: %s", request.target_key, error)
        return record_store.complete_product_reconcile_request(
            request.target_key,
            lease_owner,
            "failed",
            {"target": request.target_kind},
            _error_text(error),
        )
    if outcome.deferred:
        # Folding a request into our running one returns it to pending on completion.
        record_store.request_product_reconcile(
            ProductReconcileTarget(
                product=request.product,
                target_kind=request.target_kind,
                pull_request_number=request.pull_request_number,
            ),
            _utc_now(),
        )
    if outcome.error:
        _LOGGER.warning("Product reconcile of %s failed: %s", request.target_key, outcome.error)
        return record_store.complete_product_reconcile_request(
            request.target_key, lease_owner, "failed", outcome.plan, outcome.error
        )
    return record_store.complete_product_reconcile_request(
        request.target_key, lease_owner, "done", outcome.plan
    )


def reconcile_reservation_scope(product: str) -> str:
    """The mutation scope of Launchplane's own reconcile work for one product."""
    return f"{RECONCILE_SOURCE}:{product.strip()}"


def request_product_reconcile_sweep(
    record_store: ProductReconcileStore, now: str
) -> tuple[str, ...]:
    """Request every mapped product's testing target and every live preview; no GitHub reads."""
    targets: list[ProductReconcileTarget] = []
    inventory_records = record_store.list_repository_inventory_records()
    for profile in record_store.list_product_profile_records():
        if not profile.is_active or not _has_repository_identity(profile, inventory_records):
            continue
        if not any(lane.instance == "testing" for lane in profile.lanes):
            continue
        targets.append(ProductReconcileTarget(product=profile.product, target_kind="testing"))
        preview_context = profile.preview.context.strip()
        if not preview_context:
            continue
        for preview in record_store.list_preview_records(
            context_name=preview_context, anchor_repo=_preview_anchor_repo(profile)
        ):
            if preview.state not in _ENDED_PREVIEW_STATES:
                targets.append(
                    ProductReconcileTarget(
                        product=profile.product,
                        target_kind="preview",
                        pull_request_number=preview.anchor_pr_number,
                    )
                )
    unique_targets = {target.target_key: target for target in targets}
    for target in unique_targets.values():
        record_store.request_product_reconcile(target, now)
    return tuple(unique_targets)


def _has_repository_identity(
    profile: LaunchplaneProductProfileRecord,
    inventory_records: tuple[RepositoryInventoryRecord, ...],
) -> bool:
    try:
        product_repository_identity_from_inventory(
            profile=profile, inventory_records=inventory_records
        )
    except ProductRepositoryIdentityRefusal:
        return False
    return True


def _desired_release(
    *,
    transport: BuildProvenanceTransport,
    profile: LaunchplaneProductProfileRecord,
    repository_id: str,
    lane: ProductLaneProfile,
) -> tuple[VerifiedBuildArtifact | None, list[dict[str, str]]]:
    default_branch = profile.default_branch
    workflow_file = BUILD_WORKFLOW_PATH.rsplit("/", 1)[-1]
    query = urlencode(
        {
            "branch": default_branch,
            "event": "push",
            "status": "success",
            "per_page": str(TESTING_BUILD_RUN_PAGE_SIZE),
        }
    )
    payload = _object(
        transport.get_json(
            f"/repos/{_repository_path(profile)}/actions/workflows/{workflow_file}/runs?{query}"
        ),
        "workflow runs",
    )
    built_commits = {
        str(run.get("head_sha") or "").lower()
        for run in (item for item in _list(payload.get("workflow_runs")) if isinstance(item, dict))
        if run.get("path") == BUILD_WORKFLOW_PATH
        and run.get("event") == "push"
        and run.get("head_branch") == default_branch
        and run.get("conclusion") == "success"
        and run.get("head_sha")
    }
    ordered: list[str] = []
    if built_commits:
        for sha in first_parent_history(
            transport=transport, repository=profile.repository, default_branch=default_branch
        ):
            if sha in built_commits:
                ordered.append(sha)
                if len(ordered) == len(built_commits):
                    break
    rejected: list[dict[str, str]] = []
    for commit in ordered[:TESTING_VERIFY_LIMIT]:
        try:
            return (
                verify_build_artifact(
                    transport=transport,
                    repository=profile.repository,
                    repository_id=repository_id,
                    commit=commit,
                    purpose="release",
                    context=lane.context,
                    image_repository=profile.image.repository,
                ),
                rejected,
            )
        except BuildProvenanceError as error:
            rejected.append({"commit": commit, "error": str(error)})
    return None, rejected


def _current_testing_release(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> tuple[str, str]:
    if profile.driver_id == "odoo":
        try:
            release = record_store.read_release_tuple_record(
                context_name=lane.context, channel_name="testing"
            )
        except FileNotFoundError:
            return "", ""
        digest = release.image_digest or _artifact_digest(record_store, release.artifact_id)
        return release.artifact_id, digest.lower()
    try:
        inventory = record_store.read_environment_inventory(
            context_name=lane.context, instance_name="testing"
        )
    except FileNotFoundError:
        return "", ""
    identity = inventory.runtime_identity
    if identity is None:
        return "", ""
    digest = _image_reference_digest(identity.image_reference) or _artifact_digest(
        record_store, identity.artifact_id
    )
    return identity.artifact_id, digest.lower()


def _current_preview(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    preview_context: str,
    pull_request_number: int,
) -> tuple[dict[str, object], str]:
    """The live preview, and a token that changes with every lifecycle step of its record."""
    current: dict[str, object] = {
        "current_preview_id": "",
        "current_state": "",
        "current_head_sha": "",
        "current_image_digest": "",
    }
    if not preview_context:
        return current, "none"
    preview = find_preview_record(
        record_store=cast(PreviewMutationRecordStore, record_store),
        context_name=preview_context,
        anchor_repo=_preview_anchor_repo(profile),
        anchor_pr_number=pull_request_number,
    )
    if preview is None:
        return current, "none"
    lifecycle_token = f"{preview.preview_id}@{preview.state}@{preview.updated_at}"
    if preview.state in _ENDED_PREVIEW_STATES:
        return current, lifecycle_token
    current.update(current_preview_id=preview.preview_id, current_state=preview.state)
    generation_id = preview.serving_generation_id or preview.active_generation_id
    if not generation_id:
        return current, lifecycle_token
    try:
        generation = record_store.read_preview_generation_record(generation_id)
    except FileNotFoundError:
        return current, lifecycle_token
    digest = ""
    if generation.runtime_identity is not None:
        digest = _image_reference_digest(generation.runtime_identity.image_reference)
    if not digest and generation.artifact_id:
        digest = _artifact_digest(record_store, generation.artifact_id)
    current.update(
        current_head_sha=generation.anchor_summary.head_sha.lower(),
        current_image_digest=digest.lower(),
    )
    return current, lifecycle_token


def _pull_request_moved(
    *,
    transport: BuildProvenanceTransport,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    observed: dict[str, object],
) -> bool:
    """Read the PR again just before the provider change; a moved PR is reconciled again."""
    pull_request = _object(
        transport.get_json(f"/repos/{_repository_path(profile)}/pulls/{pull_request_number}"),
        "pull request",
    )
    head_sha = str(_object(pull_request.get("head"), "pull request head").get("sha") or "")
    return head_sha.lower() != observed.get("head_sha") or _preview_eligible(
        pull_request, profile.preview.enable_label
    ) != observed.get("eligible")


def _preview_eligible(pull_request: dict[str, object], enable_label: str) -> bool:
    return pull_request.get("state") == "open" and enable_label in _labels(pull_request)


def _labels(pull_request: dict[str, object]) -> set[str]:
    return {
        str(label.get("name") or "").strip()
        for label in (item for item in _list(pull_request.get("labels")) if isinstance(item, dict))
    }


def _fingerprint(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _artifact_digest(record_store: ProductReconcileStore, artifact_id: str) -> str:
    if not artifact_id:
        return ""
    try:
        return record_store.read_artifact_manifest(artifact_id).image.digest
    except FileNotFoundError:
        return ""


def _image_reference_digest(image_reference: str) -> str:
    _name, separator, digest = image_reference.strip().rpartition("@")
    return digest if separator and digest.startswith("sha256:") else ""


def _preview_anchor_repo(profile: LaunchplaneProductProfileRecord) -> str:
    return profile.repository.strip().partition("/")[2].strip()


def _repository_path(profile: LaunchplaneProductProfileRecord) -> str:
    return quote(profile.repository.strip(), safe="/")


def _error_text(error: Exception) -> str:
    if isinstance(error, (ProductReconcileError, BuildProvenanceError, FileNotFoundError)):
        return str(error) or type(error).__name__
    return f"Unexpected {type(error).__name__} while reconciling."


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProductReconcileError(f"GitHub returned an unexpected {label}.")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
