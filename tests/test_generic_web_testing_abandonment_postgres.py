from tests import test_generic_web_testing_abandonment as abandonment
from tests import test_testing_abandonment_authority as native_authority
from tests.test_postgres_integration import _head_postgres_database
from tests.support.auth import local_operator_policy
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from typing import Any
from control_plane import generic_web_testing_abandonment as qualification
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name


class NativeTestingAbandonmentPostgresTests(native_authority.TestingAbandonmentAuthorityTests):
    def setUp(self) -> None:
        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()


class TestingAbandonmentPostgresTests(abandonment.GenericWebTestingAbandonmentTests):
    def setUp(self) -> None:
        self.database_url = self.enterContext(_head_postgres_database())
        super().setUp()
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="testing-abandonment-fixture",
                source="fixture",
                updated_at=self.original.updated_at,
                policy=local_operator_policy(
                    actions=("generic_web_deploy.execute", "product_environment.read"),
                    products=(self.fixture.profile.product,),
                    contexts=(self.deployment.context,),
                ),
            )
        )

    def test_concurrent_reviewed_applies_replay_one_exact_disposition(self) -> None:
        plan = self.inspect()
        barrier = Barrier(2)
        inspect_original = qualification.inspect_testing_abandonment

        def synchronize(*args: Any) -> dict[str, Any] | None:
            result = inspect_original(*args)
            if not self.store.testing_replacement_execution_is_excluded(
                testing_event_execution_lock_name(self.original.provider_target_key)
            ):
                barrier.wait(timeout=10)
            return result

        with (
            patch.object(qualification, "inspect_testing_abandonment", synchronize),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(self.apply, plan["recovery_digest"]) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertTrue(all(status == 202 for status, _payload in results), results)
        self.assertTrue(
            all(
                payload["recovery_digest"] == plan["recovery_digest"]
                for _status, payload in results
            )
        )
        saved = self.store.read_idempotency_record(
            scope=self.original.scope, route_path=self.original.route_path, idempotency_key=self.key
        )
        assert saved is not None
        self.assertEqual(
            saved.response_payload["abandonment"]["original"], self.original.model_dump(mode="json")
        )
        self.assertEqual(
            self.store.read_deployment_record(self.deployment.record_id), self.deployment
        )
