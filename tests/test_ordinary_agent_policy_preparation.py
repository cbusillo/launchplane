from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from control_plane.authz_candidate_preparation import (
    compile_ordinary_agent_delivery_policy_candidate,
    ordinary_agent_delivery_policy_managed_set_id,
)
from control_plane.authz_grant_service import plan_managed_authz_policy_reconcile
from control_plane.contracts.ordinary_agent import OrdinaryAgentPolicyRule, OrdinaryAgentTarget
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetProposalInput,
    ManagedOrdinaryAgentPolicyPreparationContext,
    OrdinaryAgentDeliveryPolicyIntent,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.http_routes.privileged_operations import (
    PrivilegedOperationPlanEnvelope,
    PrivilegedPolicyOperationAgentProposalEnvelope,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url
from tests import test_privileged_operation_http as privileged_operation_http
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.privileged_operation_registry import (
    PrivilegedOperationPlanningConflictError,
    plan_managed_authz_policy_set,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy


def _inventory() -> RepositoryInventoryRecord:
    return RepositoryInventoryRecord(
        repository_id="9001",
        repository_owner_id="9002",
        repository="cbusillo/sellyouroutboard",
        inventory_state="tracked",
        inventory_revision=1,
        recorded_at="2026-09-14T12:00:00Z",
        source="test",
        reason="test",
    )


def _intent(principal_id: str = "agent_client_abc") -> OrdinaryAgentDeliveryPolicyIntent:
    return OrdinaryAgentDeliveryPolicyIntent(
        repository_id="9001",
        base_branch="main",
        principal_id=principal_id,
        client_label="Client one",
    )


def _merge_record() -> MergeTrainPolicyRecord:
    policy = build_test_merge_train_policy()
    return MergeTrainPolicyRecord(
        record_id="merge-train-policy-test",
        source="test",
        updated_at="2026-09-14T12:00:00Z",
        policy=policy,
    )


class _AuthzPolicyStore:
    def __init__(
        self,
        policy: LaunchplaneAuthzPolicy,
        *,
        inventory: RepositoryInventoryRecord | None = None,
        merge_policy: MergeTrainPolicyRecord | None = None,
    ) -> None:
        self.record = privileged_operation_http._policy_record(policy)
        self.inventory = inventory
        self.merge_policy = merge_policy

    def list_authz_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        return (self.record,) if status in {"", "active"} else ()

    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]:
        return (self.inventory,) if self.inventory is not None else ()

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        return (self.merge_policy,) if self.merge_policy is not None else ()


class OrdinaryAgentPolicyPreparationTests(unittest.TestCase):
    def test_schema_v2_compiles_one_schema_v3_rule_and_preserves_humans(self) -> None:
        current = privileged_operation_http._policy()
        state, proposal = compile_ordinary_agent_delivery_policy_candidate(
            current_policy=current,
            intent=_intent(),
            inventory=_inventory(),
            merge_policy=_merge_record(),
        )

        self.assertEqual(state, "planned")
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.schema_migration, "migrate_v2_to_v3")
        self.assertEqual(proposal.desired_policy.schema_version, 3)
        self.assertEqual(len(proposal.desired_policy.ordinary_agents), 1)
        _, _, reconciled, _ = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore(current),
            request=proposal.reconcile_request(),
        )
        self.assertEqual(reconciled.github_actions, current.github_actions)
        self.assertEqual(reconciled.github_humans, current.github_humans)
        self.assertEqual(reconciled.terminal_agents, current.terminal_agents)
        self.assertEqual(reconciled.local_operators, current.local_operators)
        self.assertEqual(reconciled.local_admins, current.local_admins)
        self.assertEqual(len(reconciled.ordinary_agents), 1)
        self.assertEqual(reconciled.ordinary_agents[0].principal_id, "agent_client_abc")

    def test_schema_v3_compiles_new_client_rule_without_migration(self) -> None:
        current = privileged_operation_http._policy().model_copy(update={"schema_version": 3})
        state, proposal = compile_ordinary_agent_delivery_policy_candidate(
            current_policy=current,
            intent=_intent("agent_client_xyz"),
            inventory=_inventory(),
            merge_policy=_merge_record(),
        )

        self.assertEqual(state, "planned")
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual(proposal.schema_migration, "reject")
        self.assertEqual(proposal.desired_policy.schema_version, 3)
        self.assertEqual(
            proposal.desired_policy.ordinary_agents[0].principal_id, "agent_client_xyz"
        )

    def test_schema_v3_reconcile_preserves_preexisting_ordinary_rule(self) -> None:
        existing = OrdinaryAgentPolicyRule(
            managed_set_id="ordinary-agent.existing",
            managed_rule_id="delivery",
            principal_id="agent_existing_abc",
            target=OrdinaryAgentTarget(
                repository_id=9001,
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
            ),
            actions=("self_read",),
        )
        current = privileged_operation_http._policy().model_copy(
            update={"schema_version": 3, "ordinary_agents": (existing,)}
        )
        state, proposal = compile_ordinary_agent_delivery_policy_candidate(
            current_policy=current,
            intent=_intent("agent_client_xyz"),
            inventory=_inventory(),
            merge_policy=_merge_record(),
        )
        self.assertEqual(state, "planned")
        assert proposal is not None
        _, _, reconciled, _ = plan_managed_authz_policy_reconcile(
            record_store=_AuthzPolicyStore(current),
            request=proposal.reconcile_request(),
        )
        self.assertEqual(
            {rule.principal_id for rule in reconciled.ordinary_agents},
            {"agent_existing_abc", "agent_client_xyz"},
        )

    def test_registry_rejects_fresh_inventory_baseline_drift(self) -> None:
        current = privileged_operation_http._policy()
        policy_record = privileged_operation_http._policy_record(current)
        inventory = _inventory()
        merge_policy = _merge_record()
        state, proposal = compile_ordinary_agent_delivery_policy_candidate(
            current_policy=current,
            intent=_intent(),
            inventory=inventory,
            merge_policy=merge_policy,
        )
        self.assertEqual(state, "planned")
        assert proposal is not None
        context = ManagedOrdinaryAgentPolicyPreparationContext(
            intent=_intent(),
            managed_set_id=proposal.managed_set_id,
            managed_rule_id="delivery",
            expected_policy_record_id=policy_record.record_id,
            expected_policy_revision=policy_record.revision,
            expected_policy_sha256=policy_record.policy_sha256,
            expected_inventory_record_id=inventory.record_id,
            expected_inventory_revision=inventory.inventory_revision,
            expected_inventory_sha256=inventory.inventory_digest,
            expected_merge_policy_record_id=merge_policy.record_id,
            expected_merge_policy_sha256=merge_policy.policy_sha256,
        )
        prepared = proposal.model_copy(update={"ordinary_agent_preparation_context": context})
        store = _AuthzPolicyStore(current, inventory=inventory, merge_policy=merge_policy)
        store.inventory = RepositoryInventoryRecord.model_validate(
            {
                **inventory.model_dump(mode="json"),
                "record_id": "repository-inventory-9001-r2",
                "inventory_revision": 2,
                "supersedes_record_id": inventory.record_id,
                "inventory_digest": "",
            }
        )
        with self.assertRaises(PrivilegedOperationPlanningConflictError):
            plan_managed_authz_policy_set(store, prepared)

    def test_exact_existing_rule_is_already_satisfied(self) -> None:
        intent = _intent()
        managed_set_id = ordinary_agent_delivery_policy_managed_set_id(intent.principal_id)
        current = LaunchplaneAuthzPolicy(
            schema_version=3,
            ordinary_agents=(
                OrdinaryAgentPolicyRule(
                    managed_set_id=managed_set_id,
                    managed_rule_id="delivery",
                    principal_id=intent.principal_id,
                    target=OrdinaryAgentTarget(
                        repository_id=9001,
                        repository="cbusillo/sellyouroutboard",
                        base_branch="main",
                    ),
                    actions=("self_read", "preflight", "guarded_merge"),
                ),
            ),
        )
        state, proposal = compile_ordinary_agent_delivery_policy_candidate(
            current_policy=current,
            intent=intent,
            inventory=_inventory(),
            merge_policy=_merge_record(),
        )

        self.assertEqual(state, "already_satisfied")
        self.assertIsNone(proposal)


class OrdinaryAgentPolicyPreparationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_human_cannot_prepare_or_create_operation(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = privileged_operation_http._policy()
            store.seed_authz_policy_if_absent(privileged_operation_http._policy_record(policy))
            unauthorized = GitHubHumanIdentity(
                login="other",
                github_id=999,
                name="Other",
                email="other@example.com",
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            )
            app = privileged_operation_http.PrivilegedOperationHttpTests()._app(
                store=store,
                policy=policy,
                mutation_human_reader=Mock(return_value=unauthorized),
            )
            async with lifespan_client(app) as client:
                response = await client.post(
                    "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/prepare",
                    json={
                        "source_event_id": "ui:ordinary-agent-policy:denied",
                        "intent": {
                            "repository_id": "9001",
                            "base_branch": "main",
                            "principal_id": "agent_denied_abc",
                            "client_label": "Denied client",
                        },
                    },
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    store.list_privileged_operation_records(
                        descriptor_id="managed-authz-policy-set", limit=None
                    ),
                    (),
                )

    async def test_prepare_replays_after_source_inputs_change_and_is_activation_visible(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(directory) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            policy = privileged_operation_http._policy()
            policy = policy.model_copy(
                update={
                    "github_humans": tuple(
                        rule.model_copy(
                            update={
                                "actions": (
                                    *rule.actions,
                                    "ordinary_agent_delivery_activation.read",
                                )
                            }
                        )
                        if rule.managed_rule_id == "human-policy-planner"
                        else rule
                        for rule in policy.github_humans
                    )
                }
            )
            policy_record = store.seed_authz_policy_if_absent(
                privileged_operation_http._policy_record(policy)
            )
            inventory = _inventory()
            store.write_repository_inventory_record(inventory)
            store.write_merge_train_policy_record(_merge_record())
            route_builder = privileged_operation_http.PrivilegedOperationHttpTests()
            app = route_builder._app(
                store=store,
                policy=policy,
                policy_record_reader=lambda: policy_record,
            )
            intent_payload: dict[str, str] = {
                "repository_id": "9001",
                "base_branch": "main",
                "principal_id": "agent_client_abc",
                "client_label": "Client one",
            }
            payload: dict[str, object] = {
                "source_event_id": "ui:ordinary-agent-policy:stable",
                "intent": intent_payload,
            }
            async with lifespan_client(app) as client:
                first = await client.post(
                    "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/prepare",
                    json=payload,
                )
                self.assertEqual(first.status_code, 200, first.text)
                operation_id = first.json()["operation_id"]
                review = await client.get(f"/v1/privileged-operations/plans/{operation_id}/review")
                self.assertEqual(review.status_code, 200, review.text)
                self.assertEqual(review.json()["review"]["title"], "Review client delivery access")
                self.assertIn("Client one", review.json()["review"]["change"]["summary"])
                self.assertIn(
                    "cbusillo/sellyouroutboard", review.json()["review"]["change"]["summary"]
                )

                updated_inventory = RepositoryInventoryRecord.model_validate(
                    {
                        **inventory.model_dump(mode="json"),
                        "record_id": "repository-inventory-9001-r2",
                        "inventory_revision": 2,
                        "recorded_at": "2026-09-14T13:00:00Z",
                        "supersedes_record_id": inventory.record_id,
                        "inventory_digest": "",
                    }
                )
                store.write_repository_inventory_record(updated_inventory)
                store.write_merge_train_policy_record(
                    build_test_merge_train_policy_record(
                        repository="cbusillo/sellyouroutboard",
                        record_id="merge-train-policy-current-2",
                        updated_at="2026-09-14T13:00:00Z",
                    )
                )
                replay = await client.post(
                    "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/prepare",
                    json=payload,
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json()["operation_id"], operation_id)

                changed_intent = {**intent_payload, "base_branch": "release"}
                changed = {**payload, "intent": changed_intent}
                conflict = await client.post(
                    "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/prepare",
                    json=changed,
                )
                self.assertEqual(conflict.status_code, 409, conflict.text)

                options = await client.get(
                    "/v1/privileged-operations/ordinary-agent-delivery-activation/options"
                )
                self.assertEqual(options.status_code, 200, options.text)
                self.assertEqual(
                    [option["policy_operation_id"] for option in options.json()["setup_options"]],
                    [operation_id],
                )
                with patch.object(
                    store,
                    "read_current_ordinary_agent_principal",
                    return_value=object(),
                ):
                    occupied = await client.post(
                        "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/prepare",
                        json={
                            **payload,
                            "source_event_id": "ui:ordinary-agent-policy:occupied",
                            "intent": {**intent_payload, "principal_id": "agent_existing_abc"},
                        },
                    )
                self.assertEqual(occupied.status_code, 409, occupied.text)
                self.assertEqual(occupied.json()["detail"]["code"], "ordinary_agent_rule_conflict")
                self.assertIn("already registered", occupied.json()["detail"]["message"])

    def test_generic_envelopes_reject_preparation_context(self) -> None:
        intent = _intent()
        context = ManagedOrdinaryAgentPolicyPreparationContext(
            intent=intent,
            managed_set_id=ordinary_agent_delivery_policy_managed_set_id(intent.principal_id),
            managed_rule_id="delivery",
            expected_policy_record_id="policy",
            expected_policy_revision=1,
            expected_policy_sha256="a" * 64,
            expected_inventory_record_id="inventory",
            expected_inventory_revision=1,
            expected_inventory_sha256="b" * 64,
            expected_merge_policy_record_id="merge",
            expected_merge_policy_sha256="c" * 64,
        )
        proposal = ManagedAuthzPolicySetProposalInput(
            managed_set_id=context.managed_set_id,
            desired_policy=LaunchplaneAuthzPolicy(schema_version=3),
            reason="test",
            ordinary_agent_preparation_context=context,
        )
        with self.assertRaises(ValueError):
            PrivilegedOperationPlanEnvelope(
                descriptor_id="managed-authz-policy-set",
                source_event_id="context-check",
                request=proposal,
            )
        with self.assertRaises(ValueError):
            PrivilegedPolicyOperationAgentProposalEnvelope(
                descriptor_id="managed-authz-policy-set",
                source_event_id="context-check",
                request=proposal,
            )


if __name__ == "__main__":
    unittest.main()
