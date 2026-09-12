from __future__ import annotations

from datetime import datetime, timezone
import unittest
from typing import cast
from unittest.mock import patch

from control_plane.contracts.merge_train_policy import (
    ProviderDeliveryProtectionExpectationV1,
    ProviderRequiredStatusCheckExpectationV1,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.provider_delivery_inspection import (
    ProviderDeliveryInspectionFactsV1,
    ProviderDeliveryInspectionResultV1,
)
from control_plane.contracts.provider_delivery_readiness import (
    ProviderDeliveryInspectionAttemptV1,
    ProviderDeliveryInspectionBindingV1,
    ProviderDeliveryInspectionReservationV1,
    ProviderDeliveryReadinessDecision,
    ProviderDeliveryReadinessReason,
    ProviderDeliveryReadinessReceiptV1,
)
from control_plane.github_app_identity import GitHubAppIdentity, GitHubAppInstallationToken
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.provider_delivery_inspection_github import (
    ProviderDeliveryInspectionCapabilityError,
)
from control_plane.provider_delivery_inspection_profile import (
    ResolvedProviderDeliveryInspectionProfile,
)
from control_plane.provider_delivery_readiness import (
    ensure_provider_delivery_readiness_for_job,
)


class _Store:
    def __init__(
        self,
        *,
        attempt: ProviderDeliveryInspectionAttemptV1,
        expectation: ProviderDeliveryProtectionExpectationV1,
        result: ProviderDeliveryInspectionResultV1,
    ) -> None:
        self.attempt = attempt
        self.expectation = expectation
        self.result = result
        self.events: list[str] = []
        self.finish_kwargs: dict[str, object] = {}

    def precheck_provider_delivery_inspection_for_job(self, *, request_id: str) -> None:
        self.events.append(f"precheck:{request_id}")

    def reserve_provider_delivery_inspection_for_job(self, **_: object) -> object:
        self.events.append("reserve")
        return ProviderDeliveryInspectionReservationV1(
            attempt=self.attempt,
            expectation=self.expectation,
        )

    def mark_provider_delivery_inspection_minting(self, **kwargs: object) -> object:
        self.events.append("minting")
        self.attempt = self.attempt.model_copy(
            update={
                "revision": self.attempt.revision + 1,
                "custody_phase": "minting",
                "inspection_installation_id": kwargs["installation_id"],
            }
        )
        return self.attempt

    def mark_provider_delivery_inspection_issued(self, **kwargs: object) -> object:
        self.events.append("issued")
        self.attempt = self.attempt.model_copy(
            update={
                "revision": self.attempt.revision + 1,
                "custody_phase": "issued",
                "token_expires_at": kwargs["token_expires_at"],
            }
        )
        return self.attempt

    def close_provider_delivery_inspection_custody(self, **_: object) -> object:
        self.events.append("cleanup")
        self.attempt = self.attempt.model_copy(
            update={
                "revision": self.attempt.revision + 1,
                "custody_phase": "closed",
            }
        )
        return self.attempt

    def close_provider_delivery_inspection_without_token(self, **_: object) -> object:
        self.events.append("closed_without_token")
        self.attempt = self.attempt.model_copy(
            update={
                "revision": self.attempt.revision + 1,
                "custody_phase": "closed",
            }
        )
        return self.attempt

    def mark_provider_delivery_inspection_issue_unknown(self, **_: object) -> object:
        raise AssertionError("issue-unknown was not expected")

    def finish_provider_delivery_inspection(self, **kwargs: object) -> object:
        self.events.append("finish")
        self.finish_kwargs = kwargs
        if kwargs.get("result") is None:
            reason = cast(ProviderDeliveryReadinessReason, kwargs["capability_reason"])
            retry_not_before = cast(int | None, kwargs.get("retry_not_before"))
            return ProviderDeliveryReadinessDecision(
                status="capability_unavailable",
                reason_code=reason,
                server_observed_at=100,
                retry_not_before=retry_not_before or 115,
            )
        assert self.result.facts is not None
        receipt = ProviderDeliveryReadinessReceiptV1(
            receipt_id="provider-readiness-" + "c" * 64,
            attempt_id=self.attempt.attempt_id,
            demand_id=self.attempt.demand_id,
            generation=self.attempt.generation,
            action_ordinal=self.attempt.action_ordinal,
            inspection_installation_id=701,
            binding=self.attempt.binding,
            facts=self.result.facts,
            reason_codes=("provider_protection_ready",),
            provider_request_count=self.result.provider_request_count,
            repository_completion_sequence=1,
            observed_at=100,
            expires_at=400,
            completed_at=101,
        )
        return ProviderDeliveryReadinessDecision(
            status="ready",
            reason_code="provider_protection_ready",
            server_observed_at=101,
            receipt=receipt,
        )


class ProviderDeliveryReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expectation = ProviderDeliveryProtectionExpectationV1(
            required_status_checks=(
                ProviderRequiredStatusCheckExpectationV1(context="ci", app_id=9001),
            ),
            strict_required_status_checks_policy=True,
            code_scanning_tools=(),
            pull_request=None,
            allowed_merge_methods=("merge",),
        )
        self.binding = ProviderDeliveryInspectionBindingV1(
            target=OrdinaryAgentTarget(
                repository_id=123,
                repository="example/repo",
                base_branch="main",
            ),
            repository_owner_id=456,
            inventory_record_id="inventory-r1",
            inventory_revision=1,
            inventory_sha256="1" * 64,
            installed_activation_sha256="2" * 64,
            ordinary_delivery_app_id=42,
            ordinary_delivery_installation_id=43,
            merge_policy_record_id="policy-r1",
            merge_policy_sha256="3" * 64,
            merge_policy_semantics_sha256="4" * 64,
            expectation_sha256=canonical_json_sha256(self.expectation.model_dump(mode="json")),
            inspection_profile_id="provider-delivery-inspection-v1",
            inspection_profile_sha256="6" * 64,
            inspection_app_id=700,
            inspection_secret_id="secret",
            inspection_secret_binding_id="binding",
            inspection_secret_version_id="version",
            permission_sha256="7" * 64,
        )
        self.attempt = ProviderDeliveryInspectionAttemptV1(
            attempt_id="provider-inspection-" + "a" * 64,
            demand_id="request-one",
            client_intent_sha256="b" * 64,
            principal_id="agent_one",
            session_id="session-one",
            lease_id="lease-one",
            generation=1,
            provider_attempt_ordinal=1,
            action_ordinal=1,
            binding=self.binding,
            inspection_started_at=100,
            observation_anchor=100,
            dispatch_deadline=145,
            publication_deadline=160,
        )
        self.facts = ProviderDeliveryInspectionFactsV1(
            repository_id=123,
            repository_owner_id=456,
            repository="example/repo",
            base_branch="main",
            ordinary_delivery_app_id=42,
            applicable_ruleset_ids=(10,),
            update_ruleset_id=10,
            classic_protection_present=False,
            effective_protection=self.expectation,
            raw_observation_sha256="8" * 64,
            provider_request_count=7,
        )
        self.result = ProviderDeliveryInspectionResultV1(
            status="ready",
            reason_codes=("provider_protection_ready",),
            facts=self.facts,
            raw_observation_sha256="8" * 64,
            provider_request_count=7,
        )
        self.profile = ResolvedProviderDeliveryInspectionProfile(
            identity=GitHubAppIdentity(app_id=700, private_key="private"),
            profile_id="provider-delivery-inspection-v1",
            profile_sha256="6" * 64,
            app_id=700,
            secret_id="secret",
            secret_binding_id="binding",
            secret_version_id="version",
            permissions=("administration:write", "contents:read", "metadata:read"),
        )

    def test_ready_inspection_persists_custody_callbacks_and_owner_binding(self) -> None:
        store = _Store(attempt=self.attempt, expectation=self.expectation, result=self.result)

        def inspect(**kwargs: object) -> object:
            self.assertEqual(kwargs["repository_owner_id"], 456)
            self.assertEqual(kwargs["ordinary_delivery_app_id"], 42)
            kwargs["before_token_mint"](700, 701)  # type: ignore[operator]
            kwargs["token_issued"](  # type: ignore[operator]
                GitHubAppInstallationToken(
                    token="token",
                    app_id=700,
                    installation_id=701,
                    repository_id=123,
                    repository="example/repo",
                    expires_at=datetime.fromtimestamp(500, timezone.utc).isoformat(),
                )
            )
            kwargs["token_cleanup"]("confirmed_revoked")  # type: ignore[operator]
            return self.result

        with patch(
            "control_plane.provider_delivery_readiness."
            "resolve_provider_delivery_inspection_profile",
            return_value=self.profile,
        ):
            receipt = ensure_provider_delivery_readiness_for_job(
                store=store,  # type: ignore[arg-type]
                request_id="request-one",
                inspect=inspect,
                wall_time=lambda: 101,
            )

        self.assertEqual(receipt.facts.repository_owner_id, 456)
        self.assertEqual(
            store.events,
            ["precheck:request-one", "reserve", "minting", "issued", "cleanup", "finish"],
        )
        self.assertIs(store.finish_kwargs["result"], self.result)

    def test_provider_wait_closes_pre_mint_custody_and_preserves_retry_deadline(self) -> None:
        store = _Store(attempt=self.attempt, expectation=self.expectation, result=self.result)

        def inspect(**_: object) -> object:
            raise ProviderDeliveryInspectionCapabilityError("provider_wait", retry_not_before=177)

        with (
            patch(
                "control_plane.provider_delivery_readiness."
                "resolve_provider_delivery_inspection_profile",
                return_value=self.profile,
            ),
            self.assertRaises(OrdinaryAgentSessionAdmissionDenied) as caught,
        ):
            ensure_provider_delivery_readiness_for_job(
                store=store,  # type: ignore[arg-type]
                request_id="request-one",
                inspect=inspect,
                wall_time=lambda: 101,
            )

        self.assertEqual(caught.exception.reason_code, "provider_wait")
        self.assertEqual(caught.exception.retry_not_before, 177)
        self.assertEqual(
            store.events,
            ["precheck:request-one", "reserve", "closed_without_token", "finish"],
        )
        self.assertEqual(store.finish_kwargs["retry_not_before"], 177)


if __name__ == "__main__":
    unittest.main()
