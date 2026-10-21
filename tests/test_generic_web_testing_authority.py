"""Standing testing permission through authenticated HTTP and the durable runner."""

from pathlib import Path
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.disposable_lanes import record_safety
from tests.support.auth import local_operator_policy, scoped_agent_transport
from tests.support.profiles import _product_profile_payload_with_prod
from tests.support.stores import _seed_generic_web_deploy_target_records, _sqlite_database_url
from tests.test_generic_web_http import _generic_web_deploy_result
from tests.test_generic_web_deploy_recovery import (
    _generic_web_recovery_original_deploy,
    _generic_web_recovery_reservation,
    _generic_web_recovery_target,
    _write_generic_web_recovery_reservation,
)
from tests.test_service import _invoke_app


class GenericWebTestingAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(database_url=_sqlite_database_url(self.root / "state.db"))
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _product_profile_payload_with_prod()
        )
        self.store.write_product_profile_record(self.profile)
        self.lane = next(lane for lane in self.profile.lanes if lane.instance == "testing")
        _seed_generic_web_deploy_target_records(
            store=self.store,
            context=self.lane.context,
            instance=self.lane.instance,
            target_id="app-syo-testing",
            target_name="testing",
        )
        record_safety(self.store, self.profile.product, self.lane.context, self.lane.instance)
        self.app = self.make_app(LaunchplaneAuthzPolicy())

    def make_app(self, policy: LaunchplaneAuthzPolicy) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    def deploy(
        self, *, key: str = "testing-deployment", instance: str = "testing"
    ) -> tuple[int, dict[str, Any]]:
        return _invoke_app(
            self.app,
            method="POST",
            path="/v1/drivers/generic-web/deploy",
            authorization="Bearer local-operator-token",
            payload={
                "schema_version": 1,
                "product": self.profile.product,
                "deploy": {
                    "schema_version": 1,
                    "product": self.profile.product,
                    "instance": instance,
                    "artifact_id": f"ghcr.io/{self.profile.repository}@sha256:abc123",
                    "source_git_ref": "abc123",
                },
            },
            headers={
                "Idempotency-Key": key,
            },
        )

    def test_authenticated_director_can_deploy_qualified_testing_without_a_grant(self) -> None:
        with patch(
            "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
            return_value=_generic_web_deploy_result(),
        ) as executor:
            status, payload = self.deploy()
        self.assertEqual(status, 202, payload)
        executor.assert_called_once()

    def test_other_operator_and_production_keep_their_existing_authority(self) -> None:
        with patch("control_plane.generic_web_deploy_http.execute_generic_web_deploy") as executor:
            with scoped_agent_transport():
                status, payload = self.deploy()
            self.assertEqual(status, 403, payload)
            status, payload = self.deploy(instance="prod")
            self.assertEqual(status, 403, payload)
            status, payload = self.deploy(instance="foreign-lane")
            self.assertEqual(status, 403, payload)
            executor.assert_not_called()

    def test_protective_and_unknown_facts_refuse_before_execution(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        for changes in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"data": "unknown"},
            {"production_resources": "shared"},
            {"production_resources": "unknown"},
            {"observed_at": past, "expires_at": past + timedelta(seconds=1)},
        ):
            with self.subTest(changes=changes):
                record_safety(
                    self.store,
                    self.profile.product,
                    self.lane.context,
                    self.lane.instance,
                    **changes,
                )
                with patch(
                    "control_plane.generic_web_deploy_http.execute_generic_web_deploy"
                ) as executor:
                    status, payload = self.deploy()
                self.assertEqual(status, 409, payload)
                self.assertEqual(payload["error"]["code"], "testing_deploy_safety_unknown")
                executor.assert_not_called()

    def test_unknown_occupancy_alone_does_not_require_an_unused_assertion(self) -> None:
        record_safety(
            self.store,
            self.profile.product,
            self.lane.context,
            self.lane.instance,
            usage="unknown",
            usage_evidence="",
        )
        with patch(
            "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
            return_value=_generic_web_deploy_result(),
        ) as executor:
            status, payload = self.deploy()
        self.assertEqual(status, 202, payload)
        executor.assert_called_once()

    def test_facts_are_rechecked_at_the_actual_first_effect_checkpoint(self) -> None:
        effects: list[str] = []
        original_checkpoint = self.store.checkpoint_mutation_provider_effect

        def change_custody_before_checkpoint(**arguments: Any) -> Any:
            record_safety(
                self.store,
                self.profile.product,
                self.lane.context,
                self.lane.instance,
                data="only_copy",
            )
            return original_checkpoint(**arguments)

        def execute(**arguments: Any) -> Any:
            arguments["provider_effect_checkpoint"]("deploy_trigger")
            effects.append("deploy")
            return _generic_web_deploy_result()

        with (
            patch.object(
                self.store,
                "checkpoint_mutation_provider_effect",
                side_effect=change_custody_before_checkpoint,
            ),
            patch(
                "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                side_effect=execute,
            ),
        ):
            status, payload = self.deploy()
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "testing_deploy_safety_unknown")
        self.assertEqual(effects, [])
        # A proved pre-effect refusal releases the reservation, so restoring
        # factual qualification permits the original key without bypassing it.
        record_safety(self.store, self.profile.product, self.lane.context, self.lane.instance)
        with patch(
            "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
            side_effect=execute,
        ):
            status, payload = self.deploy()
        self.assertEqual(status, 202, payload)
        self.assertEqual(effects, ["deploy"])

    def test_completed_replay_remains_inert_after_safety_expires(self) -> None:
        with patch(
            "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
            return_value=_generic_web_deploy_result(),
        ) as executor:
            first_status, first_payload = self.deploy()
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            record_safety(
                self.store,
                self.profile.product,
                self.lane.context,
                self.lane.instance,
                observed_at=past,
                expires_at=past + timedelta(seconds=1),
            )
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={
                        "lifecycle_state": "retiring",
                        "preview": self.profile.preview.model_copy(update={"enabled": False}),
                    }
                )
            )
            replay_status, replay_payload = self.deploy()
            fresh_status, fresh_payload = self.deploy(key="new-after-retirement")
        self.assertEqual(first_status, 202, first_payload)
        self.assertEqual(replay_status, first_status, replay_payload)
        self.assertEqual(replay_payload["records"], first_payload["records"])
        self.assertEqual(fresh_status, 409, fresh_payload)
        executor.assert_called_once()

    def test_new_recipient_facts_cannot_qualify_the_prepared_recipient(self) -> None:
        effects: list[str] = []
        original_checkpoint = self.store.checkpoint_mutation_provider_effect

        def point_at(target_id: str, target_type: str) -> None:
            arguments = {"context_name": self.lane.context, "instance_name": self.lane.instance}
            target = self.store.read_dokploy_target_record(**arguments)
            binding = self.store.read_dokploy_target_id_record(**arguments)
            provider = self.store.read_provider_target_record(**arguments)
            self.store.write_dokploy_target_record(
                target.model_copy(
                    update={
                        "target_type": target_type,
                    }
                )
            )
            self.store.write_dokploy_target_id_record(
                binding.model_copy(
                    update={
                        "target_id": target_id,
                    }
                )
            )
            self.store.write_provider_target_record(
                provider.model_copy(
                    update={
                        "target_id": target_id,
                        "provider_target_type": target_type,
                        "target_category": target_type,
                    }
                )
            )
            record_safety(self.store, self.profile.product, self.lane.context, self.lane.instance)

        def execute(**arguments: Any) -> Any:
            arguments["provider_effect_checkpoint"]("deploy_trigger")
            effects.append("deploy")
            return _generic_web_deploy_result()

        for target_type in ("application", "compose"):
            with self.subTest(target_type=target_type):
                point_at("prepared-recipient", target_type)

                def repoint_before_checkpoint(**arguments: Any) -> Any:
                    point_at("new-recipient", target_type)
                    return original_checkpoint(**arguments)

                with (
                    patch.object(
                        self.store,
                        "checkpoint_mutation_provider_effect",
                        side_effect=repoint_before_checkpoint,
                    ),
                    patch(
                        "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                        side_effect=execute,
                    ),
                ):
                    status, payload = self.deploy(key=f"recipient-{target_type}")
                self.assertEqual(status, 409, payload)
                self.assertEqual(payload["error"]["code"], "testing_deploy_safety_unknown")
                self.assertEqual(effects, [])

    def test_changed_profile_at_effect_admission_refuses_the_prepared_request(self) -> None:
        effects: list[str] = []

        def execute(**arguments: Any) -> Any:
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"display_name": "Changed during preparation"})
            )
            record_safety(self.store, self.profile.product, self.lane.context, self.lane.instance)
            arguments["provider_effect_checkpoint"]("deploy_trigger")
            effects.append("deploy")
            return _generic_web_deploy_result()

        with patch(
            "control_plane.generic_web_deploy_http.execute_generic_web_deploy", side_effect=execute
        ):
            status, payload = self.deploy()
        self.assertEqual(status, 409, payload)
        self.assertEqual(effects, [])

    def test_other_callers_unknown_original_still_fences_a_new_testing_key(self) -> None:
        held = _write_generic_web_recovery_reservation(
            self.store,
            _generic_web_recovery_reservation(
                original_deploy=_generic_web_recovery_original_deploy(),
                idempotency_key="other-callers-original",
                provider_effect_phase="deploy_trigger",
            ),
        )
        provider = SimpleNamespace(
            provider_id="dokploy",
            resolve_deploy_target=lambda **_kwargs: _generic_web_recovery_target(),
        )
        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=provider,
            ),
            patch("control_plane.generic_web_deploy_http.execute_generic_web_deploy") as executor,
        ):
            status, payload = self.deploy(key="fresh-key")
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "mutation_reconciliation_required")
        self.assertIn(held.provider_effect_phase, payload["error"]["message"])
        executor.assert_not_called()

    def test_explicit_admin_permission_keeps_manual_hold_behavior(self) -> None:
        self.app = self.make_app(
            local_operator_policy(
                actions=("generic_web_deploy.execute",),
                products=(self.profile.product,),
                contexts=(self.lane.context,),
                token_label=_local_operator_bearer_config().local_operator_token_label,
            )
        )
        target = self.store.read_dokploy_target_record(
            context_name=self.lane.context, instance_name=self.lane.instance
        )
        for purpose in ("staff_testing", "isolation_staging"):
            with self.subTest(purpose=purpose):
                hold = DokployTargetStaffTestingHold.model_validate(
                    {
                        "reason": "Deliberate admin deployment during hold",
                        "purpose": purpose,
                    }
                )
                self.store.write_dokploy_target_record(
                    target.model_copy(
                        update={
                            "policies": target.policies.model_copy(
                                update={"staff_testing_hold": hold}
                            ),
                        }
                    )
                )
                with patch(
                    "control_plane.generic_web_deploy_http.execute_generic_web_deploy",
                    return_value=_generic_web_deploy_result(),
                ) as executor:
                    status, payload = self.deploy(key=f"explicit-{purpose}")
                self.assertEqual(status, 202, payload)
                executor.assert_called_once()
