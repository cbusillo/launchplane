"""A testing address belongs to its lane, independently of disposable copies."""

from datetime import datetime
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.prepared_public_site import FrozenRecord
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity

REBUILD_SCOPE = "launchplane:testing-native-rebuild:v1"
REBUILD_ROUTE = "worker:testing-native-rebuild"


class TestingCopy(FrozenRecord):
    copy_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    private_url: str = Field(min_length=1)
    resource_binding: str = Field(min_length=1)
    runtime_identity: RuntimeIdentity


class TestingRebuildPlan(FrozenRecord):
    operation_id: str = Field(min_length=1)
    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    base_url: str = Field(min_length=1)
    route_id: str = Field(min_length=1)
    authority_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: Literal["empty", "production", "upstream"]
    source_reference: str = ""
    old: TestingCopy
    new: TestingCopy

    @model_validator(mode="after")
    def validate_lane(self) -> Self:
        origin = urlsplit(self.base_url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
        ):
            raise ValueError("Testing requires its existing HTTPS lane address.")
        if (self.source == "empty") == bool(self.source_reference):
            raise ValueError("A restore requires its managed source reference; empty does not.")
        for copy in (self.old, self.new):
            identity = copy.runtime_identity
            # Older managed copies did not install a product field or digest.
            # Keep their observed identity intact; the caller still proves lane
            # ownership from managed records. A new copy must be fully explicit.
            allowed_products = {self.product, ""} if copy is self.old else {self.product}
            if identity.product not in allowed_products or (
                identity.context,
                identity.instance,
            ) != (self.context, self.instance):
                raise ValueError("Both copies must belong to the same testing lane.")
        for field in ("copy_id", "target_id", "private_url", "resource_binding"):
            if getattr(self.old, field) == getattr(self.new, field):
                raise ValueError("A candidate requires independent target and resources.")
        if (
            self.old.runtime_identity.deployment_record_id
            == self.new.runtime_identity.deployment_record_id
        ):
            raise ValueError("A candidate needs its own deployment identity.")
        if not self.new.runtime_identity.image_digest:
            raise ValueError("The candidate must bind its immutable build digest.")
        return self

    @property
    def host(self) -> str:
        return str(urlsplit(self.base_url).hostname)

    @property
    def digest(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class TestingCopyReceipt(FrozenRecord):
    plan_digest: str
    copy_id: str
    evidence_id: str = Field(min_length=1)
    independent_resources_verified: Literal[True]
    local_credentials_verified: Literal[True]
    live_connections_removed_before_start: Literal[True]


class TestingCopyReadiness(FrozenRecord):
    copy_id: str
    host: str
    observed_at: datetime
    runtime_identity: RuntimeIdentity
    healthy: Literal[True]
    evidence_id: str = Field(min_length=1)


class TestingRouteObservation(FrozenRecord):
    route_id: str
    host: str
    target_url: str
    slot_id: str
    applied: Literal[True]
    authoritative_router_count: Literal[1]
    observed_at: datetime
    public_health: HealthcheckEvidence | None = None


TestingRebuildStep = Literal[
    "preparing",
    "prepared",
    "switching",
    "publishing",
    "retiring",
    "complete",
    "rolling_back",
    "failed",
]


class TestingRetirementDisposition(FrozenRecord):
    status: Literal["stopped", "preserved", "outstanding"]
    reason: str = Field(default="", max_length=160, pattern=r"^[a-z0-9_]*$")


class TestingRebuildRecord(FrozenRecord):
    plan: TestingRebuildPlan
    step: TestingRebuildStep = "preparing"
    began_at: datetime
    updated_at: datetime
    ended_at: datetime | None = None
    preparation: TestingCopyReceipt | None = None
    readiness: TestingCopyReadiness | None = None
    route: TestingRouteObservation | None = None
    retirement_started: bool = False
    retirement: TestingRetirementDisposition | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    publication_started: bool = False
    rollback_started: bool = False
    retirement_recovery: TestingCopyReadiness | None = None
    error: Literal[
        "",
        "candidate_public_health_failed",
        "candidate_preparation_failed",
        "candidate_route_not_applied",
        "candidate_private_readiness_failed",
        "testing_rebuild_admission_changed",
    ] = ""
    admission_refusal: str = Field(default="", exclude_if=lambda value: not value)
    preparation_failure_evidence: str = Field(default="", exclude_if=lambda value: not value)
    private_readiness_failed_at: datetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        if self.retirement is not None and (self.step != "complete" or not self.retirement_started):
            raise ValueError("A retirement disposition requires completed forward replacement.")
        if (
            self.step in {"rolling_back", "failed"}
            and self.retirement_started
            and self.retirement_recovery is None
        ):
            raise ValueError("Rollback after a stop intent requires its undispatched-stop proof.")
        if self.retirement_recovery is not None and (
            not self.retirement_started
            or (
                self.retirement_recovery.copy_id,
                self.retirement_recovery.host,
                self.retirement_recovery.runtime_identity,
            )
            != (self.plan.old.copy_id, self.plan.host, self.plan.old.runtime_identity)
        ):
            raise ValueError("Retirement recovery requires proof for this operation's old copy.")
        for stamp in (
            self.began_at,
            self.updated_at,
            self.ended_at,
            self.private_readiness_failed_at,
        ):
            if stamp is not None and stamp.utcoffset() is None:
                raise ValueError("Rebuild checkpoints require timezone-aware timestamps.")
        if self.updated_at < self.began_at or (
            self.ended_at is not None and not self.began_at <= self.ended_at <= self.updated_at
        ):
            raise ValueError("Rebuild checkpoint times are out of order.")
        if (
            self.private_readiness_failed_at is not None
            and not self.began_at <= self.private_readiness_failed_at <= self.updated_at
        ):
            raise ValueError("The private probe failure must belong to this rebuild's lifetime.")
        preparation_failed = self.step == "failed" and self.error == "candidate_preparation_failed"
        private_failed = (
            self.step == "failed" and self.error == "candidate_private_readiness_failed"
        )
        admission_failed = (
            self.step == "failed" and self.error == "testing_rebuild_admission_changed"
        )
        if (
            bool(self.admission_refusal) != admission_failed
            or admission_failed
            and (
                self.publication_started
                or self.retirement_started
                or self.rollback_started
                or self.route is None
                or self.route.slot_id != self.plan.old.copy_id
                or self.preparation is not None
                and (self.preparation.plan_digest, self.preparation.copy_id)
                != (self.plan.digest, self.plan.new.copy_id)
                or self.readiness is not None
                and (self.readiness.copy_id, self.readiness.host, self.readiness.runtime_identity)
                != (self.plan.new.copy_id, self.plan.host, self.plan.new.runtime_identity)
            )
        ):
            raise ValueError(
                "An admission refusal requires the original copy before traffic effects."
            )
        if private_failed and (
            self.private_readiness_failed_at is None
            or self.publication_started
            or self.retirement_started
            or self.rollback_started
            or self.route is not None
            and self.route.slot_id != self.plan.old.copy_id
        ):
            raise ValueError("Private readiness failure requires the original copy before cutover.")
        if bool(self.preparation_failure_evidence) != (preparation_failed or private_failed) or (
            preparation_failed
            and any(
                (
                    self.preparation is not None,
                    self.readiness is not None,
                    self.route is not None,
                    self.publication_started,
                    self.retirement_started,
                    self.rollback_started,
                )
            )
        ):
            raise ValueError(
                "Preparation failure requires retained custody before any traffic effect."
            )
        if (
            not (preparation_failed or private_failed or admission_failed)
            and self.step != "preparing"
            and (
                self.preparation is None
                or (self.preparation.plan_digest, self.preparation.copy_id)
                != (self.plan.digest, self.plan.new.copy_id)
            )
        ):
            raise ValueError("Rebuild progress requires its independent-copy receipt.")
        if (
            not (preparation_failed or private_failed or admission_failed)
            and self.step not in {"preparing", "prepared"}
            and (
                self.readiness is None
                or (self.readiness.copy_id, self.readiness.host, self.readiness.runtime_identity)
                != (self.plan.new.copy_id, self.plan.host, self.plan.new.runtime_identity)
            )
        ):
            raise ValueError("Cutover progress requires the candidate's readiness receipt.")
        if (self.step in {"complete", "failed"}) != (self.ended_at is not None):
            raise ValueError("Only a terminal rebuild has an end time.")
        return self
