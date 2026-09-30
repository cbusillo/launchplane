"""Plan what a product's testing lane and PR previews should run.

A reconcile reads GitHub's record of the product's builds and Launchplane's own
records, and says what it would change. It is plan-only until the owner
approves Launchplane acting on its own authority (#2623): it deploys, applies,
destroys, and records nothing but the plan on its request.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Literal, Protocol, cast
from urllib.parse import quote, urlencode

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane import secrets
from control_plane.build_provenance import (
    BUILD_WORKFLOW_PATH,
    BuildProvenanceError,
    BuildProvenanceTransport,
    GitHubBuildProvenanceTransport,
    VerifiedBuildArtifact,
    first_parent_history,
    verify_build_artifact,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
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
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppIdentityError,
    mint_build_provenance_installation_token,
)
from control_plane.merge_train_github_token import MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.workflows.launchplane import PreviewMutationRecordStore, find_preview_record

PRODUCT_RECONCILE_SWEEP_SECONDS = 30 * 60
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


TransportFactory = Callable[[object, LaunchplaneProductProfileRecord], BuildProvenanceTransport]


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
    if str(app.repository_id) != profile.repository_id:
        raise ProductReconcileError(
            "No build-provenance token: the merge train App's repository id is not the "
            "product's recorded repository id."
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
            repository_id=profile.repository_id,
        )
    except (GitHubAppIdentityError, click.ClickException, OSError, ValueError) as error:
        raise ProductReconcileError(
            f"No build-provenance token: minting failed ({type(error).__name__}: {error})."
        ) from error
    return GitHubBuildProvenanceTransport(token=token.token)


def plan_testing_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    transport: BuildProvenanceTransport,
) -> dict[str, object]:
    """Desired: the newest first-parent default-branch commit with a verified release build."""
    lane = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
    if lane is None:
        raise ProductReconcileError(f"Product {profile.product} has no testing lane.")
    desired, rejected = _desired_release(transport=transport, profile=profile, lane=lane)
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
        return plan
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
        plan.update(action="deploy", held=True)
    return plan


def plan_preview_target(
    *,
    record_store: ProductReconcileStore,
    profile: LaunchplaneProductProfileRecord,
    transport: BuildProvenanceTransport,
    pull_request_number: int,
) -> dict[str, object]:
    """Desired: a preview of the PR head's verified build while open and labeled."""
    preview_context = profile.preview.context.strip()
    plan: dict[str, object] = {
        "target": "preview",
        "pull_request_number": pull_request_number,
        "context": preview_context,
    }
    current = _current_preview(
        record_store=record_store,
        profile=profile,
        preview_context=preview_context,
        pull_request_number=pull_request_number,
    )
    plan.update(current)
    live = bool(current["current_preview_id"])

    def without_preview(reason: str) -> dict[str, object]:
        plan["reason"] = reason
        if live:
            plan.update(action="destroy", held=True)
        else:
            plan.update(action="none", held=False)
        return plan

    if not profile.preview.enabled or not preview_context:
        return without_preview("preview_not_configured")
    pull_request = _object(
        transport.get_json(f"/repos/{_repository_path(profile)}/pulls/{pull_request_number}"),
        "pull request",
    )
    head_sha = str(_object(pull_request.get("head"), "pull request head").get("sha") or "")
    if not head_sha:
        raise ProductReconcileError(f"GitHub returned no head for PR {pull_request_number}.")
    labels = {
        str(label.get("name") or "").strip()
        for label in (item for item in _list(pull_request.get("labels")) if isinstance(item, dict))
    }
    plan["head_sha"] = head_sha.lower()
    if pull_request.get("state") != "open":
        return without_preview("pull_request_not_open")
    if profile.preview.enable_label not in labels:
        return without_preview("preview_label_missing")
    try:
        verified = verify_build_artifact(
            transport=transport,
            repository=profile.repository,
            repository_id=profile.repository_id,
            commit=head_sha,
            purpose="preview",
            context=preview_context,
            image_repository=profile.image.repository,
            pull_request_number=pull_request_number,
        )
    except BuildProvenanceError as error:
        # A PR's build is untrusted input: an unprovable or malformed one is not built yet.
        plan.update(action="wait", reason="no_verified_build", detail=str(error), held=False)
        return plan
    manifest = verified.manifest
    desired_digest = manifest.image.digest.lower()
    plan.update(desired_commit=manifest.source_commit, desired_image_digest=desired_digest)
    if live and desired_digest == current["current_image_digest"]:
        plan.update(action="none", reason="already_serving", held=False)
    else:
        plan.update(action="apply", held=True)
    return plan


def plan_product_reconcile_request(
    *,
    record_store: ProductReconcileStore,
    request: ProductReconcileRequestRecord,
    transport_factory: TransportFactory = resolve_build_provenance_transport,
) -> dict[str, object]:
    try:
        profile = record_store.read_product_profile_record(request.product)
    except FileNotFoundError as error:
        raise ProductReconcileError(f"Product profile {request.product} is missing.") from error
    if not profile.is_active:
        return {"target": request.target_kind, "action": "none", "reason": "product_inactive"}
    if not profile.repository_id:
        raise ProductReconcileError(
            f"Product {profile.product} needs its immutable repository id recorded."
        )
    transport = transport_factory(record_store, profile)
    if request.target_kind == "testing":
        return plan_testing_target(record_store=record_store, profile=profile, transport=transport)
    assert request.pull_request_number is not None
    return plan_preview_target(
        record_store=record_store,
        profile=profile,
        transport=transport,
        pull_request_number=request.pull_request_number,
    )


def run_product_reconcile_once(
    *,
    record_store: ProductReconcileStore,
    lease_owner: str,
    lease_seconds: int = 300,
    transport_factory: TransportFactory = resolve_build_provenance_transport,
) -> ProductReconcileRequestRecord | None:
    """Claim one request, plan it, and record the plan; one bad target never stops the worker."""
    request = record_store.claim_next_product_reconcile_request(lease_owner, lease_seconds)
    if request is None:
        return None
    try:
        plan = plan_product_reconcile_request(
            record_store=record_store, request=request, transport_factory=transport_factory
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
    return record_store.complete_product_reconcile_request(
        request.target_key, lease_owner, "done", {**plan, "mode": "plan_only"}
    )


def request_product_reconcile_sweep(
    record_store: ProductReconcileStore, now: str
) -> tuple[str, ...]:
    """Request every mapped product's testing target and every live preview; no GitHub reads."""
    targets: list[ProductReconcileTarget] = []
    for profile in record_store.list_product_profile_records():
        if not profile.is_active or not profile.repository_id:
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


def _desired_release(
    *,
    transport: BuildProvenanceTransport,
    profile: LaunchplaneProductProfileRecord,
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
                    repository_id=profile.repository_id,
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
) -> dict[str, object]:
    current: dict[str, object] = {
        "current_preview_id": "",
        "current_state": "",
        "current_head_sha": "",
        "current_image_digest": "",
    }
    if not preview_context:
        return current
    preview = find_preview_record(
        record_store=cast(PreviewMutationRecordStore, record_store),
        context_name=preview_context,
        anchor_repo=_preview_anchor_repo(profile),
        anchor_pr_number=pull_request_number,
    )
    if preview is None or preview.state in _ENDED_PREVIEW_STATES:
        return current
    current.update(current_preview_id=preview.preview_id, current_state=preview.state)
    generation_id = preview.serving_generation_id or preview.active_generation_id
    if not generation_id:
        return current
    try:
        generation = record_store.read_preview_generation_record(generation_id)
    except FileNotFoundError:
        return current
    digest = ""
    if generation.runtime_identity is not None:
        digest = _image_reference_digest(generation.runtime_identity.image_reference)
    if not digest and generation.artifact_id:
        digest = _artifact_digest(record_store, generation.artifact_id)
    current.update(
        current_head_sha=generation.anchor_summary.head_sha.lower(),
        current_image_digest=digest.lower(),
    )
    return current


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
    return f"Unexpected {type(error).__name__} while planning."


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProductReconcileError(f"GitHub returned an unexpected {label}.")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
