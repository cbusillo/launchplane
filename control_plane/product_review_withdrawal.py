"""Withdraw a pending request, preserving Client decisions and release authority."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.storage.postgres import PostgresRecordStore

WITHDRAWAL_ROUTE = "/v1/product-review/withdrawal"
WITHDRAWAL_ACTION = "product_review.withdraw"


class ReviewWithdrawalRefused(ValueError):
    pass


class ReviewWithdrawalUnsettled(ReviewWithdrawalRefused):
    """The provider effect is not yet settled; retain the original operation key."""


class ReviewWithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=128)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pull_request: int = Field(ge=1)
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    reason: str = Field(min_length=1, max_length=1000)
    # A citation is an operator attestation, not a Client decision or a grant.
    director_decision_url: str = Field(
        pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*#issuecomment-[1-9][0-9]*$"
    )
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_request(self) -> "ReviewWithdrawalRequest":
        self.product = self.product.strip()
        self.reason = self.reason.strip()
        if not self.product or not self.reason:
            raise ValueError("Withdrawal needs a product and a recorded reason.")
        if self.mode != "dry-run" and not re.fullmatch(r"[a-f0-9]{64}", self.reviewed_plan_sha256):
            raise ValueError("Apply and readback need the reviewed withdrawal plan digest.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Dry-run cannot carry a reviewed plan digest.")
        return self


class ReviewWithdrawalSource(Protocol):
    def change(self) -> tuple[str, frozenset[str]]: ...
    def history(self, marker: str, body: str = "") -> str: ...
    def remove_label(self, label: str) -> None: ...


@dataclass
class ReviewWithdrawalService:
    store: PostgresRecordStore
    source: ReviewWithdrawalSource
    profile: LaunchplaneProductProfileRecord
    actor: str

    def _authority_digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "product": self.profile.product,
                    "repository": self.profile.repository.casefold(),
                    "owner": self.profile.owner.model_dump(mode="json"),
                    "active": self.profile.is_active,
                    "release_on_acceptance": self.profile.release_on_acceptance,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _plan(self, request: ReviewWithdrawalRequest) -> dict[str, object]:
        return {
            **request.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"}),
            "actor": self.actor,
            "review_label": self.profile.owner.review_label,
            # Bind review authority, so rerouting cannot use a stale Client or label.
            "profile_sha256": self._authority_digest(),
        }

    def _pending(self, request: ReviewWithdrawalRequest, *, require_label: bool = True) -> None:
        current = self.store.read_product_profile_record(request.product)
        if current != self.profile or not current.is_active or not current.owner.is_set:
            raise ReviewWithdrawalRefused("The product or its Client changed. Review again.")
        if current.repository.casefold() != request.repository.casefold():
            raise ReviewWithdrawalRefused("The product does not own this repository.")
        head, labels = self.source.change()
        if head != request.expected_head_sha:
            raise ReviewWithdrawalRefused("The PR head changed. Review again.")
        if any(
            decision.product == current.product and decision.head_sha == head
            for decision in self.store.list_product_review_decision_records(
                repository=current.repository, pull_request_number=request.pull_request
            )
        ):
            raise ReviewWithdrawalRefused("The Client has already decided on this head.")
        if require_label and current.owner.review_label.casefold() not in {
            label.casefold() for label in labels
        }:
            raise ReviewWithdrawalRefused("This PR has no pending labeled Client review request.")

    def run(self, request: ReviewWithdrawalRequest, *, key: str, trace: str) -> dict[str, object]:
        plan = self._plan(request)
        encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        scope = "product-review-withdrawal:" + self.actor
        fingerprint = hashlib.sha256((encoded + request.reviewed_plan_sha256).encode()).hexdigest()
        if request.mode != "dry-run" and (not key.strip() or len(key) > 200):
            raise ReviewWithdrawalRefused("Apply and readback require a bounded Idempotency-Key.")
        with self.store.product_review_lock(
            repository=self.profile.repository, pull_request_number=request.pull_request
        ):
            original = (
                self.store.read_idempotency_record(
                    scope=scope, route_path=WITHDRAWAL_ROUTE, idempotency_key=key
                )
                if key
                else None
            )
            if original is not None:
                saved_plan = self.store.read_original_mutation_intent(original)
                submitted = request.model_dump(
                    mode="json", exclude={"mode", "reviewed_plan_sha256"}
                )
                if (
                    any(saved_plan.get(name) != value for name, value in submitted.items())
                    or saved_plan.get("actor") != self.actor
                ):
                    raise ReviewWithdrawalRefused("The key belongs to another withdrawal request.")
                plan = saved_plan
                encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                fingerprint = hashlib.sha256(
                    (encoded + request.reviewed_plan_sha256).encode()
                ).hexdigest()
                if original.request_fingerprint != fingerprint:
                    raise ReviewWithdrawalRefused("The key belongs to another withdrawal request.")
            if request.mode == "readback":
                if original is None:
                    raise ReviewWithdrawalRefused("No saved withdrawal exists for this key.")
                head, labels = self.source.change()
                saved = cast(dict[str, object], original.response_payload.get("result", {}))
                history_url = str(saved.get("history_url", ""))
                # This is observation only. A later request is never removed on readback.
                return {
                    **saved,
                    "applied": original.state == "completed" and saved.get("applied") is True,
                    "head_sha": request.expected_head_sha,
                    "plan_sha256": digest,
                    "readback_matches": original.state == "completed"
                    and saved.get("applied") is True
                    and head == request.expected_head_sha
                    and str(plan["review_label"]).casefold()
                    not in {label.casefold() for label in labels}
                    and bool(history_url)
                    and self.source.history(str(saved.get("history_marker", ""))) == history_url,
                }
            if original is not None and original.state == "completed":
                return cast(dict[str, object], original.response_payload["result"])
            if original is None:
                self._pending(request)
            if request.mode == "dry-run":
                return {
                    "applied": False,
                    "plan_sha256": digest,
                    "head_sha": request.expected_head_sha,
                }
            if request.reviewed_plan_sha256 != digest:
                raise ReviewWithdrawalRefused("The reviewed withdrawal plan changed. Review again.")
            adapter = _WithdrawalAdapter(
                self,
                request,
                encoded,
                digest,
                trace,
                recovering=original is not None and bool(original.provider_effect_phase),
            )
            result = run_durable_provider_operation(
                store=self.store,
                scope=scope,
                route_path=WITHDRAWAL_ROUTE,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                lease_owner=trace,
                response_trace_id=trace,
                adapter=adapter,
                # An uncertain provider effect is observed, never blindly repeated.
                allow_mutation=True,
                original_intent=plan,
            )
            if result.status not in {"completed", "replayed", "adopted"}:
                raise ReviewWithdrawalUnsettled(
                    "Withdrawal outcome is unsettled. Preserve the original key and read back."
                )
            return cast(dict[str, object], result.response_payload["result"])


@dataclass
class _WithdrawalAdapter:
    service: ReviewWithdrawalService
    request: ReviewWithdrawalRequest
    encoded_plan: str
    digest: str
    trace: str
    recovering: bool = False

    def target_key(self) -> str:
        return f"product-review-withdrawal:{self.request.repository.casefold()}#{self.request.pull_request}"

    def reconciliation_key(self) -> str:
        return self.encoded_plan

    def _marker(self, operation_key: str) -> str:
        return (
            "<!-- launchplane:review-withdrawal:"
            + hashlib.sha256(operation_key.encode()).hexdigest()
            + " -->"
        )

    def _result(self, marker: str, url: str) -> dict[str, object]:
        return {
            "result": {
                "applied": True,
                "plan_sha256": self.digest,
                "head_sha": self.request.expected_head_sha,
                "history_url": url,
                "history_marker": marker,
            }
        }

    def observe(
        self, provider_operation_key: str, provider_effect_phase: str, reconciliation_key: str
    ) -> ProviderObservation:
        head, labels = self.service.source.change()
        marker = self._marker(provider_operation_key)
        url = self.service.source.history(marker)
        if url:
            # Published only after confirmed removal: a later request cannot undo
            # the historical receipt, and is never removed by its recovery.
            return ProviderObservation("present", response_payload=self._result(marker, url))
        authority = json.loads(self.encoded_plan)
        has_label = str(authority["review_label"]).casefold() in {
            label.casefold() for label in labels
        }
        decided = any(
            decision.product == self.request.product
            and decision.head_sha == self.request.expected_head_sha
            for decision in self.service.store.list_product_review_decision_records(
                repository=self.service.profile.repository,
                pull_request_number=self.request.pull_request,
            )
        )
        if (
            head == self.request.expected_head_sha
            and authority["profile_sha256"] == self.service._authority_digest()
            and not decided
        ):
            if not provider_effect_phase and has_label:
                # No mutation was dispatched. Resume with the same reviewed request.
                return ProviderObservation("absent", retry_safe=True)
            if provider_effect_phase and not has_label:
                # Only finish history; never issue another label removal.
                return ProviderObservation(
                    "absent", retry_safe=True, preserve_effect_checkpoint=True
                )
        # The current labeled request may be newer, or its authority/head changed.
        # Settle this original intent without claiming success or absence of prior
        # effects. A fresh reviewed plan and new key can then address the request.
        return ProviderObservation(
            "present",
            response_payload={
                "result": {
                    "applied": False,
                    "replan_required": True,
                    "prior_effects_unknown": bool(provider_effect_phase),
                    "plan_sha256": self.digest,
                    "head_sha": self.request.expected_head_sha,
                }
            },
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            self.service._pending(self.request, require_label=not self.recovering)
            if self.recovering:
                _, labels = self.service.source.change()
                if self.service.profile.owner.review_label.casefold() in {
                    label.casefold() for label in labels
                }:
                    raise ReviewWithdrawalRefused(
                        "A later review request may exist. Recovery cannot remove its label."
                    )
        except ReviewWithdrawalRefused as error:
            raise ProviderMutationRejectedError(error) from error
        try:
            return self._apply_effect(provider_operation_key, lease)
        except Exception as error:
            raise ProviderMutationUnknownError("Withdrawal delivery is unsettled.") from error

    def _apply_effect(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        if not self.recovering:
            lease.checkpoint_effect("withdraw-review-request")
            self.service.source.remove_label(self.service.profile.owner.review_label)
        else:
            lease.assert_current()
        head, labels = self.service.source.change()
        if (
            head != self.request.expected_head_sha
            or self.service.profile.owner.review_label.casefold()
            in {label.casefold() for label in labels}
        ):
            raise ReviewWithdrawalRefused(
                "Review-label removal was not confirmed on the reviewed head."
            )
        marker = self._marker(provider_operation_key)
        # Quote prose so it cannot forge a Client feedback marker or ping a person.
        quoted_reason = "\n".join(
            "> " + line.replace("@", "@\u200b") for line in self.request.reason.splitlines()
        )
        body = "\n".join(
            (
                marker,
                "## Pending Client review request withdrawn",
                f"Head: `{self.request.expected_head_sha}`",
                f"Recorded Director decision: {self.request.director_decision_url}",
                f"Requested by: `{self.service.actor}`",
                "",
                quoted_reason,
                "",
                "This withdraws a pending request only. It records no Client acceptance or rejection. Later review requests and release acceptance remain required under their normal rules.",
            )
        )
        url = self.service.source.history(marker, body)
        if not url:
            raise ReviewWithdrawalRefused(
                "Withdrawal history delivery is unconfirmed. Preserve the original key."
            )
        return ProviderMutationOutcome(202, self._result(marker, url))
