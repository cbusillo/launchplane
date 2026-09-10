from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Annotated, Literal, Never, assert_never, cast

from fastapi import Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.change_impact_service import (
    ChangeImpactRepositoryEvidenceProvider,
    require_change_impact_policy_read_store,
)
from control_plane.contracts.change_impact import ChangeImpactTarget, ChangeImpactTargetReference
from control_plane.contracts.advisory_check_projection import AdvisoryCheckProjectionResult
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
    OWNER_ACCEPTANCE_PROJECT_ACTION,
    OWNER_ACCEPTANCE_READ_ACTION,
    OwnerAcceptanceDecision,
    OwnerAcceptanceDecisionStatus,
    OwnerAcceptanceEventRecord,
    OwnerAcceptanceHumanActionSemantics,
    OwnerAcceptanceResolutionEvidence,
    OwnerAcceptanceTransitionError,
    OwnerAcceptanceViewerBindingEligibility,
    owner_acceptance_event_replay_matches,
    owner_acceptance_human_action_semantics,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.owner_acceptance import (
    OwnerAcceptanceAuthorizationError,
    OwnerAcceptanceBindingConflictError,
    OwnerAcceptanceEvaluationUnavailableError,
    OwnerAcceptanceEventConflictError,
    OwnerAcceptanceSelfReviewDeniedError,
    OwnerAcceptanceWriteResult,
    evaluate_owner_acceptance,
    evaluate_owner_acceptance_viewer_eligibility,
    record_owner_acceptance_event,
    require_owner_acceptance_event_store,
)
from control_plane.owner_acceptance_queue import (
    OwnerAcceptanceQueueEntry,
    build_owner_acceptance_queue,
)
from control_plane.owner_acceptance_current_items import (
    OwnerAcceptanceCurrentItem,
    OwnerAcceptanceCurrentItemsProvider,
    OwnerAcceptanceCurrentItemsRepositoryFailure,
    build_owner_acceptance_current_items,
)
from control_plane.owner_acceptance_projection import (
    OwnerAcceptanceProjectionReconciliationError,
    OwnerAcceptanceProjectionService,
)
from control_plane.product_owner_service import (
    get_product_owner_read_model,
    require_product_owner_policy_read_store,
)
from control_plane.repository_inventory import require_repository_inventory_read_store
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.workflows.launchplane import github_api_request


logger = logging.getLogger(__name__)


OWNER_ACCEPTANCE_EVALUATION_ROUTE = "/v1/owner-acceptance/evaluation"
OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE = "/v1/owner-acceptance/owner-evaluation"
OWNER_ACCEPTANCE_EVENTS_ROUTE = "/v1/owner-acceptance/events"
OWNER_ACCEPTANCE_EVENT_ROUTE = "/v1/owner-acceptance/events/{event_id}"
OWNER_ACCEPTANCE_QUEUE_ROUTE = "/v1/owner-acceptance/queue"
OWNER_ACCEPTANCE_CURRENT_ITEMS_ROUTE = "/v1/owner-acceptance/current-items"
OWNER_ACCEPTANCE_PROJECT_ROUTE = "/v1/owner-acceptance/project"


class OwnerAcceptanceProjectionUnavailableError(RuntimeError):
    pass


class OwnerAcceptanceProjectionReconciliationRequiredError(RuntimeError):
    pass


class OwnerAcceptanceEventNotPersistedError(RuntimeError):
    pass


class OwnerAcceptanceEventNotPersistedProjectionError(RuntimeError):
    pass


class OwnerAcceptanceEventPersistenceUnknownError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnerAcceptanceRouteDependencies:
    common: ReadRouteDependencies
    read_browser_mutation_identity: Callable[..., LaunchplaneIdentity]
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider
    read_write_identity: Callable[..., LaunchplaneIdentity] | None = None
    github_app_token: Callable[[str, str], GitHubAppInstallationToken] | None = None
    github_api: Callable[..., object] = github_api_request
    public_origin: str | None = None
    projection_service: OwnerAcceptanceProjectionService | None = None


class OwnerAcceptanceEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    target: ChangeImpactTargetReference
    action: Literal["accepted", "changes_requested", "revoked"]
    expected_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=4000)
    resolution: OwnerAcceptanceResolutionEvidence | None = None

    @model_validator(mode="after")
    def _validate_reason(self) -> "OwnerAcceptanceEventEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance event envelope schema version.")
        self.reason = self.reason.strip()
        if self.action != "accepted" and not self.reason:
            raise ValueError(f"Owner acceptance action {self.action!r} requires a reason")
        return self


class OwnerAcceptanceViewerCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_write_authorized: bool = Field(
        description=(
            "Whether the evaluated identity currently has route-level "
            "owner_acceptance_event.write authorization. Browser session and current product "
            "Owner authority are revalidated separately when an event is submitted."
        )
    )
    bindings: tuple[OwnerAcceptanceViewerBindingEligibility, ...] = Field(
        default=(),
        description=(
            "Viewer-specific advisory eligibility for exact product bindings. Missing or "
            "ineligible bindings must not expose event controls. Event writes revalidate "
            "the exact binding and current product Owner authority independently."
        ),
    )

    @model_validator(mode="after")
    def _validate_capabilities(self) -> "OwnerAcceptanceViewerCapabilities":
        binding_digests = tuple(binding.binding_sha256 for binding in self.bindings)
        if len(binding_digests) != len(set(binding_digests)):
            raise ValueError("Owner acceptance viewer binding eligibility must be unique.")
        if not self.event_write_authorized and any(
            binding.can_submit_event for binding in self.bindings
        ):
            raise ValueError(
                "Owner acceptance binding eligibility requires route-level event access."
            )
        return self


class OwnerAcceptanceEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    decision: OwnerAcceptanceDecision
    viewer_capabilities: OwnerAcceptanceViewerCapabilities


OwnerReviewStatus = Literal[
    "not_required",
    "review_required",
    "accepted",
    "changes_requested",
    "unavailable",
]


class OwnerAcceptanceOwnerProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    environment: str
    review_status: OwnerReviewStatus
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_url: str | None = None
    resolution_required: bool = False
    resolution_evidence_references: tuple[str, ...] = ()
    can_accept: bool
    can_request_changes: bool
    can_revoke: bool

    @model_validator(mode="after")
    def _validate_owner_product(self) -> "OwnerAcceptanceOwnerProduct":
        if self.can_accept and self.preview_url is None:
            raise ValueError("Owner product acceptance requires a preview URL.")
        if self.resolution_required and (
            self.preview_url is None or not self.resolution_evidence_references
        ):
            raise ValueError("Owner product resolution requires bound preview evidence references.")
        if not self.resolution_required and self.resolution_evidence_references:
            raise ValueError("Owner product resolution references require a pending resolution.")
        return self


class OwnerAcceptanceOwnerEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    review_status: OwnerReviewStatus
    evaluated_at: str
    products: tuple[OwnerAcceptanceOwnerProduct, ...]


class OwnerAcceptanceEventSemantics(BaseModel):
    """Machine-readable projection of a stored human product-review action.

    The stored enum and every persisted digest stay unchanged. Acceptance is an
    authoritative merge-admission prerequisite, while technical readiness,
    landing, and production authorization remain separate decisions.
    """

    model_config = ConfigDict(extra="forbid")

    human_action_semantics: OwnerAcceptanceHumanActionSemantics


class OwnerAcceptanceEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_kind: Literal["full"] = "full"
    status: Literal["ok"] = "ok"
    trace_id: str
    write_status: Literal["written", "replayed"]
    record: OwnerAcceptanceEventRecord
    semantics: OwnerAcceptanceEventSemantics
    decision: OwnerAcceptanceDecision


class OwnerAcceptanceEventReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_kind: Literal["receipt"] = "receipt"
    status: Literal["ok"] = "ok"
    trace_id: str
    write_status: Literal["written", "replayed"]


OwnerAcceptanceEventWriteResponse = Annotated[
    OwnerAcceptanceEventResponse | OwnerAcceptanceEventReceipt,
    Field(discriminator="response_kind"),
]


class OwnerAcceptanceEventReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: OwnerAcceptanceEventRecord
    semantics: OwnerAcceptanceEventSemantics


class OwnerAcceptanceQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    derivation: Literal["ledger_only"] = "ledger_only"
    generated_at: str
    total: int
    candidate: int
    truncated: bool
    has_more: bool
    entry_count: int
    entries: tuple[OwnerAcceptanceQueueEntry, ...]


class OwnerAcceptanceCurrentItemsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    derivation: Literal["active_change_impact_open_pull_requests"] = (
        "active_change_impact_open_pull_requests"
    )
    generated_at: str
    viewer_capabilities: OwnerAcceptanceViewerCapabilities
    repository_count: int
    repository_failure_count: int
    candidate_count: int
    evaluated_count: int
    unavailable_count: int
    truncated: bool
    items: tuple[OwnerAcceptanceCurrentItem, ...]
    repository_failures: tuple[OwnerAcceptanceCurrentItemsRepositoryFailure, ...]


class OwnerAcceptanceProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    target: ChangeImpactTargetReference

    @model_validator(mode="after")
    def _validate_request(self) -> "OwnerAcceptanceProjectionRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported Owner acceptance projection schema version.")
        return self


class OwnerAcceptanceProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    decision: OwnerAcceptanceDecision
    result: AdvisoryCheckProjectionResult


def _event_semantics(record: OwnerAcceptanceEventRecord) -> OwnerAcceptanceEventSemantics:
    return OwnerAcceptanceEventSemantics(
        human_action_semantics=owner_acceptance_human_action_semantics(record.action),
    )


def _owner_acceptance_event_persistence_outcome(
    *,
    store: object,
    record: OwnerAcceptanceEventRecord,
) -> Literal["persisted", "absent", "unknown"]:
    try:
        persisted = require_owner_acceptance_event_store(store).read_owner_acceptance_event_record(
            record.event_id
        )
    except FileNotFoundError:
        return "absent"
    except Exception:
        logger.warning(
            "Could not determine Owner acceptance event persistence outcome.",
            exc_info=True,
        )
        return "unknown"
    if owner_acceptance_event_replay_matches(persisted, record):
        return "persisted"
    logger.error("Owner acceptance event id resolved to a different persisted payload.")
    return "unknown"


def _owner_review_status(status: OwnerAcceptanceDecisionStatus) -> OwnerReviewStatus:
    match status:
        case "not_required":
            return "not_required"
        case "pending" | "revoked":
            return "review_required"
        case "accepted":
            return "accepted"
        case "changes_requested":
            return "changes_requested"
        case "stale" | "unavailable":
            return "unavailable"
        case unhandled:
            assert_never(unhandled)


def _current_owner_repository_id(
    *,
    store: object,
    repository: str,
    identity: GitHubHumanIdentity,
) -> str | None:
    inventory_store = require_repository_inventory_read_store(store)
    policy_store = require_product_owner_policy_read_store(store)
    inventory_by_id: dict[str, list[RepositoryInventoryRecord]] = {}
    for record in inventory_store.list_repository_inventory_records():
        inventory_by_id.setdefault(record.repository_id, []).append(record)
    matching_repository_ids: list[str] = []
    for repository_id, records in inventory_by_id.items():
        highest_revision = max(record.inventory_revision for record in records)
        current = tuple(
            record for record in records if record.inventory_revision == highest_revision
        )
        if len(current) != 1:
            continue
        record = current[0]
        if (
            record.inventory_state == "tracked"
            and record.repository.casefold() == repository.casefold()
        ):
            matching_repository_ids.append(repository_id)
    if len(matching_repository_ids) != 1:
        return None
    repository_id = matching_repository_ids[0]

    policy_scopes: set[tuple[str, str]] = set()
    for policy in policy_store.list_product_owner_policy_records():
        policy_scopes.add((policy.product, policy.system))
    for product, system in policy_scopes:
        current_policy = get_product_owner_read_model(
            store=store,
            product=product,
            system=system,
        ).current_policy
        if current_policy is None:
            continue
        for owner in current_policy.owners:
            if (
                owner.identity.provider == "github"
                and owner.identity.provider_subject_id == str(identity.github_id)
                and repository_id in owner.repository_ids
            ):
                return repository_id
    return None


def register_owner_acceptance_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: OwnerAcceptanceRouteDependencies,
) -> None:
    common = dependencies.common
    projection_identity = dependencies.read_write_identity or common.read_identity
    projection_service = dependencies.projection_service or OwnerAcceptanceProjectionService(
        repository_evidence_provider=dependencies.repository_evidence_provider,
        github_app_token=dependencies.github_app_token,
        public_origin=dependencies.public_origin,
        api_request=dependencies.github_api,
    )

    def viewer_capabilities(
        *,
        identity: LaunchplaneIdentity,
        record_store: object,
        decisions: tuple[OwnerAcceptanceDecision, ...],
    ) -> OwnerAcceptanceViewerCapabilities:
        event_write_authorized = common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        )
        bindings = (
            evaluate_owner_acceptance_viewer_eligibility(
                store=record_store,
                decisions=decisions,
                identity=identity,
            )
            if event_write_authorized
            else ()
        )
        return OwnerAcceptanceViewerCapabilities(
            event_write_authorized=event_write_authorized,
            bindings=bindings,
        )

    def evaluate(
        repository: Annotated[
            str,
            Query(min_length=3, max_length=256, pattern=r"^[^/\s]+/[^/\s]+$"),
        ],
        pull_request_number: Annotated[int, Query(ge=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceEvaluationResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read Owner acceptance evaluation.",
            )
        try:
            decision = evaluate_owner_acceptance(
                store=record_store,
                repository_evidence_provider=dependencies.repository_evidence_provider,
                target=ChangeImpactTargetReference(
                    repository=repository,
                    pull_request_number=pull_request_number,
                ),
            )
        except (OwnerAcceptanceEvaluationUnavailableError, ValueError):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_evidence_unavailable",
                message="Owner acceptance evidence is unavailable.",
            ) from None
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return OwnerAcceptanceEvaluationResponse(
            trace_id=trace_id,
            decision=decision,
            viewer_capabilities=viewer_capabilities(
                identity=identity,
                record_store=record_store,
                decisions=(decision,),
            ),
        )

    def evaluate_for_owner(
        repository: Annotated[
            str,
            Query(min_length=3, max_length=256, pattern=r"^[^/\s]+/[^/\s]+$"),
        ],
        pull_request_number: Annotated[int, Query(ge=1)],
        identity: Annotated[
            LaunchplaneIdentity,
            Depends(common.read_identity),
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceOwnerEvaluationResponse:
        trace_id = common.next_trace_id()

        def unavailable() -> Never:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="owner_review_unavailable",
                message="This product review is unavailable.",
            )

        if not isinstance(identity, GitHubHumanIdentity):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="github_human_required",
                message="Product review requires a browser-authenticated GitHub human.",
            )
        evaluated_at = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        try:
            repository_id = _current_owner_repository_id(
                store=record_store,
                repository=repository,
                identity=identity,
            )
        except (FileNotFoundError, LookupError, TypeError, ValueError):
            unavailable()
        if repository_id is None:
            unavailable()

        try:
            decision = evaluate_owner_acceptance(
                store=record_store,
                repository_evidence_provider=dependencies.repository_evidence_provider,
                target=ChangeImpactTargetReference(
                    repository=repository,
                    pull_request_number=pull_request_number,
                ),
                evaluated_at=evaluated_at,
            )
        except (OwnerAcceptanceEvaluationUnavailableError, TypeError, ValueError):
            unavailable()
        try:
            if (
                _current_owner_repository_id(
                    store=record_store,
                    repository=repository,
                    identity=identity,
                )
                != repository_id
            ):
                unavailable()
        except (FileNotFoundError, LookupError, TypeError, ValueError):
            unavailable()
        if decision.status == "not_required":
            return OwnerAcceptanceOwnerEvaluationResponse(
                trace_id=trace_id,
                review_status="not_required",
                evaluated_at=decision.evaluated_at,
                products=(),
            )
        if not decision.products:
            unavailable()

        eligibility_by_binding = {
            eligibility.binding_sha256: eligibility
            for eligibility in evaluate_owner_acceptance_viewer_eligibility(
                store=record_store,
                decisions=(decision,),
                identity=identity,
            )
            if eligibility.reason_code in {"current_product_owner", "self_review_denied"}
        }
        event_write_authorized = common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        )
        products: list[OwnerAcceptanceOwnerProduct] = []
        for product in decision.products:
            binding = product.binding
            if binding is None or binding.repository_id != repository_id:
                continue
            eligibility = eligibility_by_binding.get(binding.binding_sha256)
            if eligibility is None:
                continue
            preview_url = binding.preview.preview_url if binding.preview is not None else None
            resolution_required = bool(
                binding.preview is not None
                and product.current_event is not None
                and product.current_event.binding.binding_sha256 == binding.binding_sha256
                and product.current_event.action == "changes_requested"
            )
            products.append(
                OwnerAcceptanceOwnerProduct(
                    product=product.product,
                    environment=product.environment,
                    review_status=_owner_review_status(product.status),
                    binding_sha256=binding.binding_sha256,
                    preview_url=preview_url,
                    resolution_required=resolution_required,
                    resolution_evidence_references=(
                        (
                            f"preview:{binding.preview.preview_id}",
                            f"preview-generation:{binding.preview.serving_generation_id}",
                        )
                        if resolution_required and binding.preview is not None
                        else ()
                    ),
                    can_accept=bool(
                        event_write_authorized and eligibility.can_accept and preview_url
                    ),
                    can_request_changes=bool(
                        event_write_authorized and eligibility.can_request_changes
                    ),
                    can_revoke=bool(event_write_authorized and eligibility.can_revoke),
                )
            )
        if not products:
            unavailable()
        status_precedence: tuple[OwnerReviewStatus, ...] = (
            "unavailable",
            "changes_requested",
            "review_required",
            "accepted",
            "not_required",
        )
        product_statuses = {product.review_status for product in products}
        review_status = next(status for status in status_precedence if status in product_statuses)
        return OwnerAcceptanceOwnerEvaluationResponse(
            trace_id=trace_id,
            review_status=review_status,
            evaluated_at=decision.evaluated_at,
            products=tuple(products),
        )

    def write_event(
        envelope: OwnerAcceptanceEventEnvelope,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
            ),
        ],
        identity: Annotated[
            LaunchplaneIdentity,
            Depends(dependencies.read_browser_mutation_identity),
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceEventWriteResponse:
        trace_id = common.next_trace_id()
        if not isinstance(identity, GitHubHumanIdentity):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="github_human_required",
                message="Owner acceptance events require a browser-authenticated GitHub human.",
            )
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot write Owner acceptance events.",
            )
        broad_read_authorized = common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        )
        if not broad_read_authorized:
            try:
                owner_repository_id = _current_owner_repository_id(
                    store=record_store,
                    repository=envelope.target.repository,
                    identity=identity,
                )
            except (FileNotFoundError, LookupError, TypeError, ValueError):
                owner_repository_id = None
            if owner_repository_id is None:
                logger.info(
                    "Limited Owner acceptance event write failed repository ownership prefilter."
                )
                raise common.http_error(
                    status_code=404,
                    trace_id=trace_id,
                    code="owner_review_unavailable",
                    message="This product review is unavailable.",
                )

        def bounded_message(error: Exception, limited_message: str) -> str:
            return str(error) if broad_read_authorized else limited_message

        try:
            projection_service.resolve_current(
                store=record_store,
                target=envelope.target,
            )
        except (OwnerAcceptanceEvaluationUnavailableError, TypeError, ValueError) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_projection_unavailable",
                message=bounded_message(
                    error,
                    "Owner acceptance projection is unavailable.",
                ),
            ) from error

        try:
            with projection_service.lock_current(
                store=record_store,
                target=envelope.target,
            ) as locked_projection_target:
                prewrite_projection_target: ChangeImpactTarget | None = None
                prewrite_event_record: OwnerAcceptanceEventRecord | None = None

                def project_conservative_check(record: OwnerAcceptanceEventRecord) -> None:
                    nonlocal prewrite_event_record, prewrite_projection_target
                    binding = record.binding
                    record_target = ChangeImpactTarget(
                        repository_id=binding.repository_id,
                        repository_owner_id=binding.repository_owner_id,
                        repository=binding.repository,
                        pull_request_number=binding.pull_request_number,
                        head_sha=binding.head_sha,
                        tree_sha=binding.tree_sha,
                    )
                    try:
                        projection_service.project_conservative_locked(
                            lock_target=locked_projection_target,
                            exact_target=record_target,
                            source_event_id=idempotency_key,
                        )
                    except Exception as projection_error:
                        raise OwnerAcceptanceProjectionUnavailableError(
                            str(projection_error)
                        ) from projection_error
                    prewrite_event_record = record
                    prewrite_projection_target = record_target

                try:
                    result: OwnerAcceptanceWriteResult = record_owner_acceptance_event(
                        store=record_store,
                        repository_evidence_provider=dependencies.repository_evidence_provider,
                        target=envelope.target,
                        identity=identity,
                        action=envelope.action,
                        expected_binding_sha256=envelope.expected_binding_sha256,
                        source_event_kind="browser_api",
                        source_event_id=idempotency_key,
                        reason=envelope.reason,
                        resolution=envelope.resolution,
                        before_write=project_conservative_check,
                    )
                except Exception as event_error:
                    if prewrite_projection_target is not None and prewrite_event_record is not None:
                        persistence_outcome = _owner_acceptance_event_persistence_outcome(
                            store=record_store,
                            record=prewrite_event_record,
                        )
                        if persistence_outcome == "absent" and isinstance(
                            event_error,
                            (
                                OwnerAcceptanceSelfReviewDeniedError,
                                OwnerAcceptanceAuthorizationError,
                                OwnerAcceptanceEventConflictError,
                                OwnerAcceptanceTransitionError,
                                OwnerAcceptanceBindingConflictError,
                            ),
                        ):
                            try:
                                projection_service.reconcile_locked(
                                    store=record_store,
                                    target=envelope.target,
                                    lock_target=locked_projection_target,
                                    source_event_id=idempotency_key,
                                )
                            except OwnerAcceptanceProjectionReconciliationError as recovery_error:
                                raise OwnerAcceptanceEventNotPersistedProjectionError(
                                    str(recovery_error)
                                ) from event_error
                            raise
                        try:
                            if persistence_outcome == "persisted":
                                projection_service.restore_reconciliation_required_locked(
                                    store=record_store,
                                    target=envelope.target,
                                    lock_target=locked_projection_target,
                                    exact_target=prewrite_projection_target,
                                    source_event_id=idempotency_key,
                                )
                            else:
                                projection_service.restore_event_write_failure_locked(
                                    store=record_store,
                                    target=envelope.target,
                                    lock_target=locked_projection_target,
                                    exact_target=prewrite_projection_target,
                                    source_event_id=idempotency_key,
                                    persistence_outcome=persistence_outcome,
                                )
                        except Exception as restoration_error:
                            combined_error = ExceptionGroup(
                                "Owner acceptance event handling and GitHub recovery both failed.",
                                [event_error, restoration_error],
                            )
                            if persistence_outcome == "persisted":
                                raise OwnerAcceptanceProjectionReconciliationRequiredError(
                                    str(restoration_error)
                                ) from combined_error
                            if persistence_outcome == "absent":
                                raise OwnerAcceptanceEventNotPersistedProjectionError(
                                    str(restoration_error)
                                ) from combined_error
                            raise OwnerAcceptanceEventPersistenceUnknownError(
                                str(restoration_error)
                            ) from combined_error
                        if persistence_outcome == "persisted":
                            raise OwnerAcceptanceProjectionReconciliationRequiredError(
                                str(event_error)
                            ) from event_error
                        if persistence_outcome == "absent":
                            raise OwnerAcceptanceEventNotPersistedError(
                                str(event_error)
                            ) from event_error
                        raise OwnerAcceptanceEventPersistenceUnknownError(
                            str(event_error)
                        ) from event_error
                    raise
                try:
                    projection_outcome = projection_service.reconcile_locked(
                        store=record_store,
                        target=envelope.target,
                        lock_target=locked_projection_target,
                        source_event_id=idempotency_key,
                    )
                    final_decision = projection_outcome.decision
                except OwnerAcceptanceProjectionReconciliationError as error:
                    raise OwnerAcceptanceProjectionReconciliationRequiredError(
                        str(error)
                    ) from error
        except OwnerAcceptanceProjectionUnavailableError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_projection_unavailable",
                message=(
                    "Owner acceptance GitHub status could not be made fail-closed; "
                    "no event was persisted."
                ),
            ) from error
        except OwnerAcceptanceProjectionReconciliationRequiredError as error:
            logger.error(
                "Owner acceptance event persisted but final GitHub projection failed.",
                exc_info=True,
            )
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_projection_reconciliation_required",
                message=(
                    "Owner acceptance event was persisted, but the final GitHub status "
                    "projection requires reconciliation. Retry with the same Idempotency-Key "
                    + (
                        "or use the Owner acceptance projection endpoint."
                        if broad_read_authorized
                        else "only."
                    )
                ),
            ) from error
        except OwnerAcceptanceEventNotPersistedError as error:
            logger.error(
                "Owner acceptance event write failed without persistence.",
                exc_info=True,
            )
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_event_write_failed",
                message=(
                    "Owner acceptance event was not persisted. GitHub shows the failed "
                    "update; retry with the same Idempotency-Key."
                ),
            ) from error
        except OwnerAcceptanceEventNotPersistedProjectionError as error:
            logger.error(
                "Owner acceptance event was not persisted and GitHub recovery failed.",
                exc_info=True,
            )
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_event_write_failed_projection_unknown",
                message=(
                    "Owner acceptance event was not persisted, but Launchplane could not "
                    "confirm the restored GitHub projection. Reconcile Owner acceptance "
                    "before retrying with the same Idempotency-Key."
                ),
            ) from error
        except OwnerAcceptanceEventPersistenceUnknownError as error:
            logger.error(
                "Owner acceptance event persistence outcome is unknown.",
                exc_info=True,
            )
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_event_write_outcome_unknown",
                message=(
                    "Launchplane could not determine whether the Owner acceptance event was "
                    "persisted. Reconcile the Owner acceptance state before retrying with the "
                    "same Idempotency-Key."
                ),
            ) from error
        except OwnerAcceptanceSelfReviewDeniedError as error:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="owner_acceptance_self_review_denied",
                message=bounded_message(error, "This Owner decision is not permitted."),
            ) from error
        except OwnerAcceptanceAuthorizationError as error:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="owner_acceptance_authorization_denied",
                message=bounded_message(error, "This Owner decision is not permitted."),
            ) from error
        except OwnerAcceptanceEventConflictError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="owner_acceptance_event_conflict",
                message=bounded_message(
                    error,
                    "This Owner decision conflicts with an existing event.",
                ),
            ) from error
        except OwnerAcceptanceTransitionError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="owner_acceptance_transition_invalid",
                message=bounded_message(
                    error,
                    "This Owner decision transition is invalid.",
                ),
            ) from error
        except OwnerAcceptanceBindingConflictError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="owner_acceptance_binding_changed",
                message=bounded_message(
                    error,
                    "The reviewed Owner acceptance binding changed.",
                ),
            ) from error
        except (OwnerAcceptanceEvaluationUnavailableError, ValueError):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_evidence_unavailable",
                message="Owner acceptance evidence is unavailable.",
            ) from None
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=bounded_message(error, "Owner acceptance storage is unavailable."),
            ) from error
        if not broad_read_authorized:
            return OwnerAcceptanceEventReceipt(
                trace_id=trace_id,
                write_status=result.status,
            )
        return OwnerAcceptanceEventResponse(
            trace_id=trace_id,
            write_status=result.status,
            record=result.record,
            semantics=_event_semantics(result.record),
            decision=final_decision,
        )

    def read_event(
        event_id: Annotated[str, Path(min_length=1, max_length=128, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceEventReadResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read Owner acceptance events.",
            )
        try:
            record = require_owner_acceptance_event_store(
                record_store
            ).read_owner_acceptance_event_record(event_id)
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message="Owner acceptance event was not found.",
            ) from error
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return OwnerAcceptanceEventReadResponse(
            trace_id=trace_id,
            record=record,
            semantics=_event_semantics(record),
        )

    def read_queue(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        repository: Annotated[str, Query(max_length=256)] = "",
        status: Annotated[str, Query(max_length=64)] = "",
    ) -> OwnerAcceptanceQueueResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read Owner acceptance queue.",
            )
        try:
            result = build_owner_acceptance_queue(
                store=record_store,
                repository=repository,
                status=status,
            )
        except (TypeError, ValueError) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code=(
                    "database_storage_required"
                    if isinstance(error, TypeError)
                    else "owner_acceptance_history_unavailable"
                ),
                message=str(error),
            ) from error
        generated_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        return OwnerAcceptanceQueueResponse(
            trace_id=trace_id,
            generated_at=generated_at,
            total=result.total,
            candidate=result.candidate,
            truncated=result.truncated,
            has_more=result.has_more,
            entry_count=len(result.entries),
            entries=result.entries,
        )

    def read_current_items(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
    ) -> OwnerAcceptanceCurrentItemsResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read current Owner acceptance items.",
            )
        provider = dependencies.repository_evidence_provider
        if not callable(getattr(provider, "list_open_pull_requests", None)) or not callable(
            getattr(provider, "resolve_current_item", None)
        ):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_current_items_unavailable",
                message="Current Owner acceptance item discovery is unavailable.",
            )
        try:
            result = build_owner_acceptance_current_items(
                store=require_change_impact_policy_read_store(record_store),
                repository_evidence_provider=cast(OwnerAcceptanceCurrentItemsProvider, provider),
                limit=limit,
            )
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        generated_at = (
            datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        return OwnerAcceptanceCurrentItemsResponse(
            trace_id=trace_id,
            generated_at=generated_at,
            viewer_capabilities=viewer_capabilities(
                identity=identity,
                record_store=record_store,
                decisions=tuple(
                    item.decision for item in result.items if item.decision is not None
                ),
            ),
            repository_count=result.repository_count,
            repository_failure_count=result.repository_failure_count,
            candidate_count=result.candidate_count,
            evaluated_count=result.evaluated_count,
            unavailable_count=result.unavailable_count,
            truncated=result.truncated,
            items=result.items,
            repository_failures=result.repository_failures,
        )

    def project(
        request: OwnerAcceptanceProjectionRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(projection_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> OwnerAcceptanceProjectionResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_PROJECT_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot project Owner acceptance decisions.",
            )
        try:
            projection_outcome = projection_service.reconcile(
                store=record_store,
                target=request.target,
                source_event_id="manual-projection-reconciliation",
            )
        except (
            OwnerAcceptanceEvaluationUnavailableError,
            OwnerAcceptanceProjectionReconciliationError,
            TypeError,
            ValueError,
        ) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="owner_acceptance_projection_unavailable",
                message=str(error),
            ) from error
        if projection_outcome.result is None:
            raise RuntimeError("Owner acceptance projection did not produce a GitHub result.")
        return OwnerAcceptanceProjectionResponse(
            trace_id=trace_id,
            decision=projection_outcome.decision,
            result=projection_outcome.result,
        )

    errors = {
        400: {"model": common.error_response_model},
        401: {"model": common.error_response_model},
        403: {"model": common.error_response_model},
        404: {"model": common.error_response_model},
        409: {"model": common.error_response_model},
        503: {"model": common.error_response_model},
    }
    app.add_api_route(
        OWNER_ACCEPTANCE_EVALUATION_ROUTE,
        evaluate,
        methods=["GET"],
        response_model=OwnerAcceptanceEvaluationResponse,
        tags=["owner-acceptance"],
        operation_id="evaluate_owner_acceptance",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_OWNER_EVALUATION_ROUTE,
        evaluate_for_owner,
        methods=["GET"],
        response_model=OwnerAcceptanceOwnerEvaluationResponse,
        tags=["owner-acceptance"],
        operation_id="evaluate_owner_product_review",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_CURRENT_ITEMS_ROUTE,
        read_current_items,
        methods=["GET"],
        response_model=OwnerAcceptanceCurrentItemsResponse,
        tags=["owner-acceptance"],
        operation_id="list_owner_acceptance_current_items",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_PROJECT_ROUTE,
        project,
        methods=["POST"],
        response_model=OwnerAcceptanceProjectionResponse,
        operation_id="project_owner_acceptance_decision",
        tags=["owner-acceptance"],
        responses={
            status: {"model": common.error_response_model} for status in (400, 401, 403, 503)
        },
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_EVENTS_ROUTE,
        write_event,
        methods=["POST"],
        response_model=OwnerAcceptanceEventWriteResponse,
        status_code=202,
        tags=["owner-acceptance"],
        operation_id="write_owner_acceptance_event",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_EVENT_ROUTE,
        read_event,
        methods=["GET"],
        response_model=OwnerAcceptanceEventReadResponse,
        tags=["owner-acceptance"],
        operation_id="read_owner_acceptance_event",
        responses=errors,
    )
    app.add_api_route(
        OWNER_ACCEPTANCE_QUEUE_ROUTE,
        read_queue,
        methods=["GET"],
        response_model=OwnerAcceptanceQueueResponse,
        tags=["owner-acceptance"],
        operation_id="list_owner_acceptance_queue",
        responses=errors,
    )
