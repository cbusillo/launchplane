from dataclasses import replace
from typing import Any, get_args
import unittest
from unittest.mock import patch

from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.generic_web_deploy_recovery import GenericWebDeployRecoveryAction
from control_plane import generic_web_deploy_recovery_http as recovery
from tests.support.auth import scoped_agent_transport
from tests import test_generic_web_testing_abandonment as abandonment
from tests.test_generic_web_deploy_recovery import _OPERATOR_TOKEN, _create_recovery_app
from tests.test_generic_web_deploy_recovery import _recovery_bearer_config
from tests.test_service import _invoke_app


class TestingAbandonmentAuthorityTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.scenario = abandonment.GenericWebTestingAbandonmentTests()
        self.scenario.database_url = self.database_url
        self.scenario.setUp()
        self.addCleanup(self.scenario.doCleanups)
        self.store = self.scenario.store
        if self.store.database_dialect_name != "sqlite":
            self.store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="testing-native-abandonment-fixture",
                    source="fixture",
                    updated_at=self.scenario.original.updated_at,
                    policy=LaunchplaneAuthzPolicy(),
                )
            )
        self.app = _create_recovery_app(
            root=self.scenario.fixture.root,
            store=self.store,
            authz_policy=LaunchplaneAuthzPolicy(),
        )
        status, reference = _invoke_app(
            self.app,
            method="GET",
            path=f"/v1/admin/generic-web/deploy-recovery/{self.scenario.fixture.profile.product}/testing",
            authorization=f"Bearer {_OPERATOR_TOKEN}",
        )
        self.assertEqual(status, 200, reference)
        self.body = {key: reference[key] for key in ("product", "instance", "recovery_reference")}
        self.body["reason"] = "Abandon only this unqualified testing journal."

    def inspect(self) -> dict[str, Any]:
        status, result = _invoke_app(
            self.app,
            method="POST",
            path="/v1/admin/generic-web/deploy-recovery/dry-run",
            authorization=f"Bearer {_OPERATOR_TOKEN}",
            payload=self.body,
        )
        self.assertEqual(status, 200, result)
        return result

    def apply(self, digest: str) -> tuple[int, dict[str, Any]]:
        return _invoke_app(
            self.app,
            method="POST",
            path="/v1/admin/generic-web/deploy-recovery/apply",
            authorization=f"Bearer {_OPERATOR_TOKEN}",
            payload={**self.body, "expected_recovery_digest": digest},
        )

    def test_director_can_abandon_and_replay_reconcile_testing_without_execute_grant(self) -> None:
        plan = self.inspect()
        self.assertEqual(plan["proposed_action"], "abandon_testing_original")
        with patch(
            "control_plane.generic_web_deploy_provider_adapter.execute_generic_web_deploy_result"
        ) as deploy:
            status, applied = self.apply(plan["recovery_digest"])
            self.assertEqual(status, 202, applied)
            readback = self.inspect()
            self.assertEqual(readback["reservation_state"], "completed")
            self.assertEqual(readback["proposed_action"], "replay_completed")
            self.assertEqual(readback["recovery_digest"], plan["recovery_digest"])
            status, replay = self.apply(plan["recovery_digest"])
            self.assertEqual(status, 202, replay)
            self.assertEqual(applied["recovery_digest"], replay["recovery_digest"])
        self.assertEqual(
            self.store.read_deployment_record(self.scenario.deployment.record_id),
            self.scenario.deployment,
        )
        deploy.assert_not_called()

    def test_unmarked_operator_does_not_inherit_abandonment(self) -> None:
        plan = self.inspect()
        with scoped_agent_transport():
            status, result = self.apply(plan["recovery_digest"])
        self.assertEqual(status, 403, result)
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.scenario.original,)
        )

    def test_stale_review_cannot_complete_original(self) -> None:
        plan = self.inspect()
        digest = plan["recovery_digest"]
        changed = ("0" if digest[0] != "0" else "1") + digest[1:]
        status, result = self.apply(changed)
        self.assertEqual(status, 409, result)
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.scenario.original,)
        )

    def test_explicit_foreign_original_remains_policy_guarded(self) -> None:
        plan = self.inspect()
        status, result = _invoke_app(
            self.app,
            method="POST",
            path="/v1/admin/generic-web/deploy-recovery/apply",
            authorization=f"Bearer {_OPERATOR_TOKEN}",
            payload={
                "product": self.body["product"],
                "instance": self.body["instance"],
                "original_deploy": self.scenario.fixture.request,
                "reason": self.body["reason"],
                "expected_recovery_digest": plan["recovery_digest"],
            },
            headers={"Idempotency-Key": self.scenario.key},
        )
        self.assertEqual(status, 403, result)
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.scenario.original,)
        )

    def test_terminal_director_does_not_inherit_mutation(self) -> None:
        plan = self.inspect()
        config = _recovery_bearer_config().model_copy(
            update={
                "terminal_agent_token": "terminal-testing-fixture",
                "terminal_agent_subject": "terminal-director",
                "terminal_agent_token_label": "terminal",
            }
        )
        with patch(
            "tests.test_generic_web_deploy_recovery._recovery_bearer_config", return_value=config
        ):
            app = _create_recovery_app(
                root=self.scenario.fixture.root,
                store=self.store,
                authz_policy=LaunchplaneAuthzPolicy(),
            )
        status, result = _invoke_app(
            app,
            method="POST",
            path="/v1/admin/generic-web/deploy-recovery/apply",
            authorization=f"Bearer {config.terminal_agent_token}",
            payload={**self.body, "expected_recovery_digest": plan["recovery_digest"]},
        )
        self.assertEqual(status, 403, result)
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.scenario.original,)
        )

    def test_reference_cannot_name_production(self) -> None:
        plan = self.inspect()
        status, _ = _invoke_app(
            self.app,
            method="POST",
            path="/v1/admin/generic-web/deploy-recovery/apply",
            authorization=f"Bearer {_OPERATOR_TOKEN}",
            payload={
                **self.body,
                "instance": "prod",
                "expected_recovery_digest": plan["recovery_digest"],
            },
        )
        self.assertTrue(400 <= status < 500)
        self.assertEqual(
            self.store.list_held_provider_target_reservations(), (self.scenario.original,)
        )

    def test_other_selected_actions_refuse_before_any_write_branch(self) -> None:
        plan = self.inspect()
        inspect_original = recovery._inspect_generic_web_deploy_recovery
        for action in get_args(GenericWebDeployRecoveryAction):
            if action == plan["proposed_action"]:
                continue

            async def select_other(**kwargs: Any) -> recovery._GenericWebDeployRecoveryInspection:
                inspected = await inspect_original(**kwargs)
                return replace(inspected, proposed_action=action)

            with (
                self.subTest(action=action),
                patch.object(
                    recovery, "_inspect_generic_web_deploy_recovery", side_effect=select_other
                ),
                patch.object(recovery, "_transition_expired_running_recovery") as transition,
                patch.object(self.store, "adopt_reconciled_mutation") as adopt,
            ):
                status, result = self.apply(plan["recovery_digest"])
            self.assertEqual(status, 403, result)
            transition.assert_not_called()
            adopt.assert_not_called()
            self.assertEqual(
                self.store.list_held_provider_target_reservations(), (self.scenario.original,)
            )
