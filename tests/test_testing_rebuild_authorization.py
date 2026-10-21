"""The Director's existing transport queues only a native testing operation."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationDeniedError,
    DurableOperationAuthorizationGuard,
)
from control_plane import service_bootstrap
from control_plane.service_auth import (
    BearerIdentityConfig,
    configured_local_operator_identity,
)
from control_plane.testing_rebuild_authorization import (
    capture_director_testing_rebuild_authorization,
    director_testing_rebuild_allowed,
    director_testing_rebuild_authorization_allows,
)
from control_plane.workflows import odoo_stable_operation_worker as worker
from tests import test_testing_rebuild_worker as fixtures
from tests import test_odoo_prod_promotion_operation as administrator


class DirectorTestingRebuildAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.NativeTestingWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.policy = administrator._policy_record(administrator._ADMINISTRATOR_POLICY)
        self.store.seed_authz_policy_if_absent(self.policy)
        self.config = BearerIdentityConfig(
            local_operator_token="fixture-operator",
            local_operator_subject="fixture-director",
            local_operator_token_label="fixture-token",
        )
        identity = configured_local_operator_identity(self.config)
        assert identity is not None
        self.identity = identity
        self.original = self.fixture.operation
        self.authorization = capture_director_testing_rebuild_authorization(
            identity=identity,
            request=self.original.request,
            context=self.original.context,
            store=self.store,
            authorized_at=self.original.created_at,
        )
        self.operation = OdooStableTargetReplacementOperationRecord.model_validate(
            {
                **self.original.model_dump(mode="json"),
                "schema_version": 2,
                "authorization": self.authorization.model_dump(mode="json"),
            }
        )

    def test_standing_capture_refuses_other_callers_and_non_native_destinations(self) -> None:
        for identity, request, context in (
            (
                replace(self.identity, director_agent=False),
                self.original.request,
                self.original.context,
            ),
            (
                self.identity,
                self.original.request.model_copy(update={"instance": "prod"}),
                self.original.context,
            ),
            (
                self.identity,
                self.original.request.model_copy(update={"strategy": "recreate-in-place"}),
                self.original.context,
            ),
            (self.identity, self.original.request, "different-context"),
        ):
            with self.subTest(identity=identity, request=request, context=context):
                self.assertFalse(
                    director_testing_rebuild_allowed(
                        identity=identity, request=request, context=context, store=self.store
                    )
                )
        self.assertEqual(
            DurableOperationAuthorization.model_validate(
                self.authorization.model_dump(mode="json")
            ),
            self.authorization,
        )
        with self.assertRaises(ValidationError):
            OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **self.operation.model_dump(mode="json"),
                    "request": self.operation.request.model_copy(
                        update={"strategy": "recreate-in-place"}
                    ).model_dump(mode="json"),
                }
            )

    def test_durable_guard_requires_native_callback_and_current_bootstrap_binding(self) -> None:
        guard = DurableOperationAuthorizationGuard(
            authorization=self.authorization, policy_record_reader=lambda: self.policy
        )
        with self.assertRaises(DurableOperationAuthorizationDeniedError):
            guard.authorize_execution()
        guard.director_testing_rebuild_allows = lambda authorization: (
            director_testing_rebuild_authorization_allows(
                authorization=authorization,
                operation=self.operation,
                store=self.store,
                config=self.config,
            )
        )
        guard.authorize_execution()
        self.config.local_operator_token_label = "replacement-operator"
        with self.assertRaises(DurableOperationAuthorizationDeniedError):
            guard.recheck_provider_effect("next-effect")
        self.assertEqual(self.fixture.effects, [])

    def test_real_worker_completes_native_lifecycle_under_standing_authority(self) -> None:
        now = datetime.now(UTC)
        operation = self.operation.model_copy(
            update={
                "status": "running",
                "phase": "running",
                "lease_owner": "worker",
                "started_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        with patch.object(
            service_bootstrap, "_bearer_identity_config_from_env", return_value=self.config
        ):
            self.assertTrue(
                worker._execute_target_replacement_operation(
                    record_store=self.store,
                    control_plane_root_path=self.fixture.root,
                    operation=operation,
                    lease_owner="worker",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        completed = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(completed.status, "pass", completed.error_message)
        self.assertEqual(self.fixture.effects, ["switch-new", "stop-old"])
