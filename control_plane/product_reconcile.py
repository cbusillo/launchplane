"""Make a product's testing lane and PR previews run its newest verified builds.

A reconcile reads GitHub's record of the product's builds and Launchplane's own
records, decides what should change, and does it under Launchplane's own
reconcile grant (``launchplane_reconcile_authorization``): it queues an Odoo
testing lane's stable target replacement, deploys a generic-web testing lane,
and applies or destroys the PR's Odoo or generic-web preview. It records what it decided and did as
the plan on its request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from pathlib import Path
from threading import Event, Thread
from typing import Literal, Protocol, cast
from urllib.error import HTTPError
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
    VerifiedGenericWebBuild,
    first_parent_history,
    record_verified_build_artifact,
    verify_build_artifact,
    verify_generic_web_build,
)
from control_plane.contracts.record_failures import record_failure_summary
from control_plane.contracts.promotion_record import env_key_names
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.odoo_target_replacement_failures import deploy_failure_description
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
from control_plane.generic_web_deploy_http import (
    GENERIC_WEB_DEPLOY_ROUTE,
    GenericWebDeployEnvelope,
    GenericWebDeployProductMismatchError,
    GenericWebDeployRouteDependencyError,
    resolve_generic_web_deploy_lane,
)
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.generic_web_preview_http import (
    GenericWebPreviewDestroyEnvelope,
    GenericWebPreviewRefreshEnvelope,
    apply_generic_web_preview_destroy_result,
    apply_generic_web_preview_refresh_result,
)
from control_plane.generic_web_verification_http import (
    GenericWebPreviewVerificationEnvelope,
    apply_generic_web_preview_verification_result,
)
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewVerificationRequest,
)
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppIdentityError,
    mint_build_provenance_installation_token,
    mint_pull_request_feedback_installation_token,
)
from control_plane.launchplane_reconcile_authorization import (
    TESTING_INSTANCE,
    build_launchplane_reconcile_authorization,
    launchplane_reconcile_generic_web_testing_allowed,
    launchplane_reconcile_preview_destination_allowed,
    reconciles_as_generic_web,
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
from control_plane.product_reconcile_feedback import (
    PR_FEEDBACK_PLAN_KEY,
    FeedbackTokenFactory,
    OwnerReviewStatusWriter,
    post_reconcile_feedback,
)
from control_plane.product_review import require_product_review_store
from control_plane.product_review_status import OwnerReviewStatus, OwnerReviewStatusPublisher
from control_plane.product_repository_identity import (
    ProductRepositoryIdentity,
    ProductRepositoryIdentityRefusal,
    product_repository_identity_from_inventory,
    resolve_product_repository_identity,
)
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    DurableProviderOperationStore,
    run_durable_provider_operation,
)
from control_plane.service_human_auth import launchplane_public_origin_from_env
from control_plane.testing_lane_hold import (
    STAFF_TESTING_HOLD_REASON,
    is_staff_testing_hold_cancellation,
    read_staff_testing_hold,
)
from control_plane.workflows.generic_web_deploy import GenericWebDeployRequest
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    default_generic_web_deploy_provider,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewRefreshRequest,
    resolve_generic_web_preview_slug,
)
from control_plane.workflows.launchplane import PreviewMutationRecordStore, find_preview_record
from control_plane.workflows.odoo_preview_runtime import (
    OdooPreviewApplyInputsRequest,
    OdooPreviewDokployApplyRequest,
)

PRODUCT_RECONCILE_SWEEP_SECONDS = 30 * 60
# A running reconcile renews its lease every third of it, so a long preview apply
# keeps it; a crashed worker's request is reclaimed after this, well inside one sweep.
PRODUCT_RECONCILE_LEASE_SECONDS = 20 * 60
RECONCILE_SOURCE = "launchplane-reconcile"
TESTING_DEPLOY_MAX_FAILED_ATTEMPTS = 3
PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS = 3
TESTING_DEPLOY_MAX_ATTEMPT_CHAIN = 20
PREVIEW_APPLY_TIMEOUT_SECONDS = 600
# A generic-web refresh waits for the deploy and then for health, each up to this
# long, inside the request's lease.
GENERIC_WEB_PREVIEW_TIMEOUT_SECONDS = 300
TESTING_BUILD_RUN_PAGE_SIZE = 50
TESTING_VERIFY_LIMIT = 3
_ENDED_PREVIEW_STATES = frozenset({"destroyed", "teardown_pending"})
OPEN_PULL_REQUEST_SWEEP_PAGES = 5
_LEASE_LOST = "This worker no longer holds the reconcile request's lease; nothing more was changed."
_LOGGER = logging.getLogger(__name__)


class ProductReconcileError(Exception):
    """The reconcile cannot decide; preview records use the optional fixed reason code."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


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

    def renew_product_reconcile_lease(
        self, target_key: str, lease_owner: str, lease_seconds: int
    ) -> bool: ...

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
GenericWebPreviewChange = Callable[..., tuple[dict[str, object], dict[str, object]]]


@dataclass(frozen=True)
class PreviewProviderHooks:
    """The provider-facing steps of a preview apply; tests replace them with fakes."""

    build_inputs: Callable[..., dict[str, object]] = build_odoo_preview_apply_inputs_result
    execute_apply: ExecuteOdooPreviewApply = execute_odoo_preview_apply_result
    observe_apply: ObserveOdooPreviewApply = observe_odoo_preview_apply_result
    refresh_generic_web: GenericWebPreviewChange = apply_generic_web_preview_refresh_result
    destroy_generic_web: GenericWebPreviewChange = apply_generic_web_preview_destroy_result


@dataclass(frozen=True)
class TestingProviderHooks:
    """The provider a generic-web testing deploy runs through; tests replace it with a fake."""

    generic_web_deploy_provider: Callable[[], GenericWebDeployProvider] = (
        default_generic_web_deploy_provider
    )


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
    verified: VerifiedBuildArtifact | VerifiedGenericWebBuild | None = None
    lifecycle_token: str = "none"
    observed: dict[str, object] = field(default_factory=dict)


def resolve_build_provenance_transport(
    record_store: object, profile: LaunchplaneProductProfileRecord
) -> BuildProvenanceTransport:
    """Mint the product's read-only build-provenance token from its merge-train App."""
    identity, repository_id = _merge_train_app_identity(
        record_store, profile, purpose="build-provenance"
    )
    try:
        token = mint_build_provenance_installation_token(
            identity=identity, repository=profile.repository, repository_id=repository_id
        )
    except (GitHubAppIdentityError, click.ClickException, OSError, ValueError) as error:
        raise ProductReconcileError(
            f"No build-provenance token: minting failed ({type(error).__name__}: {error})."
        ) from error
    return GitHubBuildProvenanceTransport(token=token.token)


def resolve_pull_request_feedback_token(
    record_store: object, profile: LaunchplaneProductProfileRecord
) -> str:
    """Mint the token the reconciler comments with: the merge-train App, and no other."""
    identity, repository_id = _merge_train_app_identity(
        record_store, profile, purpose="pull request feedback"
    )
    try:
        token = mint_pull_request_feedback_installation_token(
            identity=identity, repository=profile.repository, repository_id=repository_id
        )
    except (GitHubAppIdentityError, click.ClickException, OSError, ValueError) as error:
        raise ProductReconcileError(
            f"No pull request feedback token: minting failed ({type(error).__name__}: {error})."
        ) from error
    return token.token


def _merge_train_app_identity(
    record_store: object, profile: LaunchplaneProductProfileRecord, *, purpose: str
) -> tuple[GitHubAppIdentity, str]:
    """The product repository's merge-train App and its inventory repository id."""
    try:
        policy_record = resolve_merge_train_policy_record(record_store)
    except MergeTrainPolicyStoreMissingError as error:
        raise ProductReconcileError(f"No {purpose} token: {error}.") from error
    try:
        repository_policy = policy_record.policy.find_repository_policy(
            repository=profile.repository, base_branch=profile.default_branch
        )
    except ValueError as error:
        raise ProductReconcileError(f"No {purpose} token: {error}.") from error
    app = repository_policy.github_token.github_app
    if app is None:
        raise ProductReconcileError(
            f"No {purpose} token: the merge train policy for {profile.repository} "
            "has no GitHub App.",
            code="preview_credentials_unavailable",
        )
    identity = product_repository_identity(record_store, profile)
    if str(app.repository_id) != identity.repository_id:
        raise ProductReconcileError(
            f"No {purpose} token: the merge train App's repository id is not the "
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
            f"No {purpose} token: the merge train App key could not be read "
            f"({type(error).__name__}).",
            code="preview_credentials_unavailable",
        ) from error
    if not private_key:
        raise ProductReconcileError(
            f"No {purpose} token: the merge train App key is not recorded.",
            code="preview_credentials_unavailable",
        )
    return (
        GitHubAppIdentity(app_id=app.app_id, private_key=private_key),
        identity.repository_id,
    )


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
    control_plane_root: Path | None = None,
    testing_hooks: TestingProviderHooks = TestingProviderHooks(),
) -> ReconcileOutcome:
    """Deploy the testing lane's desired artifact when it runs anything else."""
    plan, desired = _plan_testing_target(
        record_store=record_store,
        profile=profile,
        repository_id=repository_id,
        transport=transport,
    )
    if desired is None or plan["action"] != "deploy":
        return ReconcileOutcome(plan)
    if isinstance(desired, VerifiedGenericWebBuild):
        return _deploy_generic_web_testing(
            record_store=record_store,
            profile=profile,
            plan=plan,
            desired=desired,
            control_plane_root=control_plane_root,
            testing_hooks=testing_hooks,
        )
    try:
        lane = resolve_odoo_target_replacement_apply_lane(
            record_store=record_store, product=profile.product, instance=TESTING_INSTANCE
        )
    except (
        OdooTargetReplacementApplyProductMismatchError,
        OdooTargetReplacementApplyRouteDependencyError,
    ):
        # Only Odoo and generic-web testing deploys run on Launchplane's reconcile grant.
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
    if attempt.last_failed is not None:
        # The operation's own status read needs the grant that starts a deploy;
        # this plan is what the product read shows, so the reason is copied here.
        error_code, error_summary = _testing_failure_reason(attempt.last_failed)
        plan.update(
            last_failed_operation_id=attempt.last_failed.operation_id,
            last_failed_error_code=error_code,
            last_failed_error_summary=error_summary,
        )
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
                f"{attempt.failed_attempts} times; Launchplane stops retrying it until a "
                f"newer build. Last attempt {attempt.last_failed_operation_id}: "
                f"{plan['last_failed_error_code']}: {plan['last_failed_error_summary']}"
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


def _deploy_generic_web_testing(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    plan: dict[str, object],
    desired: VerifiedGenericWebBuild,
    control_plane_root: Path | None,
    testing_hooks: TestingProviderHooks,
) -> ReconcileOutcome:
    """Deploy the verified image to the generic-web testing lane, in-process.

    It runs the deploy route's durable provider operation under the reconcile's
    reservation scope. A deploy whose provider outcome is unknown stays reserved
    for generic-web deploy recovery and is never bypassed; a recorded result for
    the same image and starting point is replayed, not run again.
    """
    if control_plane_root is None:
        raise ProductReconcileError("A generic-web testing deploy needs the control-plane root.")
    try:
        _profile, lane = resolve_generic_web_deploy_lane(
            record_store=record_store, product=profile.product, instance=TESTING_INSTANCE
        )
    except (GenericWebDeployProductMismatchError, GenericWebDeployRouteDependencyError) as error:
        raise ProductReconcileError(
            f"Product {profile.product} has no generic-web testing lane."
        ) from error
    if not launchplane_reconcile_generic_web_testing_allowed(
        record_store=record_store,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
    ):
        raise ProductReconcileError("Launchplane's reconcile may not deploy this lane.")
    hold = read_staff_testing_hold(record_store=record_store, context=lane.context)
    if hold is not None:
        plan.update(
            action="wait",
            held=True,
            reason=STAFF_TESTING_HOLD_REASON,
            hold_reason=hold.reason,
            hold_recorded_by=hold.recorded_by,
            hold_recorded_at=hold.recorded_at,
        )
        return ReconcileOutcome(plan)
    envelope = GenericWebDeployEnvelope(
        product=profile.product,
        deploy=GenericWebDeployRequest(
            product=profile.product,
            instance=TESTING_INSTANCE,
            artifact_id=desired.image_reference,
            source_git_ref=desired.manifest.source_commit,
        ),
    )
    # The deployment testing ran when this deploy was decided is part of its key:
    # every deploy and rollback records a new one, so a lane changed since gets the
    # desired image again instead of a replay of an earlier success.
    starting_point = _current_testing_deployment_id(record_store=record_store, lane=lane) or "none"
    idempotency_key = (
        f"{RECONCILE_SOURCE}:{profile.product}:{lane.context}:{TESTING_INSTANCE}:"
        f"{desired.manifest.image.digest}:from-{starting_point}"
    )
    trace_id = f"{RECONCILE_SOURCE}-{uuid4().hex}"
    plan.update(held=False, deploy_idempotency_key=idempotency_key)
    try:
        result = run_durable_provider_operation(
            store=cast(DurableProviderOperationStore, record_store),
            scope=reconcile_reservation_scope(profile.product),
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key=idempotency_key,
            request_fingerprint=_fingerprint(envelope.model_dump(mode="json")),
            lease_owner=trace_id,
            response_trace_id=trace_id,
            adapter=GenericWebDeployProviderMutationAdapter(
                control_plane_root=control_plane_root,
                record_store=record_store,
                deploy_request=envelope,
                profile=profile,
                lane=lane,
                trace_id=trace_id,
                deploy_provider=testing_hooks.generic_web_deploy_provider(),
            ),
        )
    except (FileNotFoundError, ValueError, click.ClickException) as error:
        # Refused before any provider change; the next event or sweep tries again. The
        # message can name provider targets, so it goes to the worker log only.
        _LOGGER.warning("Generic-web testing deploy of %s refused: %s", profile.product, error)
        plan["deploy_operation_status"] = "refused"
        return ReconcileOutcome(
            plan,
            error=(
                "The testing deploy was refused before any provider change "
                f"({type(error).__name__}); the worker log has the reason."
            ),
        )
    outcome = _generic_web_testing_outcome(plan=plan, result=result)
    if outcome.error or outcome.deferred:
        return outcome
    current_artifact_id, current_digest = _current_testing_release(
        record_store=record_store, profile=profile, lane=lane
    )
    if desired.manifest.image.digest.lower() != current_digest:
        # A recorded success Launchplane's testing record does not show (a recovery that
        # closed out from runtime evidence records no deployment): promotion reads that
        # record, so the image is never announced as running.
        return ReconcileOutcome(
            plan,
            error=(
                "The testing deploy is recorded as passed, but no deployment of its image is "
                "recorded for the testing lane. The next verified build, or an admin deploy "
                "of this image, records one."
            ),
        )
    plan.update(current_artifact_id=current_artifact_id, current_image_digest=current_digest)
    return outcome


def _current_testing_deployment_id(
    *, record_store: ProductReconcileStore, lane: ProductLaneProfile
) -> str:
    try:
        inventory = record_store.read_environment_inventory(
            context_name=lane.context, instance_name=TESTING_INSTANCE
        )
    except FileNotFoundError:
        return ""
    return inventory.deployment_record_id


def _generic_web_testing_outcome(
    *, plan: dict[str, object], result: DurableProviderOperationResult
) -> ReconcileOutcome:
    plan["deploy_operation_status"] = result.status
    if result.status == "in_progress" or (
        result.status == "target_busy"
        and (result.record is None or result.record.state != "reconcile_required")
    ):
        plan["deferred"] = "lane_busy"
        return ReconcileOutcome(plan, deferred=True)
    if result.status in {"target_busy", "reconcile_required"}:
        return ReconcileOutcome(
            plan,
            error=(
                "A testing deploy's provider outcome is unknown; Launchplane deploys this "
                "lane again after generic-web deploy recovery settles it."
            ),
        )
    if result.status == "conflict":
        return ReconcileOutcome(
            plan, error="A different testing deploy is recorded under this deploy's key."
        )
    driver_result = result.response_payload.get("result")
    driver_result = driver_result if isinstance(driver_result, dict) else {}
    records = result.response_payload.get("records")
    deployment_record_id = (
        str(records.get("deployment_record_id") or "") if isinstance(records, dict) else ""
    )
    deploy_status = str(driver_result.get("deploy_status") or "")
    post_deploy_status = str(driver_result.get("post_deploy_status") or "")
    plan.update(
        deployment_record_id=deployment_record_id,
        deploy_status=deploy_status,
        post_deploy_status=post_deploy_status,
    )
    if deploy_status != "pass" or post_deploy_status == "fail":
        # Statuses only: the driver's message can name provider targets and hosts.
        return ReconcileOutcome(
            plan,
            error=(
                f"The testing deploy failed: deploy {deploy_status or 'unknown'}, "
                f"post-deploy {post_deploy_status or 'unknown'}. Launchplane deploys "
                "again when a newer build is verified or the testing lane changes."
            ),
        )
    return ReconcileOutcome(plan)


@dataclass(frozen=True)
class _TestingAttempt:
    idempotency_key: str
    active: OdooStableTargetReplacementOperationRecord | None = None
    failed_attempts: int = 0
    last_failed: OdooStableTargetReplacementOperationRecord | None = None
    deployed_operation_id: str = ""

    @property
    def last_failed_operation_id(self) -> str:
        return self.last_failed.operation_id if self.last_failed is not None else ""


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
    last_failed: OdooStableTargetReplacementOperationRecord | None = None
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
                last_failed=last_failed,
            )
        if existing.status not in ODOO_STABLE_TARGET_REPLACEMENT_TERMINAL_OPERATION_STATUSES:
            return _TestingAttempt(
                idempotency_key=key,
                active=existing,
                failed_attempts=failed_attempts,
                last_failed=last_failed,
            )
        if existing.status == "pass":
            if testing_runs_desired():
                # It finished after the plan read the release; nothing to redeploy.
                return _TestingAttempt(
                    idempotency_key=key, deployed_operation_id=existing.operation_id
                )
        elif not is_staff_testing_hold_cancellation(existing):
            failed_attempts += 1
            last_failed = existing
        # A passed attempt that testing no longer runs was rolled back: deploy again.
        key = f"{base_key}:after-{existing.operation_id}"
    raise ProductReconcileError(
        f"The testing deploy has more than {TESTING_DEPLOY_MAX_ATTEMPT_CHAIN} attempts."
    )


# What each failed testing attempt's code means, in Launchplane's own words. The
# summary built from it never carries provider, script or exception text: that
# text can name targets, hosts and databases no redactor reliably finds.
TESTING_FAILURE_DESCRIPTIONS: dict[str, str] = {
    "deploy_failed": "The deploy step failed.",
    "post_deploy_override_failed": "Applying the lane's post-deploy setting overrides failed.",
    "post_deploy_failed": "The post-deploy update failed.",
    "post_deploy_not_run": "The deploy finished, but the post-deploy update did not run.",
    "health_check_failed": "The health check did not pass.",
    "canonical_check_failed": "The canonical URL check did not pass.",
    "logo_check_failed": "The website logo check did not pass.",
    "driver_result_failed": "The driver reported a failure outside its recorded steps.",
    "operation_failed": "The deploy stopped with an error before the driver returned a result.",
    "plan_build_failed": "Building the replacement plan failed before the deploy started.",
    "plan_not_ready": "The replacement plan was blocked before the deploy started.",
    "strategy_unsupported": "The deploy asked for a replacement strategy Launchplane does not run.",
    "target_not_compose": "The lane's Dokploy target is not a compose target.",
    "artifact_id_missing": "The deploy named no artifact and the lane records none.",
    "source_ref_missing": "The deploy named no source commit and the lane records none.",
    "artifact_repository_mismatch": (
        "The artifact's image repository does not match the product profile's image repository."
    ),
    "artifact_source_ref_mismatch": (
        "The deploy's source commit does not match the artifact manifest's source commit."
    ),
    "artifact_required_modules_missing": (
        "The artifact does not declare the Odoo modules Launchplane requires."
    ),
    "health_verification_required": (
        "The lane requires runtime identity, but the deploy turned health verification off."
    ),
    "health_url_missing": "The lane requires runtime identity, but it has no health URL.",
    "post_deploy_setup_failed": (
        "The deploy finished, but the post-deploy update could not start: its target or "
        "setting overrides could not be read."
    ),
    "release_tuple_mint_failed": (
        "The deploy passed, but recording its release tuple from the artifact manifest failed."
    ),
    "operation_cancelled": "The deploy was cancelled.",
    "operation_authorization_reconcile_refused": (
        "Launchplane's reconcile grant did not cover this deploy when it ran."
    ),
    "operation_authorization_revoked": (
        "The deploy's authorization was removed or narrowed before it ran."
    ),
    "operation_authorization_policy_unavailable": (
        "The authorization policy could not be read when the deploy ran."
    ),
    "operation_authorization_provenance_missing": (
        "The deploy had no recorded authorization and could not run."
    ),
}
_UNKNOWN_TESTING_FAILURE = "The deploy failed with a code this Launchplane does not describe."
# What each replacement-plan blocker code means, for ``plan_not_ready.<code>``.
PLAN_BLOCKER_DESCRIPTIONS: dict[str, str] = {
    "target_record_missing": "The lane has no Dokploy target record.",
    "target_id_record_missing": "The lane has no Dokploy target-id record.",
    "target_not_compose": "The lane's Dokploy target is not a compose target.",
    "allow_empty_data_required": (
        "A prelaunch rebuild request did not explicitly allow empty data."
    ),
    "volume_authority_unresolved": (
        "Launchplane could not resolve the lane's stored Odoo volume settings."
    ),
    "prelaunch_rebuild_policy_refused": "The lane's prelaunch rebuild policy refused the request.",
    "volume_env_keys_missing": "The current target is missing required Odoo volume settings.",
    "volume_authority_drift": (
        "The current target's Odoo volume settings do not match Launchplane's stored settings."
    ),
    "domains_missing": "The current target has no domains to carry over.",
    "runtime_keys_undeclared": (
        "The lane's upstream-restore settings are not declared in its product profile."
    ),
    "provider_keys_unrecorded": (
        "The current target has settings that no Launchplane record for the site holds."
    ),
    "upstream_restore_environment_invalid": (
        "The lane's upstream-restore settings are missing or invalid."
    ),
    "live_runtime_keys_invalid": (
        "The lane's runtime settings could not be checked against its product profile."
    ),
    "compose_or_override_render_failed": (
        "Launchplane could not render the replacement compose file or setting overrides."
    ),
    "current_artifact_changed": ("The lane's current artifact changed after the readiness check."),
    "artifact_manifest_missing": "Launchplane has no manifest for the deploy's artifact.",
    "artifact_repository_mismatch": (
        "The artifact's image repository does not match the product profile's image repository."
    ),
    "artifact_source_ref_missing": "The deploy has no source commit evidence for its artifact.",
    "artifact_source_ref_mismatch": (
        "The deploy's source commit does not match the artifact manifest's source commit."
    ),
    "artifact_required_modules_missing": (
        "The artifact does not declare the Odoo modules Launchplane requires."
    ),
}
_UNKNOWN_PLAN_BLOCKER = "Launchplane does not describe this blocker."
_PLAN_NOT_READY_PREFIX = "plan_not_ready."
_ERROR_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _testing_failure_reason(
    operation: OdooStableTargetReplacementOperationRecord,
) -> tuple[str, str]:
    """The failed attempt's error code and a structured summary of it.

    The code is the operation's own ``error_code``, or the first failed step of
    its driver result. The summary is that code's fixed description plus any
    validated env-key names, the result's step statuses and the worker attempt;
    never the error message.
    """
    error_code = operation.error_code.strip()
    if not _ERROR_CODE_PATTERN.match(error_code):
        error_code = _testing_failure_code(operation)
    if error_code.startswith("unexpected."):
        # The worker names an error it did not expect by its class, never its message.
        description = (
            "The deploy stopped with an unexpected error before the driver returned a result."
        )
    elif error_code.startswith(_PLAN_NOT_READY_PREFIX):
        # The apply names the plan's first blocker by its code, never its message.
        blocker_code = error_code.removeprefix(_PLAN_NOT_READY_PREFIX)
        description = (
            f"{TESTING_FAILURE_DESCRIPTIONS['plan_not_ready']} Blocker: "
            f"{PLAN_BLOCKER_DESCRIPTIONS.get(blocker_code, _UNKNOWN_PLAN_BLOCKER)}"
        )
    else:
        description = deploy_failure_description(error_code) or TESTING_FAILURE_DESCRIPTIONS.get(
            error_code, _UNKNOWN_TESTING_FAILURE
        )
    parts = [description]
    if operation.error_detail_keys:
        # Env-key names the record validated, such as undeclared runtime keys.
        parts.append(f"Keys: {', '.join(sorted(operation.error_detail_keys))}.")
    result = operation.result
    if result is not None:
        parts.append(
            f"Steps: deploy {result.deploy_status}, post-deploy {result.post_deploy_status}, "
            f"setting overrides {result.post_deploy_override_status}, "
            f"health {result.health_status}, canonical {result.canonical_status}, "
            f"logo {result.logo_status}."
        )
    if operation.attempt:
        parts.append(f"Worker attempt {operation.attempt}.")
    return error_code, " ".join(parts)


def _testing_failure_code(operation: OdooStableTargetReplacementOperationRecord) -> str:
    if operation.status == "cancelled":
        return "operation_cancelled"
    result = operation.result
    if result is None:
        return "operation_failed"
    steps = (
        ("deploy_failed", result.deploy_status == "fail"),
        ("post_deploy_override_failed", result.post_deploy_override_status == "fail"),
        ("post_deploy_failed", result.post_deploy_status == "fail"),
        ("post_deploy_not_run", result.post_deploy_status != "pass"),
        ("health_check_failed", result.health_status == "fail"),
        ("canonical_check_failed", result.canonical_status == "fail"),
        ("logo_check_failed", result.logo_status == "fail"),
    )
    return next((code for code, failed in steps if failed), "driver_result_failed")


def _plan_testing_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    repository_id: str,
    transport: BuildProvenanceTransport,
) -> tuple[dict[str, object], VerifiedBuildArtifact | VerifiedGenericWebBuild | None]:
    """Desired: the newest first-parent default-branch commit with a verified release build."""
    lane = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
    if lane is None:
        raise ProductReconcileError(f"Product {profile.product} has no testing lane.")
    desired, rejected, absent_reason = _desired_release(
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
        plan.update(action="none", reason=absent_reason, held=False)
        return plan, None
    desired_artifact_id = (
        desired.image_reference
        if isinstance(desired, VerifiedGenericWebBuild)
        else desired.manifest.artifact_id
    )
    desired_digest = desired.manifest.image.digest.lower()
    plan.update(
        desired_artifact_id=desired_artifact_id,
        desired_commit=desired.manifest.source_commit,
        desired_image_digest=desired_digest,
    )
    if desired_artifact_id == current_artifact_id or desired_digest == current_digest:
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
    previous_plan: dict[str, object] | None = None,
    lease_held: Callable[[], bool] = lambda: True,
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
    odoo = product_profile_uses_odoo_driver(profile)
    if not odoo and not reconciles_as_generic_web(profile):
        plan.update(held=True, reason="no_reconcile_preview_for_driver")
        return ReconcileOutcome(plan)
    if control_plane_root is None:
        raise ProductReconcileError("A preview reconcile needs the control-plane root.")
    previous = previous_plan or {}
    failed_attempts = 0
    if plan["action"] == "destroy":
        retry_key = _fingerprint(
            {
                "lifecycle": decision.lifecycle_token,
                "context": plan["context"],
                "reason": plan["reason"],
            }
        )
        if previous.get("destroy_retry_key") == retry_key:
            count = previous.get("destroy_failed_attempts", 0)
            if isinstance(count, int) and not isinstance(count, bool):
                failed_attempts = max(0, count)
        plan.update(destroy_retry_key=retry_key, destroy_failed_attempts=failed_attempts)
        if failed_attempts >= PREVIEW_DESTROY_MAX_FAILED_ATTEMPTS:
            plan.update(
                held=True,
                reason="preview_destroy_retry_limit",
                last_failed_error_code=previous.get(
                    "last_failed_error_code", "preview_apply_failed"
                ),
                last_failed_error_summary=previous.get("last_failed_error_summary", ""),
                destroy_retry_stop_reason=(
                    f"Preview destroy failed {failed_attempts} times; Launchplane stops retrying "
                    "until the preview lifecycle record or destroy reason changes. "
                    "The preview remains recorded; retirement requires an operator."
                ),
            )
            return ReconcileOutcome(plan)
    try:
        if not odoo:
            outcome = _run_generic_web_preview_operation(
                record_store=record_store,
                profile=profile,
                transport=transport,
                decision=decision,
                pull_request_number=pull_request_number,
                control_plane_root=control_plane_root,
                preview_hooks=preview_hooks,
                previous_plan=previous,
                lease_held=lease_held,
            )
        else:
            outcome = _run_preview_operation(
                record_store=record_store,
                profile=profile,
                transport=transport,
                decision=decision,
                pull_request_number=pull_request_number,
                control_plane_root=control_plane_root,
                preview_hooks=preview_hooks,
            )
    except Exception as error:
        if plan["action"] != "destroy" or (
            isinstance(error, ProductReconcileError) and error.code == "preview_lease_lost"
        ):
            raise
        _LOGGER.warning("Preview destroy of %s raised: %s", profile.product, error)
        outcome = _preview_failure(plan, "preview_reconcile_failed")
    if plan["action"] == "destroy" and outcome.error:
        plan["destroy_failed_attempts"] = failed_attempts + 1
    return outcome


def _plan_preview_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    repository_id: str,
    transport: BuildProvenanceTransport,
    pull_request_number: int,
) -> _PreviewDecision:
    """Desired: a preview of the PR head's verified build while the PR is open, draft or not."""
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
        eligible=_preview_eligible(pull_request),
    )
    if pull_request.get("state") != "open":
        return without_preview("pull_request_not_open")
    # The agent that opened the PR marks it for the Owner with a label.
    plan["owner_review_requested"] = profile.owner.review_label in _labels(pull_request)
    try:
        verified: VerifiedBuildArtifact | VerifiedGenericWebBuild = (
            verify_generic_web_build(
                transport=transport,
                repository=profile.repository,
                repository_id=repository_id,
                commit=head_sha,
                purpose="preview",
                image_repository=profile.image.repository,
                pull_request_number=pull_request_number,
            )
            if reconciles_as_generic_web(profile)
            else verify_build_artifact(
                transport=transport,
                repository=profile.repository,
                repository_id=repository_id,
                commit=head_sha,
                purpose="preview",
                context=preview_context,
                image_repository=profile.image.repository,
                pull_request_number=pull_request_number,
            )
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
    verified = decision.verified
    manifest = verified.manifest if isinstance(verified, VerifiedBuildArtifact) else None
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
        # or moved its head since the plan releases with no effect.
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
            return _preview_failure(plan, "preview_plan_blocked")
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
            return _preview_failure(plan, "preview_destination_refused")
        plan.update(preview_slug=issued_plan.preview_slug, preview_url=issued_plan.preview_url)
        if issued_plan.omitted_integration_credential_keys:
            plan["omitted_integration_credential_keys"] = list(
                issued_plan.omitted_integration_credential_keys
            )
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
            return _preview_failure(
                plan,
                "reconcile_required"
                if result.status == "reconcile_required"
                else "preview_apply_failed",
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
    except OdooPreviewApplyConfigError as error:
        plan["missing_keys"] = list(env_key_names(error.missing_keys))
        return _preview_failure(plan, "preview_config_refused")
    except OdooPreviewPlanProvenanceError:
        return _preview_failure(plan, "preview_provenance_refused")
    except (FileNotFoundError, ValueError, click.ClickException):
        return _preview_failure(plan, "preview_apply_failed")
    return ReconcileOutcome(plan)


def _preview_failure(plan: dict[str, object], code: str) -> ReconcileOutcome:
    summary = record_failure_summary(code)
    if code == "preview_config_refused":
        keys = env_key_names(cast(list[object], plan.get("missing_keys", [])))
        plan["missing_keys"] = list(keys)
        if keys:
            summary += f" Missing: {', '.join(keys)}."
    plan.update(last_failed_error_code=code, last_failed_error_summary=summary)
    return ReconcileOutcome(plan, error=summary)


def _run_generic_web_preview_operation(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    transport: BuildProvenanceTransport,
    decision: _PreviewDecision,
    pull_request_number: int,
    control_plane_root: Path,
    preview_hooks: PreviewProviderHooks,
    previous_plan: dict[str, object],
    lease_held: Callable[[], bool],
) -> ReconcileOutcome:
    """Refresh or destroy the PR's generic-web preview in-process, as its routes do.

    The lease on this PR's reconcile request makes it the only writer of the
    preview. A refresh that crashed is planned again and run again, as a re-run of
    the product's preview workflow was. Provider and driver text can name targets
    and hosts, so it goes to the worker log only.
    """
    plan = decision.plan
    operation: Literal["refresh", "destroy"] = "refresh" if plan["action"] == "apply" else "destroy"
    verified = decision.verified
    if operation == "refresh" and not isinstance(verified, VerifiedGenericWebBuild):
        raise ProductReconcileError("A preview refresh needs its verified build.")
    try:
        preview_slug = resolve_generic_web_preview_slug(
            profile=profile,
            preview_slug="",
            anchor_pr_number=pull_request_number,
            label="Preview reconcile",
        )
    except click.ClickException as error:
        raise ProductReconcileError(
            "The preview has no slug.", code="preview_slug_unavailable"
        ) from error
    if not launchplane_reconcile_preview_destination_allowed(
        record_store=record_store,
        product=profile.product,
        context=profile.preview.context,
        preview_slug=preview_slug,
    ):
        return _preview_failure(plan, "preview_destination_refused")
    plan["preview_slug"] = preview_slug
    if isinstance(verified, VerifiedGenericWebBuild):
        build_run = f"run-{verified.source_build.run_id}-{verified.source_build.run_attempt}"
        plan["preview_build_run"] = build_run
        if previous_plan.get("preview_build_run") == build_run and previous_plan.get(
            "preview_result_status"
        ) in {"fail", "blocked"}:
            # Every sweep would otherwise run the same failing refresh again.
            plan["preview_result_status"] = previous_plan["preview_result_status"]
            return _preview_failure(plan, "preview_build_failed")
    if decision.observed and _pull_request_moved(
        transport=transport,
        profile=profile,
        pull_request_number=pull_request_number,
        observed=decision.observed,
    ):
        plan["deferred"] = "pull_request_moved"
        return ReconcileOutcome(plan, deferred=True)
    if not lease_held():
        raise ProductReconcileError(_LEASE_LOST, code="preview_lease_lost")
    try:
        if isinstance(verified, VerifiedGenericWebBuild):
            _records, result = preview_hooks.refresh_generic_web(
                control_plane_root=control_plane_root,
                record_store=record_store,
                request=GenericWebPreviewRefreshEnvelope(
                    product=profile.product,
                    refresh=GenericWebPreviewRefreshRequest(
                        product=profile.product,
                        preview_slug=preview_slug,
                        anchor_pr_number=pull_request_number,
                        anchor_head_sha=verified.manifest.source_commit,
                        image_reference=verified.image_reference,
                        source=RECONCILE_SOURCE,
                        timeout_seconds=GENERIC_WEB_PREVIEW_TIMEOUT_SECONDS,
                    ),
                ),
                profile=profile,
            )
            status = str(result.get("refresh_status") or "")
        else:
            _records, result = preview_hooks.destroy_generic_web(
                control_plane_root=control_plane_root,
                record_store=record_store,
                request=GenericWebPreviewDestroyEnvelope(
                    product=profile.product,
                    destroy=GenericWebPreviewDestroyRequest(
                        product=profile.product,
                        preview_slug=preview_slug,
                        anchor_pr_number=pull_request_number,
                        destroy_reason="pull_request_not_open",
                        timeout_seconds=GENERIC_WEB_PREVIEW_TIMEOUT_SECONDS,
                    ),
                ),
                profile=profile,
            )
            status = str(result.get("destroy_status") or "")
    except (FileNotFoundError, ValueError, click.ClickException) as error:
        _LOGGER.warning(
            "Generic-web preview %s of %s refused: %s", operation, profile.product, error
        )
        plan["preview_result_status"] = "refused"
        return _preview_failure(plan, "preview_apply_failed")
    plan["preview_result_status"] = status
    if status != "pass":
        _LOGGER.warning(
            "Generic-web preview %s of %s ended %s: %s",
            operation,
            profile.product,
            status,
            result.get("error_message"),
        )
        return _preview_failure(plan, "preview_apply_failed")
    if isinstance(verified, VerifiedGenericWebBuild):
        preview_url = str(result.get("preview_url") or "")
        plan["preview_url"] = preview_url
        if not lease_held():
            # Another worker owns this PR now; its refresh decides what serves.
            raise ProductReconcileError(_LEASE_LOST, code="preview_lease_lost")
        _record_generic_web_preview_serving(
            record_store=record_store,
            profile=profile,
            pull_request_number=pull_request_number,
            preview_url=preview_url,
            verified_at=str(result.get("refresh_finished_at") or "") or _utc_now(),
            control_plane_root=control_plane_root,
        )
    return ReconcileOutcome(plan)


def _record_generic_web_preview_serving(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    preview_url: str,
    verified_at: str,
    control_plane_root: Path,
) -> None:
    """Make the refreshed generation the one the preview serves.

    The refresh waited until the preview's health endpoint reported the expected
    build, which is the check the product's preview workflow recorded before. A
    driver that already recorded the generation as ready needs nothing more.
    """
    preview = find_preview_record(
        record_store=cast(PreviewMutationRecordStore, record_store),
        context_name=profile.preview.context.strip(),
        anchor_repo=_preview_anchor_repo(profile),
        anchor_pr_number=pull_request_number,
    )
    if preview is None:
        raise ProductReconcileError("The preview refresh passed but recorded no preview.")
    if preview.active_generation_id and (
        preview.serving_generation_id == preview.active_generation_id
    ):
        return
    apply_generic_web_preview_verification_result(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=GenericWebPreviewVerificationEnvelope(
            product=profile.product,
            verification=GenericWebPreviewVerificationRequest(
                context=preview.context,
                anchor_repo=preview.anchor_repo,
                anchor_pr_number=pull_request_number,
                verification_status="pass",
                verified_at=verified_at,
                checked_urls=(preview_url,) if preview_url else (),
                timeout_seconds=GENERIC_WEB_PREVIEW_TIMEOUT_SECONDS,
            ),
        ),
    )


def reconcile_product_request(
    *,
    record_store: ProductReconcileStore,
    request: ProductReconcileRequestRecord,
    transport_factory: TransportFactory = resolve_build_provenance_transport,
    control_plane_root: Path | None = None,
    preview_hooks: PreviewProviderHooks = PreviewProviderHooks(),
    testing_hooks: TestingProviderHooks = TestingProviderHooks(),
    lease_held: Callable[[], bool] = lambda: True,
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
            control_plane_root=control_plane_root,
            testing_hooks=testing_hooks,
        )
    assert request.pull_request_number is not None
    return reconcile_preview_target(
        previous_plan=dict(request.last_plan),
        lease_held=lease_held,
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
    testing_hooks: TestingProviderHooks = TestingProviderHooks(),
    feedback_token: FeedbackTokenFactory = resolve_pull_request_feedback_token,
    public_origin: Callable[[], str] = launchplane_public_origin_from_env,
    owner_review_status: OwnerReviewStatusWriter | None = None,
) -> ProductReconcileRequestRecord | None:
    """Claim one request, reconcile it, and record the plan; one bad target never stops the worker."""
    request = record_store.claim_next_product_reconcile_request(lease_owner, lease_seconds)
    if request is None:
        return None
    try:
        with _LeaseHeartbeat(
            record_store=record_store,
            target_key=request.target_key,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        ) as lease:
            outcome = reconcile_product_request(
                record_store=record_store,
                request=request,
                transport_factory=transport_factory,
                control_plane_root=control_plane_root,
                preview_hooks=preview_hooks,
                testing_hooks=testing_hooks,
                lease_held=lease.held,
            )
    except Exception as error:
        _LOGGER.warning("Product reconcile of %s failed: %s", request.target_key, error)
        failed_plan: dict[str, object] = {"target": request.target_kind}
        if request.target_kind == "preview":
            # A failed read or lost lease must not re-arm a previously exhausted destroy.
            for key in ("destroy_retry_key", "destroy_failed_attempts"):
                if key in request.last_plan:
                    failed_plan[key] = request.last_plan[key]
            failed_plan["last_failed_error_code"] = getattr(error, "code", "")
        outcome = ReconcileOutcome(failed_plan, error=_error_text(error))
    else:
        if outcome.error:
            _LOGGER.warning("Product reconcile of %s failed: %s", request.target_key, outcome.error)
    if request.target_kind == "preview" and outcome.error:
        code = outcome.plan.get("last_failed_error_code")
        if code not in {
            "preview_plan_blocked",
            "preview_config_refused",
            "preview_provenance_refused",
            "preview_apply_failed",
            "preview_reconcile_failed",
            "preview_build_failed",
            "preview_destination_refused",
            "preview_credentials_unavailable",
            "preview_lease_lost",
            "preview_slug_unavailable",
            "reconcile_required",
        }:
            code = "preview_reconcile_failed"
        outcome = _preview_failure(dict(outcome.plan), str(code))
    plan = dict(outcome.plan)
    feedback = post_reconcile_feedback(
        record_store=record_store,
        request=request,
        plan=plan,
        error=outcome.error,
        feedback_token=feedback_token,
        public_origin=public_origin,
        source=RECONCILE_SOURCE,
        recorded_at=_utc_now(),
        owner_review_status=owner_review_status
        or _owner_review_status_writer(
            record_store=record_store,
            control_plane_root=control_plane_root,
            public_origin=public_origin,
        ),
    )
    if feedback is not None:
        plan[PR_FEEDBACK_PLAN_KEY] = feedback
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
        return record_store.complete_product_reconcile_request(
            request.target_key, lease_owner, "failed", plan, outcome.error
        )
    return record_store.complete_product_reconcile_request(
        request.target_key, lease_owner, "done", plan
    )


def _owner_review_status_writer(
    *,
    record_store: object,
    control_plane_root: Path | None,
    public_origin: Callable[[], str],
) -> OwnerReviewStatusWriter | None:
    """Write the Owner-review status as the preview feedback route does, with the
    preview context's feedback credential; the merge-train App cannot write statuses."""
    if control_plane_root is None:
        return None
    try:
        review_store = require_product_review_store(record_store)
    except TypeError:
        return None

    def write(
        profile: LaunchplaneProductProfileRecord, pull_request_number: int
    ) -> OwnerReviewStatus | None:
        return OwnerReviewStatusPublisher(
            control_plane_root=control_plane_root, public_origin=public_origin() or None
        ).publish(store=review_store, profile=profile, pull_request_number=pull_request_number)

    return write


class _LeaseHeartbeat:
    """Renew a claimed request's lease while its reconcile runs.

    A preview refresh can take longer than one lease; without renewal another
    worker could claim the same PR and change its preview at the same time. A
    worker that stops also stops renewing, so its request is reclaimed.
    """

    def __init__(
        self,
        *,
        record_store: ProductReconcileStore,
        target_key: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> None:
        self._record_store = record_store
        self._target_key = target_key
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._stop = Event()
        self._thread = Thread(
            target=self._run, name=f"product-reconcile-lease:{target_key}", daemon=True
        )

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def held(self) -> bool:
        """Renew now; False, failing closed, once the lease is lost or cannot be renewed."""
        try:
            return self._record_store.renew_product_reconcile_lease(
                self._target_key, self._lease_owner, self._lease_seconds
            )
        except Exception as error:  # noqa: BLE001 - an unknown lease is not held
            _LOGGER.warning(
                "Product reconcile lease on %s could not be checked: %s", self._target_key, error
            )
            return False

    def _run(self) -> None:
        while not self._stop.wait(self._lease_seconds / 3):
            try:
                if not self._record_store.renew_product_reconcile_lease(
                    self._target_key, self._lease_owner, self._lease_seconds
                ):
                    _LOGGER.warning("Product reconcile lease on %s was lost.", self._target_key)
                    return
            except Exception as error:  # noqa: BLE001 - the next beat tries again
                _LOGGER.warning(
                    "Product reconcile lease on %s was not renewed: %s", self._target_key, error
                )


def reconcile_reservation_scope(product: str) -> str:
    """The mutation scope of Launchplane's own reconcile work for one product."""
    return f"{RECONCILE_SOURCE}:{product.strip()}"


def request_product_reconcile_sweep(
    record_store: ProductReconcileStore,
    now: str,
    transport_factory: TransportFactory = resolve_build_provenance_transport,
) -> tuple[str, ...]:
    """Request every mapped product's testing target, live preview and open PR.

    A preview stays up while its pull request is open, so the sweep also lists each
    product's open PRs: one whose event was missed still gets its preview within a sweep.
    """
    targets: list[ProductReconcileTarget] = []
    inventory_records = record_store.list_repository_inventory_records()
    profiles = record_store.list_product_profile_records()
    for profile in profiles:
        if not profile.is_active or not _has_repository_identity(
            profile, inventory_records, profiles
        ):
            continue
        if not any(lane.instance == "testing" for lane in profile.lanes):
            continue
        targets.append(ProductReconcileTarget(product=profile.product, target_kind="testing"))
        preview_context = profile.preview.context.strip()
        if not preview_context:
            continue
        for number in _open_pull_requests(record_store, profile, transport_factory):
            targets.append(
                ProductReconcileTarget(
                    product=profile.product, target_kind="preview", pull_request_number=number
                )
            )
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


def _open_pull_requests(
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    transport_factory: TransportFactory,
) -> tuple[int, ...]:
    """The product's open PRs, drafts included; an unreadable list only skips this product."""
    if not profile.preview.enabled:
        return ()
    numbers: list[int] = []
    try:
        transport = transport_factory(record_store, profile)
        for page in range(1, OPEN_PULL_REQUEST_SWEEP_PAGES + 1):
            pulls = _list(
                transport.get_json(
                    f"/repos/{_repository_path(profile)}/pulls?state=open&per_page=100&page={page}"
                )
            )
            numbers.extend(
                pull["number"]
                for pull in pulls
                if isinstance(pull, dict) and isinstance(pull.get("number"), int)
            )
            if len(pulls) < 100:
                break
    except (BuildProvenanceError, ProductReconcileError, OSError, ValueError) as error:
        _LOGGER.warning("Sweep could not list %s's open pull requests: %s", profile.product, error)
    return tuple(numbers)


def _has_repository_identity(
    profile: LaunchplaneProductProfileRecord,
    inventory_records: tuple[RepositoryInventoryRecord, ...],
    profiles: tuple[LaunchplaneProductProfileRecord, ...],
) -> bool:
    try:
        product_repository_identity_from_inventory(
            profile=profile, inventory_records=inventory_records, profiles=profiles
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
) -> tuple[VerifiedBuildArtifact | VerifiedGenericWebBuild | None, list[dict[str, str]], str]:
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
    try:
        payload = _object(
            transport.get_json(
                f"/repos/{_repository_path(profile)}/actions/workflows/{workflow_file}/runs?{query}"
            ),
            "workflow runs",
        )
    except BuildProvenanceError as error:
        if not isinstance(error.__cause__, HTTPError) or error.__cause__.code != 404:
            raise
        # A successful, complete Actions inventory distinguishes absence from hidden access.
        workflows = _object(
            transport.get_json(
                f"/repos/{_repository_path(profile)}/actions/workflows?per_page=100"
            ),
            "workflows",
        )
        entries = workflows.get("workflows")
        if (
            not isinstance(entries, list)
            or workflows.get("total_count") != len(entries)
            or any(not isinstance(entry, dict) or not entry.get("path") for entry in entries)
            or any(entry["path"] == BUILD_WORKFLOW_PATH for entry in entries)
        ):
            raise
        return None, [], "build_workflow_missing"
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
    generic_web = reconciles_as_generic_web(profile)
    for commit in ordered[:TESTING_VERIFY_LIMIT]:
        try:
            verified: VerifiedBuildArtifact | VerifiedGenericWebBuild = (
                verify_generic_web_build(
                    transport=transport,
                    repository=profile.repository,
                    repository_id=repository_id,
                    commit=commit,
                    purpose="release",
                    image_repository=profile.image.repository,
                )
                if generic_web
                else verify_build_artifact(
                    transport=transport,
                    repository=profile.repository,
                    repository_id=repository_id,
                    commit=commit,
                    purpose="release",
                    context=lane.context,
                    image_repository=profile.image.repository,
                )
            )
            return verified, rejected, ""
        except BuildProvenanceError as error:
            rejected.append({"commit": commit, "error": str(error)})
    return None, rejected, "no_verified_build"


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
        "current_preview_url": "",
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
    current.update(
        current_preview_id=preview.preview_id,
        current_state=preview.state,
        current_preview_url=preview.canonical_url.strip(),
    )
    generic_web = reconciles_as_generic_web(profile)
    # A generic-web refresh changes the preview's one application in place, so its
    # latest generation is what runs, whatever an earlier one served.
    generation_id = (
        preview.active_generation_id or preview.serving_generation_id
        if generic_web
        else preview.serving_generation_id or preview.active_generation_id
    )
    if not generation_id:
        return current, lifecycle_token
    try:
        generation = record_store.read_preview_generation_record(generation_id)
    except FileNotFoundError:
        return current, lifecycle_token
    if generic_web and generation.state != "ready":
        # Its verification is not recorded (the refresh failed, or the worker stopped
        # before recording it), so it serves nothing, though it names its image.
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
        pull_request
    ) != observed.get("eligible")


def _preview_eligible(pull_request: dict[str, object]) -> bool:
    """A preview stays up until its PR closes or merges; drafts and labels play no part."""
    return pull_request.get("state") == "open"


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
