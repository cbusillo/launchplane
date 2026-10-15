"""Same authenticated agent observes or repairs only a qualified testing copy."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy.source import DokployTargetDefinition
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.action_effects import AGENT_READ_ROLE_ACTION, ODOO_CONNECTION_ISOLATION_ACTIONS
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalOperatorPolicyRule,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class ConnectionIsolationAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.store.write_product_profile_record(profile)
        for lane in profile.lanes:
            self.store.write_dokploy_target_record(
                DokployTargetRecord(
                    context=lane.context, instance=lane.instance, updated_at=profile.updated_at
                )
            )
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=lane.context,
                    instance=lane.instance,
                    target_id=f"fixture-{lane.instance}",
                    updated_at=profile.updated_at,
                )
            )
        self.app = self.make_app()

    def make_app(
        self,
        config: BearerIdentityConfig | None = None,
        policy: LaunchplaneAuthzPolicy | None = None,
    ) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy or LaunchplaneAuthzPolicy(),
            bearer_identity_config=config or _local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    async def call(
        self, mode: str | None, *, instance: str = "testing", key: str = "", **changes: Any
    ) -> Any:
        body: dict[str, Any] = {"context": "cm", "instance": instance}
        if mode is not None:
            body["connection_isolation"] = mode
        if mode == "apply":
            body.update(
                reviewed_dry_run=True,
                reviewed_plan_sha256="a" * 64,
                reviewed_resource_sha256="b" * 64,
                reason="Remove copied connections from the testing fixture",
            )
        body.update(changes)
        return await request(
            self.app,
            "POST",
            "/v1/drivers/odoo/post-deploy",
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
            payload={"product": "example-odoo", "post_deploy": body},
        )

    def safety(self, **changes: Any) -> None:
        record_safety(self.store, "example-odoo", "cm", "testing", **changes)

    async def test_same_director_can_observe_without_write_grant_or_disposable_facts(self) -> None:
        for mode in ("dry-run", "readback"):
            with (
                self.subTest(mode=mode),
                patch(
                    "control_plane.http_app.execute_odoo_post_deploy_result",
                    return_value=({}, {"post_deploy_status": "pass"}),
                ) as execute,
            ):
                response = await self.call(mode)
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(
                    execute.call_args.kwargs["request"].post_deploy.connection_isolation, mode
                )
        self.assertIsNone(self.store.read_disposable_lane_safety("example-odoo", "cm", "testing"))

    async def test_terminal_director_observes_but_has_no_unrelated_maintenance(self) -> None:
        self.app = self.make_app(
            BearerIdentityConfig(
                terminal_agent_token="local-operator-token",
                terminal_agent_subject="fixture-terminal",
                terminal_agent_token_label="fixture",
            )
        )
        with patch(
            "control_plane.http_app.execute_odoo_post_deploy_result",
            return_value=({}, {"post_deploy_status": "pass"}),
        ) as execute:
            self.assertEqual((await self.call("dry-run")).status_code, 202)
            execute.reset_mock()
            self.assertEqual((await self.call(None)).status_code, 403)
            execute.assert_not_called()

    async def test_generic_agent_and_unknown_token_cannot_inherit_director_repair(self) -> None:
        self.safety()
        with (
            scoped_agent_transport(),
            patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute,
        ):
            for mode in ("dry-run", "readback", "apply"):
                response = await self.call(mode, key="scoped")
                self.assertEqual(response.status_code, 403, response.text)
            execute.assert_not_called()
        self.app = self.make_app(BearerIdentityConfig())
        self.assertEqual((await self.call("dry-run")).status_code, 401)

    async def test_director_repair_uses_bounded_driver_and_replays_stable_key(self) -> None:
        self.safety()
        target = DokployTargetDefinition(
            context="cm", instance="testing", target_type="compose", target_id="fixture-testing"
        )

        def repair(**kwargs: Any) -> dict[str, str]:
            self.assertEqual(kwargs["mode"], "apply")
            kwargs["before_mutation"]()
            return {"connection_isolation_ok": "true", "integration_readback_ok": "true"}

        with (
            patch(
                "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                return_value=target,
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                return_value=("fixture", "fixture"),
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.run_connection_isolation",
                side_effect=repair,
            ) as run,
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update"
            ) as maintenance,
        ):
            first = await self.call("apply", key="reviewed-isolation")
            self.assertEqual(first.status_code, 202, first.text)
            second = await self.call("apply", key="reviewed-isolation")
            self.assertEqual(second.status_code, 202, second.text)
            run.assert_called_once()
            maintenance.assert_not_called()

    async def test_production_unknown_modes_and_maintenance_refuse_before_driver(self) -> None:
        with patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute:
            for mode in ("dry-run", "readback", "apply"):
                for instance in ("prod", "production", " PROD ", "live", "main"):
                    with self.subTest(mode=mode, instance=instance):
                        self.assertEqual(
                            (await self.call(mode, instance=instance, key="prod")).status_code, 400
                        )
            self.assertEqual((await self.call("unknown")).status_code, 400)
            self.assertEqual(
                (await self.call("apply", instance="pr-1", key="preview")).status_code, 400
            )
            self.assertEqual((await self.call(None)).status_code, 403)
            self.assertEqual((await self.call("apply")).status_code, 400)
            self.assertEqual(
                (await self.call("apply", key="unreviewed", reviewed_dry_run=False)).status_code,
                400,
            )
            execute.assert_not_called()

    async def test_recorded_protections_refuse_before_driver(self) -> None:
        with patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute:
            for field, value in (
                ("usage", "in_use"),
                ("data", "only_copy"),
                ("production_resources", "shared"),
                ("production_resources", "separation_ready"),
            ):
                with self.subTest(field=field, value=value):
                    self.safety(**{field: value})
                    response = await self.call("apply", key=f"protected-{field}-{value}")
                    self.assertEqual(response.status_code, 409, response.text)
            execute.assert_not_called()

    async def test_target_alias_and_staff_hold_refuse_before_driver(self) -> None:
        self.safety()
        target = self.store.read_dokploy_target_id_record(
            context_name="cm", instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"target_id": "fixture-prod"})
        )
        with patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute:
            self.assertEqual((await self.call("apply", key="alias")).status_code, 409)
            self.store.write_dokploy_target_id_record(target)
            record = self.store.read_dokploy_target_record(
                context_name="cm", instance_name="testing"
            )
            hold = DokployTargetStaffTestingHold(reason="Staff test in progress")
            self.store.write_dokploy_target_record(
                record.model_copy(
                    update={
                        "policies": record.policies.model_copy(update={"staff_testing_hold": hold})
                    }
                )
            )
            self.safety()
            self.assertEqual((await self.call("apply", key="hold")).status_code, 409)
            execute.assert_not_called()

    async def test_safety_is_checked_again_immediately_before_provider_mutation(self) -> None:
        self.safety()

        def changed(**kwargs: Any) -> Any:
            self.safety(usage="in_use")
            kwargs["before_connection_isolation_mutation"]()
            self.fail("Changed protection must stop the repair")

        with patch("control_plane.http_app.execute_odoo_post_deploy_result", side_effect=changed):
            response = await self.call("apply", key="changed-usage")
        self.assertEqual(response.status_code, 409, response.text)

        self.assertEqual(response.json()["error"]["code"], "disposable_lane_safety_unknown")

    async def test_keyed_readback_is_independent_and_never_replays_old_evidence(self) -> None:
        with patch(
            "control_plane.http_app.execute_odoo_post_deploy_result",
            side_effect=[({}, {"observation": "first"}), ({}, {"observation": "changed"})],
        ):
            first = await self.call("readback", key="same-read-key")
            second = await self.call("readback", key="same-read-key")
        self.assertEqual(first.json()["result"]["observation"], "first")
        self.assertEqual(second.json()["result"]["observation"], "changed")

    async def test_uncertain_apply_retains_fence_for_same_and_new_keys(self) -> None:
        from control_plane.dokploy.api import DokployScheduleExecutionFailed

        self.safety()

        def uncertain(**kwargs: Any) -> Any:
            kwargs["before_connection_isolation_mutation"]()
            raise DokployScheduleExecutionFailed(
                schedule_id="fixture",
                deployment_id="",
                deployment_status="unknown",
                cause="trigger_outcome_unknown",
            )

        with patch(
            "control_plane.http_app.execute_odoo_post_deploy_result", side_effect=uncertain
        ) as execute:
            first = await self.call("apply", key="uncertain-apply")
            self.assertEqual(first.status_code, 503, first.text)
            for key in ("uncertain-apply", "different-key"):
                response = await self.call("apply", key=key)
                self.assertEqual(response.status_code, 409, response.text)
            execute.assert_called_once()

    async def test_original_key_adopts_terminal_evidence_without_another_repair(self) -> None:
        from control_plane.dokploy import post_deploy
        from control_plane.provider_operations import (
            ProviderObservation,
            provider_operation_response_payload,
        )
        import click

        def uncertain(**kwargs: Any) -> Any:
            kwargs["before_connection_isolation_mutation"](
                post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER
            )
            raise click.ClickException("Acknowledgement lost")

        with (
            patch(
                "control_plane.http_app.execute_odoo_post_deploy_result", side_effect=uncertain
            ) as execute,
            patch("control_plane.http_app.observe_odoo_connection_isolation_result") as observe,
        ):
            first = await self.call("apply", key="recover-original")
            self.assertEqual(first.status_code, 503, first.text)
            observe.return_value = ProviderObservation(outcome="unknown")
            self.assertEqual((await self.call("apply", key="new-key")).status_code, 409)
            observe.return_value = ProviderObservation(
                outcome="present",
                response_status_code=202,
                response_payload=provider_operation_response_payload(
                    trace_id="fixture", records={}, result={"post_deploy_status": "pass"}
                ),
            )
            recovered = await self.call("apply", key="recover-original")
            self.assertEqual(recovered.status_code, 202, recovered.text)
            self.assertEqual(recovered.json()["result"]["post_deploy_status"], "pass")
            replayed = await self.call("apply", key="recover-original")
            self.assertEqual(replayed.status_code, 202, replayed.text)
            execute.assert_called_once()
            self.assertEqual(
                observe.call_args.kwargs["provider_effect_phase"],
                post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER,
            )

    async def test_explicit_isolation_apply_rule_still_protects_lane(self) -> None:
        self.app = self.make_app(
            policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-read",),
                        products=("example-odoo",),
                        contexts=("cm",),
                        instances=("testing",),
                        actions=(ODOO_CONNECTION_ISOLATION_ACTIONS["apply"],),
                    ),
                ),
            )
        )
        with (
            scoped_agent_transport(),
            patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute,
        ):
            self.safety(usage="in_use")
            response = await self.call("apply", key="granted-but-protected")
            self.assertEqual(response.status_code, 409, response.text)
            execute.assert_not_called()

    async def test_scoped_read_role_does_not_acquire_provider_execution(self) -> None:
        self.app = self.make_app(
            policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-read",),
                        products=("example-odoo",),
                        contexts=("cm",),
                        instances=("testing",),
                        actions=(AGENT_READ_ROLE_ACTION,),
                    ),
                ),
            )
        )
        with (
            scoped_agent_transport(),
            patch("control_plane.http_app.execute_odoo_post_deploy_result") as execute,
        ):
            for mode in ("dry-run", "readback"):
                response = await self.call(mode)
                self.assertEqual(response.status_code, 403, response.text)
            execute.assert_not_called()

    async def test_refused_before_effect_releases_reservation_for_valid_repair(self) -> None:
        self.safety(usage="in_use")
        refused = await self.call("apply", key="protected-repair")
        self.assertEqual(refused.json()["error"]["code"], "disposable_lane_safety_unknown")
        self.safety()

        def valid(**kwargs: Any) -> Any:
            kwargs["before_connection_isolation_mutation"]()
            return {}, {"post_deploy_status": "pass"}

        with patch("control_plane.http_app.execute_odoo_post_deploy_result", side_effect=valid):
            response = await self.call("apply", key="valid-repair")
        self.assertEqual(response.status_code, 202, response.text)

    async def test_lease_loss_before_dispatch_uses_runner_reconciliation(self) -> None:
        from control_plane.provider_operations import _ProviderOperationLeaseLostError

        self.safety()

        def execute(**kwargs: Any) -> Any:
            kwargs["before_connection_isolation_mutation"]()
            self.fail("A lost lease must prevent dispatch")

        with (
            patch("control_plane.http_app.execute_odoo_post_deploy_result", side_effect=execute),
            patch(
                "control_plane.provider_operations._ReservationHeartbeat.assert_current",
                side_effect=_ProviderOperationLeaseLostError(),
            ),
        ):
            response = await self.call("apply", key="lease-lost")
        self.assertEqual(response.status_code, 409, response.text)

        def recovered(**kwargs: Any) -> Any:
            kwargs["before_connection_isolation_mutation"]()
            return {}, {"post_deploy_status": "pass"}

        with patch(
            "control_plane.http_app.execute_odoo_post_deploy_result", side_effect=recovered
        ) as execute_again:
            response = await self.call("apply", key="lease-lost")
        self.assertEqual(response.status_code, 202, response.text)
        execute_again.assert_called_once()

    async def test_post_effect_transport_errors_are_unverified_and_retained(self) -> None:
        import click

        self.safety()

        def uncertain(**kwargs: Any) -> Any:
            kwargs["before_connection_isolation_mutation"]()
            raise click.ClickException("Provider acknowledgement missing")

        with patch(
            "control_plane.http_app.execute_odoo_post_deploy_result", side_effect=uncertain
        ) as execute:
            response = await self.call("apply", key="transport-error")
            self.assertEqual(response.status_code, 503, response.text)
            response = await self.call("apply", key="transport-error")
            self.assertEqual(response.status_code, 409, response.text)
            execute.assert_called_once()
