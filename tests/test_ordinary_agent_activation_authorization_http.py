from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator, cast
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationRevokeHumanEvidence,
    OrdinaryAgentDeliveryActivationRevokeRequest,
    OrdinaryAgentDeliveryActivationReference,
    OrdinaryAgentDeliveryActivationScope,
    OrdinaryAgentDeliveryActivationSetupHumanEvidence,
    OrdinaryAgentDeliveryActivationSetupRequest,
    OrdinaryAgentDeliveryInventoryReference,
    OrdinaryAgentDeliveryPolicyPackageReference,
    OrdinaryAgentDeliveryRuntimeCapabilityEvidence,
)
from control_plane.contracts.privileged_operation import (
    ORDINARY_AGENT_DELIVERY_ACTIVATION_APPROVE_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_CANCEL_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_REVOKE_ACTION,
    PrivilegedOperationHumanEvidence,
    PrivilegedOperationRequest,
)
from control_plane.http_routes.privileged_operations import (
    PrivilegedOperationRouteDependencies,
    register_privileged_operation_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane import privileged_operation_registry
from control_plane.privileged_operation_registry import (
    ORDINARY_AGENT_DELIVERY_ACTIVATION_DESCRIPTOR,
    RegisteredPrivilegedOperationDescriptor,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.http import lifespan_client


class _ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


def _human(*, github_id: int = 123) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="operator",
        github_id=github_id,
        name="Operator",
        email="operator@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _agent() -> TerminalAgentIdentity:
    return TerminalAgentIdentity(subject="agent:planner", token_label="planner")


def _policy(
    *,
    actions: tuple[str, ...],
    duplicate: bool = False,
    context: str = "launchplane",
) -> LaunchplaneAuthzPolicy:
    rule: dict[str, object] = {
        "managed_set_id": "ordinary-agent.activation-administration",
        "managed_rule_id": "activation-administrator",
        "github_ids": [123],
        "roles": ["admin"],
        "products": ["launchplane"],
        "contexts": [context],
        "actions": list(actions),
    }
    rules = [rule]
    if duplicate:
        rules.append(
            {
                **rule,
                "managed_rule_id": "activation-administrator-overlap",
            }
        )
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": rules,
        }
    )


def _policy_record(policy: LaunchplaneAuthzPolicy) -> LaunchplaneAuthzPolicyRecord:
    return LaunchplaneAuthzPolicyRecord(
        record_id="launchplane-authz-policy-activation-http",
        revision=7,
        source="test",
        updated_at="2026-09-10T20:00:00Z",
        policy=policy,
    )


def _scope() -> OrdinaryAgentDeliveryActivationScope:
    return OrdinaryAgentDeliveryActivationScope(
        target=OrdinaryAgentTarget(
            repository_id=1001,
            repository="example/launchplane",
            base_branch="main",
        ),
        managed_set_id="ordinary-agent.pilot",
        managed_rule_id="agent-one",
    )


def _capability() -> OrdinaryAgentDeliveryRuntimeCapabilityEvidence:
    return OrdinaryAgentDeliveryRuntimeCapabilityEvidence(
        observed_database_revision="activation-head",
        database_revision_compatible=True,
        activation_schema_invariants_sha256="a" * 64,
        activation_schema_invariants_valid=True,
        finite_request_versions=(1, 2),
        read_attempt_versions=(1,),
        custody_reservation_versions=(1,),
        qualification_attestation_versions=(1,),
        activation_record_versions=(1,),
        activation_event_versions=(1,),
        recovery_versions=(1,),
        authz_policy_read_versions=(2, 3),
        variant_parsers_registered=True,
        activation_storage_registered=True,
        activation_cas_registered=True,
        activation_recovery_registered=True,
        bounded_cleanup_registered=True,
        rollback_reader_registered=True,
        qualification_advancer_registered=False,
        guarded_worker_registered=False,
        policy_v3_write_supported=False,
        observed_at="2026-09-10T20:05:00Z",
    )


def _planned_evidence(
    record_store: object,
    request: PrivilegedOperationRequest,
) -> PrivilegedOperationHumanEvidence:
    _ = record_store
    if isinstance(request, OrdinaryAgentDeliveryActivationSetupRequest):
        return OrdinaryAgentDeliveryActivationSetupHumanEvidence(
            result_status="ok",
            scope=_scope(),
            policy_package=OrdinaryAgentDeliveryPolicyPackageReference(
                policy_operation_id=request.policy_operation_id,
                request_sha256="b" * 64,
                evidence_sha256="c" * 64,
                plan_sha256="d" * 64,
                desired_set_sha256="e" * 64,
                candidate_policy_sha256="f" * 64,
            ),
            inventory=OrdinaryAgentDeliveryInventoryReference(
                record_id=request.repository_inventory_record_id,
                revision=1,
                inventory_sha256="1" * 64,
            ),
            predecessor=request.predecessor,
            activation_expires_at=request.activation_expires_at,
            runtime_capability=_capability(),
            plan_digest="2" * 64,
        )
    if isinstance(request, OrdinaryAgentDeliveryActivationRevokeRequest):
        return OrdinaryAgentDeliveryActivationRevokeHumanEvidence(
            scope=_scope(),
            activation=OrdinaryAgentDeliveryActivationReference(
                activation_id=request.activation_id,
                revision=request.expected_revision,
                activation_sha256=request.expected_activation_sha256,
            ),
            source_setup_operation_id="privileged-operation-original-setup",
            plan_digest="3" * 64,
        )
    raise AssertionError("activation test planner received another request variant")


@contextmanager
def _inert_activation_planner() -> Iterator[None]:
    registration = RegisteredPrivilegedOperationDescriptor(
        descriptor=ORDINARY_AGENT_DELIVERY_ACTIVATION_DESCRIPTOR,
        planner=_planned_evidence,
    )
    with patch.dict(
        privileged_operation_registry._REGISTRY,
        {"ordinary-agent-delivery-activation": registration},
    ):
        yield


def _setup_payload(source_event_id: str) -> dict[str, object]:
    return {
        "descriptor_id": "ordinary-agent-delivery-activation",
        "source_event_id": source_event_id,
        "request": {
            "action": "setup",
            "policy_operation_id": "privileged-operation-policy-package",
            "repository_inventory_record_id": "repository-inventory-1001-r1",
            "activation_expires_at": "2030-09-11T20:00:00Z",
            "reason": "Prepare one qualification-only activation.",
        },
    }


def _revoke_payload(source_event_id: str) -> dict[str, object]:
    return {
        "descriptor_id": "ordinary-agent-delivery-activation",
        "source_event_id": source_event_id,
        "request": {
            "action": "revoke_activation",
            "activation_id": "ordinary-agent-delivery-activation-" + "4" * 32,
            "expected_revision": 1,
            "expected_activation_sha256": "5" * 64,
            "reason": "Prepare permanent revocation of the activation.",
        },
    }


class OrdinaryAgentActivationAuthorizationHttpTests(unittest.IsolatedAsyncioTestCase):
    def _app(
        self,
        *,
        store: object,
        policy: LaunchplaneAuthzPolicy,
        human: GitHubHumanIdentity | None = None,
        policy_reader: Callable[[], LaunchplaneAuthzPolicy] | None = None,
    ) -> FastAPI:
        app = FastAPI()
        trace_counter = iter(range(1, 100))

        def http_error(
            *,
            status_code: int,
            trace_id: str,
            code: str,
            message: str,
            authz: dict[str, object] | None = None,
        ) -> HTTPException:
            _ = trace_id, authz
            return HTTPException(status_code=status_code, detail={"code": code, "message": message})

        read_policy = policy_reader or (lambda: policy)

        def read_human() -> GitHubHumanIdentity:
            return human or _human()

        register_privileged_operation_routes(
            cast(ApiRouteRegistrar, app),
            dependencies=PrivilegedOperationRouteDependencies(
                common=ReadRouteDependencies(
                    read_identity=_agent,
                    get_record_store=lambda: store,
                    next_trace_id=lambda: f"trace-{next(trace_counter)}",
                    authorization_allows=lambda **_: False,
                    http_error=http_error,
                    error_response_model=_ErrorResponse,
                ),
                read_bearer_identity=_agent,
                read_github_human_identity=read_human,
                read_github_human_mutation_identity=read_human,
                policy_reader=read_policy,
                policy_record_reader=lambda: _policy_record(policy),
            ),
        )
        return app

    async def test_exact_actions_allow_the_complete_human_lifecycle(self) -> None:
        policy = _policy(
            actions=(
                ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,
                ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,
                ORDINARY_AGENT_DELIVERY_ACTIVATION_CANCEL_ACTION,
                ORDINARY_AGENT_DELIVERY_ACTIVATION_APPROVE_ACTION,
                ORDINARY_AGENT_DELIVERY_ACTIVATION_REVOKE_ACTION,
            )
        )
        with TemporaryDirectory() as directory, _inert_activation_planner():
            app = self._app(
                store=FilesystemRecordStore(Path(directory)),
                policy=policy,
            )
            async with lifespan_client(app) as client:
                setup_plan = await client.post(
                    "/v1/privileged-operations/plans",
                    json=_setup_payload("activation-setup-plan"),
                )
                self.assertEqual(setup_plan.status_code, 200, setup_plan.text)
                setup_operation_id = setup_plan.json()["record"]["operation_id"]

                listed = await client.get(
                    "/v1/privileged-operations/plans",
                    params={"descriptor_id": "ordinary-agent-delivery-activation"},
                )
                read = await client.get(f"/v1/privileged-operations/plans/{setup_operation_id}")
                reviewed = await client.get(
                    f"/v1/privileged-operations/plans/{setup_operation_id}/review"
                )
                approved = await client.post(
                    f"/v1/privileged-operations/plans/{setup_operation_id}/approve",
                    json={
                        "source_event_id": "activation-setup-approval",
                        "reason": "Approve the exact setup plan.",
                    },
                )
                revoked = await client.post(
                    f"/v1/privileged-operations/plans/{setup_operation_id}/revoke",
                    json={
                        "source_event_id": "activation-setup-approval-revocation",
                        "reason": "Withdraw the setup approval before execution.",
                    },
                )

                revoke_plan = await client.post(
                    "/v1/privileged-operations/plans",
                    json=_revoke_payload("activation-revoke-plan"),
                )
                self.assertEqual(revoke_plan.status_code, 200, revoke_plan.text)
                revoke_operation_id = revoke_plan.json()["record"]["operation_id"]
                cancelled = await client.post(
                    f"/v1/privileged-operations/plans/{revoke_operation_id}/cancel",
                    json={
                        "source_event_id": "activation-revoke-cancellation",
                        "reason": "Cancel the unneeded revocation plan.",
                    },
                )

        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["record"]["status"], "approved")
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json()["record"]["status"], "revoked")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["record"]["status"], "cancelled")
        self.assertEqual(setup_plan.json()["record"]["request"]["action"], "setup")
        self.assertEqual(
            revoke_plan.json()["record"]["request"]["action"],
            "revoke_activation",
        )

    async def test_activation_authorization_fails_closed_for_inexact_matches(self) -> None:
        cases = (
            (
                "legacy empty-action wildcard",
                _policy(actions=()),
                _human(),
            ),
            (
                "wrong action",
                _policy(actions=(ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,)),
                _human(),
            ),
            (
                "wrong scope",
                _policy(
                    actions=(ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,),
                    context="another-context",
                ),
                _human(),
            ),
            (
                "overlapping explicit rules",
                _policy(
                    actions=(ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,),
                    duplicate=True,
                ),
                _human(),
            ),
            (
                "third-party immutable identity",
                _policy(actions=(ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,)),
                _human(github_id=456),
            ),
        )
        for label, policy, human in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                app = self._app(
                    store=FilesystemRecordStore(Path(directory)),
                    policy=policy,
                    human=human,
                )
                async with lifespan_client(app) as client:
                    response = await client.get(
                        "/v1/privileged-operations/plans",
                        params={"descriptor_id": "ordinary-agent-delivery-activation"},
                    )

                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["detail"]["code"], "authorization_denied")

    async def test_legacy_empty_action_rule_remains_valid_for_existing_descriptor(self) -> None:
        policy = _policy(actions=())
        with TemporaryDirectory() as directory:
            app = self._app(
                store=FilesystemRecordStore(Path(directory)),
                policy=policy,
            )
            async with lifespan_client(app) as client:
                activation = await client.get(
                    "/v1/privileged-operations/plans",
                    params={"descriptor_id": "ordinary-agent-delivery-activation"},
                )
                existing = await client.get("/v1/privileged-operations/plans")

        self.assertEqual(activation.status_code, 403, activation.text)
        self.assertEqual(existing.status_code, 200, existing.text)
        self.assertEqual(existing.json()["total"], 0)

    async def test_activation_agent_summary_rejects_before_policy_authorization(self) -> None:
        policy = _policy(
            actions=(
                ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,
                ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,
            )
        )
        with TemporaryDirectory() as directory, _inert_activation_planner():
            store = FilesystemRecordStore(Path(directory))
            planning_app = self._app(store=store, policy=policy)
            async with lifespan_client(planning_app) as client:
                planned = await client.post(
                    "/v1/privileged-operations/plans",
                    json=_setup_payload("activation-agent-summary-plan"),
                )
            self.assertEqual(planned.status_code, 200, planned.text)
            operation_id = planned.json()["record"]["operation_id"]

            policy_reader = Mock(side_effect=AssertionError("authorization must not run"))
            summary_app = self._app(
                store=store,
                policy=policy,
                policy_reader=policy_reader,
            )
            async with lifespan_client(summary_app) as client:
                response = await client.get(f"/v1/agent/privileged-operations/plans/{operation_id}")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(
            response.json()["detail"]["code"],
            "privileged_operation_agent_summary_unavailable",
        )
        policy_reader.assert_not_called()

    async def test_activation_plan_envelope_rejects_cross_variant_payloads(self) -> None:
        policy = _policy(actions=(ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,))
        mixed_setup = _setup_payload("activation-mixed-plan")
        mixed_request = cast(dict[str, object], mixed_setup["request"])
        mixed_request["expected_revision"] = 1
        with TemporaryDirectory() as directory, _inert_activation_planner():
            app = self._app(
                store=FilesystemRecordStore(Path(directory)),
                policy=policy,
            )
            async with lifespan_client(app) as client:
                wrong_descriptor_request = await client.post(
                    "/v1/privileged-operations/plans",
                    json={
                        "descriptor_id": "ordinary-agent-delivery-activation",
                        "source_event_id": "activation-wrong-request-plan",
                        "request": {"reason": "This is a managed-secret request."},
                    },
                )
                mixed_variant = await client.post(
                    "/v1/privileged-operations/plans",
                    json=mixed_setup,
                )

        self.assertEqual(wrong_descriptor_request.status_code, 422)
        self.assertEqual(mixed_variant.status_code, 422)


if __name__ == "__main__":
    unittest.main()
