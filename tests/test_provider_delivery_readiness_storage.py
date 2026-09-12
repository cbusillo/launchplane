from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_policy import (
    ProviderDeliveryProtectionExpectationV1,
    ProviderRequiredStatusCheckExpectationV1,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.provider_delivery_readiness import (
    ProviderDeliveryInspectionBindingV1,
    ProviderDeliveryInspectionAttemptV1,
    ProviderDeliveryInspectionReservationV1,
    ProviderDeliveryReadinessDecision,
    provider_delivery_binding_is_current,
    provider_delivery_inspection_attempt_id,
)
from control_plane.contracts.provider_delivery_inspection import (
    ProviderDeliveryInspectionFactsV1,
    ProviderDeliveryInspectionResultV1,
)
from control_plane.provider_delivery_inspection_profile import (
    ResolvedProviderDeliveryInspectionProfile,
)
from control_plane.github_app_identity import GitHubAppIdentity, GitHubAppInstallationToken
from control_plane.ordinary_agent_session_lifecycle import (
    _policy_inputs,
    OrdinaryAgentSessionAdmissionDenied,
    require_ordinary_agent_current_job_authority,
)
from control_plane.ordinary_agent_eligibility import evaluate_ordinary_agent_policy
from control_plane.provider_delivery_readiness import ensure_provider_delivery_readiness_for_job
from control_plane.storage.postgres import (
    _OrdinaryAgentCurrentJobContext,
    _OrdinaryAgentRuntimePrerequisites,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneProviderDeliveryInspectionRow,
)
from tests import test_ordinary_agent_effect_storage as effect_support


class ProviderDeliveryReadinessStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.store = fixture.store
        self.session = fixture.fixture
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
            target=fixture.request.target,
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
        self.profile = ResolvedProviderDeliveryInspectionProfile(
            identity=GitHubAppIdentity(app_id=700, private_key="private"),
            profile_id=self.binding.inspection_profile_id,
            profile_sha256=self.binding.inspection_profile_sha256,
            app_id=700,
            secret_id="secret",
            secret_binding_id="binding",
            secret_version_id="version",
            permissions=("administration:write", "contents:read", "metadata:read"),
        )
        self.activation = Mock()
        self.activation.activation_expires_at = datetime.fromtimestamp(
            self.fixture.request.expires_at + 60, timezone.utc
        ).isoformat()
        self.prerequisites = patch.object(
            self.store,
            "_require_ordinary_agent_runtime_prerequisites",
            return_value=self._prerequisites(self.session.now),
        )
        self.binding_resolver = patch.object(
            self.store,
            "_provider_delivery_binding_locked",
            return_value=(self.binding, self.expectation),
        )
        self.prerequisites_mock = self.prerequisites.start()
        self.binding_resolver_mock = self.binding_resolver.start()
        self.addCleanup(self.prerequisites.stop)
        self.addCleanup(self.binding_resolver.stop)

    def _prerequisites(self, observed_at: int) -> _OrdinaryAgentRuntimePrerequisites:
        return _OrdinaryAgentRuntimePrerequisites(
            activation=self.activation,
            evidence_ids=(),
            custody_valid_from=0,
            custody_expires_at=self.fixture.request.expires_at,
            qualification_expires_at=self.fixture.request.expires_at,
            observed_at=observed_at,
        )

    def _actions_used(self) -> int:
        with self.store._session_factory() as db:
            row = db.get(LaunchplaneOrdinaryAgentLeaseRow, self.fixture.request.lease_id)
            assert row is not None
            return int(row.payload["budget"]["actions_used"])

    def _reserve(
        self,
    ) -> ProviderDeliveryInspectionReservationV1 | ProviderDeliveryReadinessDecision:
        return self.store.reserve_provider_delivery_inspection_for_job(
            request_id=self.fixture.request.request_id,
            profile=self.profile,
        )

    def _result(
        self,
        *,
        status: str = "ready",
        owner_id: int = 456,
        expectation: ProviderDeliveryProtectionExpectationV1 | None = None,
    ) -> ProviderDeliveryInspectionResultV1:
        facts = ProviderDeliveryInspectionFactsV1(
            repository_id=self.binding.target.repository_id,
            repository_owner_id=owner_id,
            repository=self.binding.target.repository,
            base_branch=self.binding.target.base_branch,
            ordinary_delivery_app_id=self.binding.ordinary_delivery_app_id,
            applicable_ruleset_ids=(10,),
            update_ruleset_id=10,
            classic_protection_present=False,
            effective_protection=expectation or self.expectation,
            raw_observation_sha256="8" * 64,
            provider_request_count=7,
        )
        return ProviderDeliveryInspectionResultV1(
            status=status,  # type: ignore[arg-type]
            reason_codes=(
                ("provider_protection_ready",)
                if status == "ready"
                else ("required_status_checks_mismatch",)
            ),
            facts=facts,
            raw_observation_sha256="8" * 64,
            provider_request_count=7,
        )

    def _publish_ready(self) -> ProviderDeliveryReadinessDecision:
        reserved = self._reserve()
        assert isinstance(reserved, ProviderDeliveryInspectionReservationV1)
        minting = self.store.mark_provider_delivery_inspection_minting(
            attempt_id=reserved.attempt.attempt_id,
            expected_revision=reserved.attempt.revision,
            app_id=700,
            installation_id=701,
        )
        issued = self.store.mark_provider_delivery_inspection_issued(
            attempt_id=minting.attempt_id,
            expected_revision=minting.revision,
            app_id=700,
            installation_id=701,
            repository_id=self.binding.target.repository_id,
            token_expires_at=self.session.now + 600,
        )
        closed = self.store.close_provider_delivery_inspection_custody(
            attempt_id=issued.attempt_id,
            expected_revision=issued.revision,
            outcome="confirmed_revoked",
        )
        return self.store.finish_provider_delivery_inspection(
            attempt_id=closed.attempt_id,
            expected_revision=closed.revision,
            result=self._result(),
        )

    def test_capability_retry_reuses_generation_action_and_single_charge(self) -> None:
        before = self._actions_used()
        first = self._reserve()
        self.assertIsInstance(first, ProviderDeliveryInspectionReservationV1)
        assert isinstance(first, ProviderDeliveryInspectionReservationV1)
        self.assertEqual(self._actions_used(), before + 1)
        follower = self._reserve()
        self.assertIsInstance(follower, ProviderDeliveryReadinessDecision)
        assert isinstance(follower, ProviderDeliveryReadinessDecision)
        self.assertEqual(
            (follower.status, follower.retry_not_before),
            ("in_progress", first.attempt.publication_deadline),
        )
        closed = self.store.close_provider_delivery_inspection_without_token(
            attempt_id=first.attempt.attempt_id,
            expected_revision=first.attempt.revision,
        )
        deferred = self.store.finish_provider_delivery_inspection(
            attempt_id=closed.attempt_id,
            expected_revision=closed.revision,
            capability_reason="provider_wait",
            retry_not_before=self.session.now + 15,
        )
        self.assertEqual(deferred.retry_not_before, self.session.now + 15)

        cached = self._reserve()
        self.assertIsInstance(cached, ProviderDeliveryReadinessDecision)
        assert isinstance(cached, ProviderDeliveryReadinessDecision)
        self.assertEqual(cached.reason_code, "provider_wait")
        self.assertEqual(self._actions_used(), before + 1)

        self.session.now += 15
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        second = self._reserve()
        self.assertIsInstance(second, ProviderDeliveryInspectionReservationV1)
        assert isinstance(second, ProviderDeliveryInspectionReservationV1)
        self.assertEqual(
            (
                second.attempt.generation,
                second.attempt.provider_attempt_ordinal,
                second.attempt.action_ordinal,
            ),
            (first.attempt.generation, 2, first.attempt.action_ordinal),
        )
        self.assertEqual(self._actions_used(), before + 1)

    def test_repository_flight_shares_same_branch_cache_but_not_cross_branch(self) -> None:
        original_context = self.store._ordinary_agent_current_chain_context
        with self.store._session_factory() as db:
            base = original_context(db, request_id=self.fixture.request.request_id)

        second_main = base.request.model_copy(
            update={
                "request_id": "finite-request-second-main",
                "idempotency_key": "request-second-main",
                "lease_id": "lease-second-main",
            }
        )
        release_target = OrdinaryAgentTarget(
            repository_id=base.request.target.repository_id,
            repository=base.request.target.repository,
            base_branch="release",
        )
        release_request = base.request.model_copy(
            update={
                "request_id": "finite-request-release",
                "idempotency_key": "request-release",
                "principal_id": "agent_release",
                "session_id": "session-release",
                "lease_id": "lease-release",
                "target": release_target,
            }
        )
        second_main_lease = base.lease.model_copy(
            update={
                "lease_id": second_main.lease_id,
                "budget": base.lease.budget.model_copy(update={"actions_used": 0}),
            }
        )
        release_rule = base.policy.policy.ordinary_agents[0].model_copy(
            update={
                "principal_id": release_request.principal_id,
                "target": release_target,
                "managed_rule_id": "agent_release.launchplane.release",
            }
        )
        release_policy = base.policy.model_copy(
            update={
                "policy": base.policy.policy.model_copy(update={"ordinary_agents": (release_rule,)})
            }
        )
        release_binding = base.principal.policy.model_copy(
            update={
                "target": release_target,
                "managed_rule_id": release_rule.managed_rule_id,
            }
        )
        release_principal = base.principal.model_copy(
            update={
                "principal_id": release_request.principal_id,
                "credential_id": "credential_release",
                "policy": release_binding,
            }
        )
        release_credential = base.credential.model_copy(
            update={
                "principal_id": release_request.principal_id,
                "credential_id": release_principal.credential_id,
                "policy": release_binding,
            }
        )
        release_session = base.session.model_copy(
            update={
                "session_id": release_request.session_id,
                "principal_id": release_request.principal_id,
                "credential_id": release_principal.credential_id,
            }
        )
        release_snapshot, release_subject = _policy_inputs(release_policy, release_principal)
        release_decision = evaluate_ordinary_agent_policy(
            snapshot=release_snapshot,
            principal=release_subject,
            target=release_target,
            action=base.lease.action,
            managed_set_id=release_binding.managed_set_id,
            managed_rule_id=release_binding.managed_rule_id,
        )
        self.assertEqual(release_decision.decision, "allow")
        release_lease = base.lease.model_copy(
            update={
                "lease_id": release_request.lease_id,
                "session_id": release_request.session_id,
                "principal_id": release_request.principal_id,
                "target": release_target,
                "managed_rule_id": release_rule.managed_rule_id,
                "effective_decision_fingerprint": release_decision.effective_decision_fingerprint,
                "budget": base.lease.budget.model_copy(update={"actions_used": 0}),
            }
        )
        with self.store._session_factory() as db:
            db.add_all(
                (
                    LaunchplaneOrdinaryAgentLeaseRow(
                        lease_id=second_main_lease.lease_id,
                        session_id=second_main_lease.session_id,
                        revision=second_main_lease.revision,
                        payload=self.store._payload_dict(second_main_lease),
                    ),
                    LaunchplaneOrdinaryAgentLeaseRow(
                        lease_id=release_lease.lease_id,
                        session_id=release_lease.session_id,
                        revision=release_lease.revision,
                        payload=self.store._payload_dict(release_lease),
                    ),
                    LaunchplaneOrdinaryAgentFiniteRequestRow(
                        request_id=second_main.request_id,
                        principal_id=second_main.principal_id,
                        session_id=second_main.session_id,
                        lease_id=second_main.lease_id,
                        idempotency_key=second_main.idempotency_key,
                        intent_sha256="b" * 64,
                        payload=self.store._payload_dict(second_main),
                    ),
                    LaunchplaneOrdinaryAgentFiniteRequestRow(
                        request_id=release_request.request_id,
                        principal_id=release_request.principal_id,
                        session_id=release_request.session_id,
                        lease_id=release_request.lease_id,
                        idempotency_key=release_request.idempotency_key,
                        intent_sha256="c" * 64,
                        payload=self.store._payload_dict(release_request),
                    ),
                )
            )
            db.commit()

        def context_for(db: Any, *, request_id: str) -> _OrdinaryAgentCurrentJobContext:
            session = db
            request_row = session.get(LaunchplaneOrdinaryAgentFiniteRequestRow, request_id)
            assert request_row is not None
            if request_id == base.request.request_id:
                request, lease, principal, credential, session_record, policy = (
                    base.request,
                    base.lease,
                    base.principal,
                    base.credential,
                    base.session,
                    base.policy,
                )
            elif request_id == second_main.request_id:
                request, lease, principal, credential, session_record, policy = (
                    second_main,
                    second_main_lease,
                    base.principal,
                    base.credential,
                    base.session,
                    base.policy,
                )
            else:
                request, lease, principal, credential, session_record, policy = (
                    release_request,
                    release_lease,
                    release_principal,
                    release_credential,
                    release_session,
                    release_policy,
                )
            lease_row = session.get(LaunchplaneOrdinaryAgentLeaseRow, lease.lease_id)
            assert lease_row is not None
            require_ordinary_agent_current_job_authority(
                policy=policy,
                principal=principal,
                credential=credential,
                session=session_record,
                lease=lease,
                request=request,
                now=self.session.now,
            )
            return _OrdinaryAgentCurrentJobContext(
                request,
                request_row,
                session_record,
                lease,
                lease_row,
                policy,
                principal,
                credential,
                self.session.now,
            )

        def actions_used(lease_id: str) -> int:
            with self.store._session_factory() as db:
                row = db.get(LaunchplaneOrdinaryAgentLeaseRow, lease_id)
                assert row is not None
                return int(row.payload["budget"]["actions_used"])

        def binding_for(
            _db: object, *, context: _OrdinaryAgentCurrentJobContext, **_: object
        ) -> tuple[
            ProviderDeliveryInspectionBindingV1,
            ProviderDeliveryProtectionExpectationV1,
        ]:
            return self.binding.model_copy(
                update={"target": context.request.target}
            ), self.expectation

        def provider_result(base_branch: str) -> ProviderDeliveryInspectionResultV1:
            result = self._result()
            assert result.facts is not None
            return result.model_copy(
                update={"facts": result.facts.model_copy(update={"base_branch": base_branch})}
            )

        def complete_inspection(**kwargs: object) -> ProviderDeliveryInspectionResultV1:
            before_token_mint = kwargs["before_token_mint"]
            token_issued = kwargs["token_issued"]
            token_cleanup = kwargs["token_cleanup"]
            assert callable(before_token_mint)
            assert callable(token_issued)
            assert callable(token_cleanup)
            before_token_mint(700, 701)
            token_issued(
                GitHubAppInstallationToken(
                    token="test-inspection-token",
                    app_id=700,
                    installation_id=701,
                    repository_id=self.binding.target.repository_id,
                    repository=self.binding.target.repository,
                    expires_at=datetime.fromtimestamp(
                        self.session.now + 600, timezone.utc
                    ).isoformat(),
                    permissions=("administration:write", "contents:read", "metadata:read"),
                )
            )
            token_cleanup("confirmed_revoked")
            return provider_result(str(kwargs["base_branch"]))

        follower_reasons: list[str] = []

        def main_inspection(**kwargs: object) -> ProviderDeliveryInspectionResultV1:
            for request in (second_main, release_request):
                with self.assertRaises(OrdinaryAgentSessionAdmissionDenied) as raised:
                    ensure_provider_delivery_readiness_for_job(
                        store=self.store,
                        request_id=request.request_id,
                        inspect=lambda **_: (_ for _ in ()).throw(
                            AssertionError("active followers must not inspect")
                        ),
                    )
                follower_reasons.append(raised.exception.reason_code)
            self.assertEqual(
                tuple(
                    actions_used(lease_id)
                    for lease_id in (
                        base.lease.lease_id,
                        second_main.lease_id,
                        release_request.lease_id,
                    )
                ),
                (1, 0, 0),
            )
            return complete_inspection(**kwargs)

        with (
            patch.object(
                self.store, "_ordinary_agent_current_chain_context", side_effect=context_for
            ),
            patch.object(
                self.store,
                "_require_ordinary_agent_runtime_prerequisites",
                side_effect=lambda _db, *, context, **_: self._prerequisites(context.now),
            ),
            patch.object(self.store, "_provider_delivery_binding_locked", side_effect=binding_for),
            patch.object(
                self.store,
                "_provider_delivery_expectation_locked",
                return_value=(
                    self.fixture.merge_policy,
                    self.fixture.merge_policy.policy.policies[0],
                    self.expectation,
                ),
            ),
            patch(
                "control_plane.provider_delivery_readiness."
                "resolve_provider_delivery_inspection_profile",
                return_value=self.profile,
            ),
        ):
            main_receipt = ensure_provider_delivery_readiness_for_job(
                store=self.store,
                request_id=base.request.request_id,
                inspect=main_inspection,
                wall_time=lambda: float(self.session.now),
            )
            second_main_receipt = ensure_provider_delivery_readiness_for_job(
                store=self.store,
                request_id=second_main.request_id,
                inspect=lambda **_: (_ for _ in ()).throw(
                    AssertionError("same-branch cache must not inspect")
                ),
                wall_time=lambda: float(self.session.now),
            )
            release_receipt = ensure_provider_delivery_readiness_for_job(
                store=self.store,
                request_id=release_request.request_id,
                inspect=complete_inspection,
                wall_time=lambda: float(self.session.now),
            )

        self.assertEqual(
            follower_reasons,
            ["provider_readiness_in_progress", "provider_readiness_in_progress"],
        )
        self.assertEqual(second_main_receipt.receipt_id, main_receipt.receipt_id)
        self.assertEqual(second_main_receipt.facts.base_branch, "main")
        self.assertNotEqual(release_receipt.receipt_id, main_receipt.receipt_id)
        self.assertEqual(release_receipt.facts.base_branch, "release")
        self.assertEqual(
            tuple(
                actions_used(lease_id)
                for lease_id in (
                    base.lease.lease_id,
                    second_main.lease_id,
                    release_request.lease_id,
                )
            ),
            (1, 0, 1),
        )

    def test_binding_currentness_ignores_container_rotation_but_not_consumed_semantics(
        self,
    ) -> None:
        rotated = self.binding.model_copy(
            update={
                "merge_policy_record_id": "policy-r2",
                "merge_policy_sha256": "8" * 64,
            }
        )
        self.assertTrue(provider_delivery_binding_is_current(self.binding, rotated))
        changed = rotated.model_copy(update={"merge_policy_semantics_sha256": "9" * 64})
        self.assertFalse(provider_delivery_binding_is_current(self.binding, changed))

        with self.assertRaisesRegex(ValueError, "independent GitHub App"):
            ProviderDeliveryInspectionBindingV1.model_validate(
                {
                    **self.binding.model_dump(mode="json"),
                    "inspection_app_id": self.binding.ordinary_delivery_app_id,
                }
            )

    def test_stored_ready_survives_container_rotation_and_semantic_change_refreshes(
        self,
    ) -> None:
        ready = self._publish_ready()
        self.assertEqual(ready.status, "ready")
        rotated = self.binding.model_copy(
            update={
                "merge_policy_record_id": "policy-r2",
                "merge_policy_sha256": "8" * 64,
            }
        )
        self.binding_resolver_mock.return_value = (rotated, self.expectation)
        selected = self._reserve()
        assert isinstance(selected, ProviderDeliveryReadinessDecision)
        self.assertEqual(selected.status, "ready")

        changed = rotated.model_copy(update={"merge_policy_semantics_sha256": "9" * 64})
        self.binding_resolver_mock.return_value = (changed, self.expectation)
        refresh = self._reserve()
        assert isinstance(refresh, ProviderDeliveryInspectionReservationV1)
        self.assertEqual(refresh.attempt.generation, 2)

    def test_ready_inside_required_margin_requests_refresh_without_false_inconclusive(self) -> None:
        ready = self._publish_ready()
        assert ready.receipt is not None
        self.prerequisites_mock.return_value = self._prerequisites(ready.receipt.expires_at - 30)
        with self.store._session_factory() as db:
            context = self.store._ordinary_agent_current_chain_context(
                db, request_id=self.fixture.request.request_id
            )
            with (
                patch.object(
                    self.store,
                    "_ordinary_agent_database_epoch",
                    return_value=ready.receipt.expires_at - 30,
                ),
                patch.object(
                    self.store,
                    "_recheck_ordinary_agent_runtime_prerequisites_at",
                ),
            ):
                decision = self.store._provider_delivery_readiness_decision_locked(
                    db,
                    context=context,
                    activation=Mock(),
                    prerequisites=self._prerequisites(ready.receipt.expires_at - 30),
                    required_margin_seconds=30,
                    current_binding=self.binding,
                )

        self.assertEqual(
            (decision.status, decision.reason_code),
            ("refresh_required", "provider_readiness_refresh_required"),
        )

    def test_post_repository_lock_revalidation_rejects_new_authority_expiry(self) -> None:
        before = self._actions_used()
        with self.store._session_factory() as db:
            context = self.store._ordinary_agent_current_chain_context(
                db, request_id=self.fixture.request.request_id
            )
            prerequisites = self._prerequisites(context.now)
            with (
                patch.object(
                    self.store,
                    "_ordinary_agent_database_epoch",
                    return_value=context.lease.expires_at,
                ),
                self.assertRaises(OrdinaryAgentSessionAdmissionDenied),
            ):
                self.store._provider_delivery_readiness_decision_locked(
                    db,
                    context=context,
                    activation=Mock(),
                    prerequisites=prerequisites,
                    required_margin_seconds=30,
                    current_binding=self.binding,
                )

        self.assertEqual(self._actions_used(), before)

    def test_new_conclusive_protection_drift_shadows_stored_ready(self) -> None:
        ready = self._publish_ready()
        self.assertEqual(ready.status, "ready")
        with self.store._session_factory() as db:
            request_row = db.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow,
                self.fixture.request.request_id,
            )
            assert request_row is not None
            client_intent_sha256 = request_row.intent_sha256
        attempt = ProviderDeliveryInspectionAttemptV1(
            attempt_id=provider_delivery_inspection_attempt_id(
                demand_id=self.fixture.request.request_id,
                generation=2,
                provider_attempt_ordinal=1,
            ),
            demand_id=self.fixture.request.request_id,
            client_intent_sha256=client_intent_sha256,
            principal_id=self.fixture.request.principal_id,
            session_id=self.fixture.request.session_id,
            lease_id=self.fixture.request.lease_id,
            generation=2,
            provider_attempt_ordinal=1,
            action_ordinal=1,
            binding=self.binding,
            inspection_started_at=self.session.now,
            observation_anchor=self.session.now,
            dispatch_deadline=self.session.now + 45,
            publication_deadline=self.session.now + 60,
        )
        with self.store._session_factory() as db:
            db.add(
                LaunchplaneProviderDeliveryInspectionRow(
                    attempt_id=attempt.attempt_id,
                    demand_id=attempt.demand_id,
                    generation=attempt.generation,
                    provider_attempt_ordinal=attempt.provider_attempt_ordinal,
                    action_ordinal=attempt.action_ordinal,
                    profile_id=attempt.binding.inspection_profile_id,
                    profile_sha256=attempt.binding.inspection_profile_sha256,
                    repository_id=attempt.binding.target.repository_id,
                    repository=attempt.binding.target.repository,
                    base_branch=attempt.binding.target.base_branch,
                    inspection_phase=attempt.inspection_phase,
                    custody_phase=attempt.custody_phase,
                    revision=attempt.revision,
                    inspection_started_at=attempt.inspection_started_at,
                    dispatch_deadline=attempt.dispatch_deadline,
                    publication_deadline=attempt.publication_deadline,
                    payload=attempt.model_dump(mode="json"),
                )
            )
            db.commit()
        minting = self.store.mark_provider_delivery_inspection_minting(
            attempt_id=attempt.attempt_id,
            expected_revision=attempt.revision,
            app_id=700,
            installation_id=702,
        )
        issued = self.store.mark_provider_delivery_inspection_issued(
            attempt_id=minting.attempt_id,
            expected_revision=minting.revision,
            app_id=700,
            installation_id=702,
            repository_id=self.binding.target.repository_id,
            token_expires_at=self.session.now + 600,
        )
        closed = self.store.close_provider_delivery_inspection_custody(
            attempt_id=issued.attempt_id,
            expected_revision=issued.revision,
            outcome="confirmed_revoked",
        )
        drifted_expectation = self.expectation.model_copy(
            update={
                "required_status_checks": (
                    ProviderRequiredStatusCheckExpectationV1(
                        context="different-check", app_id=9002
                    ),
                )
            }
        )
        negative = self.store.finish_provider_delivery_inspection(
            attempt_id=closed.attempt_id,
            expected_revision=closed.revision,
            result=self._result(
                status="protection_not_ready",
                expectation=drifted_expectation,
            ),
        )
        selected = self._reserve()

        self.assertEqual(negative.status, "protection_not_ready")
        assert isinstance(selected, ProviderDeliveryReadinessDecision)
        self.assertEqual(
            (selected.status, selected.reason_code),
            ("protection_not_ready", "provider_protection_not_ready"),
        )

    def test_crashed_reserved_flight_recovers_once_then_retries_without_recharge(self) -> None:
        before = self._actions_used()
        first = self._reserve()
        assert isinstance(first, ProviderDeliveryInspectionReservationV1)

        self.session.now = first.attempt.publication_deadline
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        recovered = self._reserve()
        assert isinstance(recovered, ProviderDeliveryReadinessDecision)
        self.assertEqual(
            (recovered.status, recovered.reason_code),
            ("capability_unavailable", "provider_inspection_abandoned"),
        )
        self.assertEqual(self._actions_used(), before + 1)

        self.session.now = recovered.retry_not_before or 0
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        successor = self._reserve()
        assert isinstance(successor, ProviderDeliveryInspectionReservationV1), successor
        self.assertEqual(
            (
                successor.attempt.generation,
                successor.attempt.provider_attempt_ordinal,
                successor.attempt.action_ordinal,
            ),
            (first.attempt.generation, 2, first.attempt.action_ordinal),
        )
        self.assertEqual(self._actions_used(), before + 1)

    def test_late_result_and_stale_finisher_cannot_publish_ready(self) -> None:
        reserved = self._reserve()
        assert isinstance(reserved, ProviderDeliveryInspectionReservationV1)
        minting = self.store.mark_provider_delivery_inspection_minting(
            attempt_id=reserved.attempt.attempt_id,
            expected_revision=reserved.attempt.revision,
            app_id=700,
            installation_id=701,
        )
        issued = self.store.mark_provider_delivery_inspection_issued(
            attempt_id=minting.attempt_id,
            expected_revision=minting.revision,
            app_id=700,
            installation_id=701,
            repository_id=self.binding.target.repository_id,
            token_expires_at=self.session.now + 600,
        )
        closed = self.store.close_provider_delivery_inspection_custody(
            attempt_id=issued.attempt_id,
            expected_revision=issued.revision,
            outcome="confirmed_revoked",
        )
        self.session.now = reserved.attempt.publication_deadline
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()

        late = self.store.finish_provider_delivery_inspection(
            attempt_id=closed.attempt_id,
            expected_revision=closed.revision,
            result=self._result(),
        )

        self.assertEqual(
            (late.status, late.reason_code),
            ("capability_unavailable", "provider_inspection_late_result"),
        )
        self.assertIsNone(late.receipt)
        with self.assertRaisesRegex(Exception, "provider_readiness_in_progress"):
            self.store.finish_provider_delivery_inspection(
                attempt_id=closed.attempt_id,
                expected_revision=closed.revision,
                result=self._result(),
            )

    def test_wrong_repository_owner_facts_cannot_publish_ready_receipt(self) -> None:
        reserved = self._reserve()
        assert isinstance(reserved, ProviderDeliveryInspectionReservationV1)
        minting = self.store.mark_provider_delivery_inspection_minting(
            attempt_id=reserved.attempt.attempt_id,
            expected_revision=reserved.attempt.revision,
            app_id=700,
            installation_id=701,
        )
        issued = self.store.mark_provider_delivery_inspection_issued(
            attempt_id=minting.attempt_id,
            expected_revision=minting.revision,
            app_id=700,
            installation_id=701,
            repository_id=self.binding.target.repository_id,
            token_expires_at=self.session.now + 600,
        )
        closed = self.store.close_provider_delivery_inspection_custody(
            attempt_id=issued.attempt_id,
            expected_revision=issued.revision,
            outcome="confirmed_revoked",
        )
        decision = self.store.finish_provider_delivery_inspection(
            attempt_id=closed.attempt_id,
            expected_revision=closed.revision,
            result=self._result(owner_id=457),
        )

        self.assertEqual(
            (decision.status, decision.reason_code),
            ("capability_unavailable", "provider_inspection_abandoned"),
        )
        self.assertIsNone(decision.receipt)


if __name__ == "__main__":
    unittest.main()
