"""New storage can be prepared while the original lane remains held and unknown."""

import asyncio
from contextlib import nullcontext
import unittest
from unittest.mock import patch
from httpx2 import Response

from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.http_app import launchplane_identity_actor
from control_plane.odoo_testing_resources import GenericWebTestingResources
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    run_durable_provider_operation,
)
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.testing_separation_resources import (
    ROUTE,
    STORAGE_PROGRAM_NAME,
    ProgramLogReadStatus,
    SeparationResourcesAdapter,
    SeparationResourcesRequest,
    SeparationResourcesRefused,
    preparation_binding,
)
from tests import test_testing_event_deploy_recovery as historical
from tests import test_odoo_testing_resources as storage_program


class GenericStorageProgramTests(storage_program.TestingResourceProgramTests):
    def setUp(self) -> None:
        super().setUp()
        self.resources = GenericWebTestingResources(**self.resources.model_dump())

    def test_production_resources_and_ports_are_unchanged(self) -> None:
        production = self.root / "production-db" / "_data"
        production.mkdir(parents=True)
        data = production / "business-data"
        data.write_text("needed production data")
        self.containers["production"] = {
            "Config": {"Labels": {"com.docker.compose.project": "production"}},
            "HostConfig": {"PortBindings": {"3306/tcp": [{"HostPort": "3307"}]}},
            "Mounts": [{"Type": "volume", "Name": "production-db", "Source": str(production)}],
        }
        before = self.containers.copy()
        self.prepare()
        self.assertEqual(data.read_text(), "needed production data")
        self.assertEqual(self.containers, before)
        self.assertEqual(set(self.volumes), set(self.resources.volumes.values()))


class SeparationResourceTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = historical.TestingEventDeployRecoveryTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.root = self.fixture.store, self.fixture.root
        config = patch(
            "control_plane.dokploy.source.read_dokploy_config",
            return_value=("https://provider.test", "fixture-token"),
        )
        config.start()
        self.addCleanup(config.stop)
        if self.store.database_dialect_name != "postgresql":
            # Unit/rehearsal adapter tests; the HTTP route refuses SQLite apply.
            # The inherited PostgreSQL inventory runs the real guard below.
            guard = patch.object(
                self.store, "serialize_disposable_testing_resources", side_effect=nullcontext
            )
            guard.start()
            self.addCleanup(guard.stop)
        self.product, self.context = self.fixture.profile.product, self.fixture.context
        self.actor = launchplane_identity_actor(
            LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
        )
        self.original = self.fixture.reserve(phase="deploy_trigger")
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        # Omitted purpose reproduces the retained legacy record. Its prose is
        # deliberately a real staff hold: no reason-string inference is allowed.
        target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            reason="Staff are using the old lane; never change it.", recorded_by=self.actor
        )
        self.store.write_dokploy_target_record(target)
        self.retained = target
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "instance": "prod",
                    "policies": target.policies.model_copy(update={"staff_testing_hold": None}),
                    "env": {"PRODUCTION_PORT": "3307", "PRODUCTION_VOLUME": "business-db"},
                }
            )
        )
        self.production = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="prod"
        )
        binding, _ = preparation_binding(
            self.store, self.product, self.context, self.actor, self.root
        )
        self.body = SeparationResourcesRequest(
            context=self.context,
            mode="apply",
            reason="Prepare new independent storage without disturbing staff.",
            reviewed_plan_sha256=binding,
        )
        self.adapter = SeparationResourcesAdapter(
            self.root, self.store, self.product, self.body, self.actor, "launchplane_req_fixture"
        )
        self.effects: list[str] = []

    def run_preparation(self, key: str = "prepare") -> DurableProviderOperationResult:
        return run_durable_provider_operation(
            store=self.store,
            scope="fixture-director-agent",
            route_path=ROUTE.replace("{product}", self.product),
            idempotency_key=key,
            request_fingerprint="a" * 64,
            lease_owner="fixture-worker",
            response_trace_id="launchplane_req_fixture",
            adapter=self.adapter,
        )

    def prepare(self, **kwargs: object) -> dict[str, str]:
        callback = kwargs["before_effect"]
        assert callable(callback)
        callback("prepare_new_storage")
        self.effects.append("new empty storage")
        resources = kwargs["resources"]
        assert isinstance(resources, GenericWebTestingResources)
        return {
            "testing_resources_ok": "true",
            "testing_resources_binding_sha256": resources.binding_sha256,
        }

    def assert_retained(self) -> None:
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            self.retained,
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            self.production,
        )
        self.assertIn(self.original, self.store.list_held_provider_target_reservations())
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    def test_legacy_staff_hold_and_unknown_deploy_survive_preparation_and_replay(self) -> None:
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=self.prepare,
        ):
            first = self.run_preparation()
            replay = self.run_preparation()
        self.assertEqual(first.response_status_code, 202)
        self.assertEqual(replay.response_status_code, 202)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assert_retained()

    def test_lost_reply_retains_both_fences_and_never_redispatches(self) -> None:
        def lost(**kwargs: object) -> dict[str, str]:
            if kwargs.get("mode") == "readback":
                raise OSError("Remote readback is unavailable")
            self.prepare(**kwargs)
            raise OSError("Lost remote schedule outcome")

        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule", side_effect=lost
        ):
            for key in ("prepare", "prepare", "new-key"):
                self.assertEqual(self.run_preparation(key).response_status_code, 409)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assertEqual(len(self.store.list_held_provider_target_reservations()), 2)
        self.assert_retained()

    def test_original_key_adopts_only_actual_complete_storage_readback(self) -> None:
        def lost_then_read(**kwargs: object) -> dict[str, str]:
            if kwargs.get("mode") == "readback":
                resources = kwargs["resources"]
                assert isinstance(resources, GenericWebTestingResources)
                return {
                    "testing_resources_ok": "true",
                    "testing_resources_binding_sha256": resources.binding_sha256,
                }
            self.prepare(**kwargs)
            raise OSError("Lost completed schedule response")

        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=lost_then_read,
        ):
            self.assertEqual(self.run_preparation().response_status_code, 409)
            recovered = self.run_preparation()
            self.assertEqual(recovered.response_status_code, 202)
            replay = self.run_preparation()
            self.assertEqual(replay.response_status_code, 202)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))
        self.assert_retained()

    def test_foreign_hold_and_production_alias_refuse_before_effect(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        hold = self.retained.policies.staff_testing_hold
        assert hold is not None
        changed = self.retained.model_copy(deep=True)
        changed.policies.staff_testing_hold = hold.model_copy(
            update={"recorded_by": "another-agent"}
        )
        self.store.write_dokploy_target_record(changed)
        with self.assertRaises(ValueError):
            self.adapter.plan()
        self.store.write_dokploy_target_record(self.retained)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.context,
                instance="prod",
                target_id=self.fixture.target.resolved_target.target_id,
                updated_at=self.fixture.now(),
            )
        )
        with self.assertRaises(ValueError):
            self.adapter.plan()
        self.assertEqual(self.effects, [])

    def test_changed_authority_after_dispatch_is_unknown_and_preserves_original(self) -> None:
        def drift(**kwargs: object) -> dict[str, str]:
            result = self.prepare(**kwargs)
            changed = self.retained.model_copy(deep=True)
            changed.policies.staff_testing_hold = None
            self.store.write_dokploy_target_record(changed)
            return result

        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule", side_effect=drift
        ):
            for _ in range(2):
                self.assertEqual(self.run_preparation().response_status_code, 409)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assertIn(self.original, self.store.list_held_provider_target_reservations())

    def test_unknown_provider_inventory_refuses_without_any_schedule(self) -> None:
        self.fixture.responses["/api/deployment.queueList"] = {"items": [], "hasMore": True}
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule"
        ) as schedule:
            with self.assertRaises(SeparationResourcesRefused):
                self.run_preparation()
            schedule.assert_not_called()
        self.assert_retained()

    def test_retired_product_cannot_allocate_resources(self) -> None:
        profile = self.store.read_product_profile_record(self.product)
        retired = profile.model_copy(deep=True)
        retired.lifecycle_state = "retired"
        retired.preview.enabled = False
        self.store.write_product_profile_record(retired)
        with self.assertRaises(SeparationResourcesRefused):
            self.adapter.plan()

    def test_empty_storage_carries_no_production_credential(self) -> None:
        from control_plane.contracts.secret_record import SecretRecord, SecretBinding
        from control_plane.contracts.runtime_key_safety_policy import (
            RuntimeKeySafetyPolicyRecord,
            RuntimeSecretSafetyRule,
        )
        from control_plane.live_target_runtime import (
            evaluate_runtime_key_safety_for_live_target_sync,
            LiveTargetRuntimeError,
        )

        timestamp = self.fixture.now()
        secret = SecretRecord(
            secret_id="production-only",
            scope="context_instance",
            integration="runtime_environment",
            name="SYNC_DB_PASSWORD",
            context=self.context,
            instance="prod",
            current_version_id="production-version",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.store.write_secret_record(secret)
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="shared-binding",
                secret_id=secret.secret_id,
                integration="runtime_environment",
                binding_key="SYNC_DB_PASSWORD",
                context=self.context,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="production-only-policy",
                source="fixture",
                updated_at=timestamp,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="SYNC_DB_PASSWORD", secret_class="prod_only"
                    ),
                ),
            )
        )
        self.body.reviewed_plan_sha256 = preparation_binding(
            self.store, self.product, self.context, self.actor, self.root
        )[0]
        with self.assertRaises(LiveTargetRuntimeError):
            evaluate_runtime_key_safety_for_live_target_sync(
                record_store=self.store, context_name=self.context, instance_name="testing"
            )
        before = self.store.list_secret_bindings()
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=self.prepare,
        ):
            result = self.run_preparation()
        self.assertEqual(result.response_status_code, 202)
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertFalse(payload["credentials_installed"])
        self.assertEqual(self.store.list_secret_bindings(), before)
        self.assertEqual(self.store.read_secret_record(secret.secret_id), secret)
        self.assert_retained()

    def test_existing_preparation_schedule_is_never_overwritten_or_redispatched(self) -> None:
        from control_plane.testing_separation_resources import run_preparation_schedule
        from unittest.mock import Mock

        resources = GenericWebTestingResources(
            product=self.product,
            context=self.context,
            operation_id="fixture",
            target_id=self.fixture.target.resolved_target.target_id,
            namespace="lp-testing-" + "a" * 32,
        )
        checkpoint = Mock()
        with (
            patch(
                "control_plane.testing_separation_resources._find_storage_schedule",
                return_value={"scheduleId": "retained-preparation"},
            ),
            patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as schedule,
        ):
            with self.assertRaises(ValueError):
                run_preparation_schedule(
                    root=self.root, resources=resources, before_effect=checkpoint
                )
        checkpoint.assert_not_called()
        schedule.assert_not_called()
        self.assert_retained()

    def test_readback_schedule_requires_terminal_original_provenance_and_never_repeats_creation(
        self,
    ) -> None:
        from control_plane.testing_separation_resources import run_preparation_schedule
        from unittest.mock import Mock

        resources = self.adapter.resources(self.body.reviewed_plan_sha256)
        schedules = []

        def run(**kwargs: object) -> dict[str, str]:
            payload = kwargs["schedule_payload"]
            assert isinstance(payload, dict)
            schedules.append(payload.copy())
            self.fixture.responses["/api/schedule.list"] = [
                {**payload, "scheduleId": "fixture-preparation"}
            ]
            return {
                "testing_resources_ok": "true",
                "testing_resources_binding_sha256": resources.binding_sha256,
            }

        with patch("control_plane.dokploy.post_deploy.run_manual_data_schedule", side_effect=run):
            run_preparation_schedule(root=self.root, resources=resources, before_effect=Mock())
        history = {
            "scheduleId": "fixture-preparation",
            "deploymentId": "fixture-creation",
            "status": "done",
            "startedAt": self.fixture.now(-10),
            "finishedAt": self.fixture.now(-5),
        }
        for status in ("done", "error"):
            self.fixture.responses["/api/deployment.allByType"] = [{**history, "status": status}]
            with patch(
                "control_plane.dokploy.post_deploy.run_manual_data_schedule",
                return_value={
                    "schedule_id": "fixture-readback",
                    "testing_resources_ok": "true",
                    "testing_resources_binding_sha256": resources.binding_sha256,
                },
            ) as reader:
                result = run_preparation_schedule(
                    root=self.root, resources=resources, mode="readback", before_effect=Mock()
                )
                self.assertEqual(
                    reader.call_args.kwargs["schedule_payload"]["script"],
                    resources.script("readback"),
                )
                self.assertEqual(
                    result["testing_resources_binding_sha256"], resources.binding_sha256
                )
                receipt = self.adapter.result(resources, result)
                self.assertEqual(receipt["schedule_id"], history["scheduleId"])
                self.assertEqual(receipt["readback_schedule_id"], result["schedule_id"])
        self.assertEqual(len(schedules), 1)
        for invalid in (
            [],
            {"items": [], "hasMore": True},
            [{**history, "status": "running"}],
            [history, history],
            [{**history, "scheduleId": "other-schedule"}],
            [{**history, "finishedAt": None}],
        ):
            self.fixture.responses["/api/deployment.allByType"] = invalid
            with (
                self.subTest(history=invalid),
                patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as reader,
            ):
                with self.assertRaises(ValueError):
                    run_preparation_schedule(
                        root=self.root, resources=resources, mode="readback", before_effect=Mock()
                    )
                reader.assert_not_called()
        self.fixture.responses["/api/deployment.allByType"] = [history]
        original = self.fixture.responses["/api/schedule.list"]
        assert isinstance(original, list) and isinstance(original[0], dict)
        for change in ({"script": resources.script("readback")}, {"enabled": True}):
            self.fixture.responses["/api/schedule.list"] = [{**original[0], **change}]
            with patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as reader:
                with self.assertRaises(ValueError):
                    run_preparation_schedule(
                        root=self.root, resources=resources, mode="readback", before_effect=Mock()
                    )
                reader.assert_not_called()
        self.fixture.responses["/api/schedule.list"] = [
            original[0],
            {
                **original[0],
                "name": str(original[0]["name"]) + " readback",
                "script": resources.script("readback"),
                "scheduleId": "active-readback",
            },
        ]

        def read(**kwargs: object) -> object:
            if kwargs["path"] == "/api/deployment.allByType":
                query = kwargs["query"]
                assert isinstance(query, dict)
                return (
                    [history]
                    if query["id"] == history["scheduleId"]
                    else [{**history, "scheduleId": query["id"], "status": "running"}]
                )
            return self.fixture.read(**kwargs)

        with (
            patch("control_plane.dokploy.api.dokploy_request", side_effect=read),
            patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as reader,
        ):
            with self.assertRaises(ValueError):
                run_preparation_schedule(
                    root=self.root, resources=resources, mode="readback", before_effect=Mock()
                )
            reader.assert_not_called()
        self.assert_retained()

    def test_absent_effect_checkpoint_is_not_provider_absence_proof(self) -> None:
        with patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as dispatch:
            observed = self.adapter.observe(
                "original-operation", "", self.body.reviewed_plan_sha256
            )
        self.assertEqual(observed.outcome, "unknown")
        dispatch.assert_not_called()
        self.assert_retained()

    def test_native_inspection_reads_only_correlated_program_markers(self) -> None:
        resources = self.adapter.resources(self.body.reviewed_plan_sha256, "original-operation")
        self.fixture.responses["/api/schedule.list"] = [
            {
                "scheduleId": "original-storage",
                "name": STORAGE_PROGRAM_NAME,
                "appName": resources.namespace,
                "script": resources.script("prepare"),
                "enabled": False,
                "private": "fixture-private-output",
            }
        ]
        self.fixture.responses["/api/deployment.allByType"] = [
            {
                "scheduleId": "original-storage",
                "deploymentId": "original-job",
                "status": "error",
                "startedAt": self.fixture.now(),
                "finishedAt": self.fixture.now(),
            }
        ]
        with (
            patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as dispatch,
            patch("control_plane.dokploy.api.upsert_dokploy_schedule") as upsert,
            patch(
                "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers",
                return_value={
                    "log_available": "true",
                    "testing_resources_ok": "false",
                    "testing_resources_binding_sha256": "wrong-fixture-binding",
                    "private": "fixture-private-output",
                },
            ) as markers,
        ):
            programs = self.adapter.inspect_original("original-operation")["programs"]
        self.assertEqual(programs[0].deployment_id, "original-job")
        self.assertEqual(programs[0].deployment_status, "error")
        self.assertIs(programs[0].resources_ok, False)
        self.assertIs(programs[0].result_binding_matches, False)
        self.assertTrue(programs[0].script_matches)
        self.assertTrue(programs[0].disabled)
        self.assertEqual(programs[0].log_read_status, ProgramLogReadStatus.AVAILABLE)
        self.assertEqual(programs[1].status, "missing")
        self.assertNotIn("fixture-private-output", str(programs))
        self.assertNotIn(resources.script("prepare"), str(programs))
        markers.assert_called_once()
        dispatch.assert_not_called()
        upsert.assert_not_called()
        self.assert_retained()

    def test_inspection_reads_original_namespace_after_current_input_drift(self) -> None:
        changed = self.retained.model_copy(deep=True)
        changed.env["CHANGED_AFTER_ORIGINAL"] = "fixture"
        self.store.write_dokploy_target_record(changed)
        self.fixture.responses["/api/schedule.list"] = []
        with patch.object(self.store, "testing_replacement_execution_lock") as execution_lock:
            observed = self.adapter.inspect_original("original-operation")
        self.assertIs(observed["inputs_match"], False)
        self.assertTrue(observed["programs"])
        execution_lock.assert_not_called()
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            changed,
        )

    def test_plan_provider_reads_leave_the_http_event_loop(self) -> None:
        import threading

        main_thread = threading.get_ident()
        threads = []
        plan = self.adapter.plan()

        def read() -> dict[str, object]:
            threads.append(threading.get_ident())
            return plan

        with patch.object(SeparationResourcesAdapter, "plan", side_effect=read):
            result = asyncio.run(self.call_http(mode="dry-run"))
        self.assertEqual(result.status_code, 202, result.text)
        self.assertTrue(threads)
        self.assertTrue(all(thread != main_thread for thread in threads))
        import click

        with patch.object(
            SeparationResourcesAdapter,
            "plan",
            side_effect=click.ClickException("private-provider-fixture"),
        ):
            unavailable = asyncio.run(self.call_http(mode="dry-run"))
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertNotIn("private-provider-fixture", unavailable.text)
        from control_plane.runtime_platform_credentials import (
            PlatformCredentialRefusedError,
            PlatformCredentialFinding,
        )

        protective = PlatformCredentialRefusedError(
            target="private-target-fixture",
            findings=(
                PlatformCredentialFinding(key="DOKPLOY_TOKEN", reason="platform_credential_key"),
            ),
        )
        with patch.object(SeparationResourcesAdapter, "plan", side_effect=protective):
            refused = asyncio.run(self.call_http(mode="dry-run"))
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertNotIn("private-target-fixture", refused.text)
        self.assert_retained()

    def test_native_inspection_never_treats_missing_or_incomplete_history_as_a_result(self) -> None:
        resources = self.adapter.resources(self.body.reviewed_plan_sha256, "original-operation")
        schedule = {
            "scheduleId": "original-storage",
            "name": STORAGE_PROGRAM_NAME,
            "appName": resources.namespace,
            "script": resources.script("prepare"),
            "enabled": False,
        }
        self.fixture.responses["/api/schedule.list"] = [schedule]
        history = {
            "scheduleId": schedule["scheduleId"],
            "deploymentId": "original-job",
            "status": "done",
            "startedAt": self.fixture.now(),
            "finishedAt": self.fixture.now(),
        }
        for rows in (
            [],
            [history, history],
            [{**history, "scheduleId": "foreign"}],
            [{**history, "status": "running"}],
            [{**history, "finishedAt": None}],
            [{**history, "deploymentId": "private/invalid-identifier"}],
            {"rows": [history], "hasMore": True},
        ):
            with self.subTest(history=rows):
                self.fixture.responses["/api/deployment.allByType"] = rows
                with patch(
                    "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers"
                ) as markers:
                    programs = self.adapter.inspect_original("original-operation")["programs"]
                markers.assert_not_called()
                self.assertIsNone(programs[0].resources_ok)
                self.assertNotIn("private/invalid-identifier", str(programs))
        for schedule_rows in (
            [],
            [schedule, schedule],
            [{**schedule, "enabled": True}],
            [{**schedule, "script": "foreign-private-program"}],
            {"rows": [schedule], "hasMore": True},
            [None],
        ):
            with self.subTest(schedules=schedule_rows):
                self.fixture.responses["/api/schedule.list"] = schedule_rows
                with patch(
                    "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers"
                ) as markers:
                    programs = self.adapter.inspect_original("original-operation")["programs"]
                markers.assert_not_called()
                self.assertIsNone(programs[0].resources_ok)
                self.assertNotIn("foreign-private-program", str(programs))
        self.assert_retained()

    def test_application_inspection_separates_original_capability_and_storage_programs(
        self,
    ) -> None:
        from control_plane.contracts.canonical_json import canonical_json_sha256
        from control_plane.odoo_testing_resources import ApplicationTestingResources
        from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS
        from control_plane.testing_application_capabilities import (
            RETAINED_CAPABILITY_PROGRAM_NAME,
            retained_application_capability_program,
        )
        from control_plane.testing_separation_inspection import inspect_preparation_programs

        resources = ApplicationTestingResources(
            product=self.product,
            context=self.context,
            target_id=self.fixture.target.resolved_target.target_id,
            operation_id=self.body.reviewed_plan_sha256,
            namespace="lp-testing-" + "a" * 32,
            state_kind="volume",
        )
        spec: dict[str, object] = {"application_id": resources.target_id}
        current = retained_application_capability_program(spec)
        line = next(
            row for row in current.splitlines() if row.startswith("PLATFORM_CREDENTIAL_KEYS=")
        )
        historical = current.replace(
            line,
            "PLATFORM_CREDENTIAL_KEYS=frozenset({"
            + ", ".join(map(repr, sorted(PLATFORM_CREDENTIAL_KEYS, reverse=True)))
            + "})",
        )
        self.fixture.payload["applicationId"] = resources.target_id
        self.fixture.responses["/api/schedule.list"] = [
            {
                "name": RETAINED_CAPABILITY_PROGRAM_NAME,
                "appName": resources.namespace,
                "scheduleId": "original-capability",
                "script": historical,
                "enabled": False,
            }
        ]
        self.fixture.responses["/api/deployment.allByType"] = [
            {
                "scheduleId": "original-capability",
                "deploymentId": "original-job",
                "status": "done",
                "startedAt": self.fixture.now(),
                "finishedAt": self.fixture.now(),
            }
        ]
        with (
            patch(
                "control_plane.testing_application_capabilities.retained_application_capability_spec",
                return_value=spec,
            ),
            patch(
                "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers",
                return_value={
                    "log_available": "true",
                    "testing_resources_ok": "true",
                    "testing_resources_binding_sha256": canonical_json_sha256(spec),
                },
            ),
            patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as dispatch,
        ):
            programs = inspect_preparation_programs(
                root=self.root, resources=resources, store=self.store, application=True
            )
        self.assertTrue(programs[0].resources_ok)
        self.assertTrue(programs[0].result_binding_matches)
        self.assertEqual(programs[0].schedule_id, "original-capability")
        self.assertTrue(all(item.status == "missing" for item in programs[1:]))
        self.assertTrue(any(item.kind == "storage_preparation" for item in programs))
        dispatch.assert_not_called()
        for suffix, kind in (
            ("", "retained_capability"),
            (" readback", "retained_capability_readback"),
        ):
            self.fixture.responses["/api/schedule.list"][0].update(
                name=RETAINED_CAPABILITY_PROGRAM_NAME + suffix,
                script=historical.replace("set -euo pipefail", "set -euO pipefail", 1),
            )
            with (
                self.subTest(kind=kind),
                patch(
                    "control_plane.testing_application_capabilities.retained_application_capability_spec",
                    return_value=spec,
                ),
                patch(
                    "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers"
                ) as markers,
            ):
                changed = inspect_preparation_programs(
                    root=self.root, resources=resources, store=self.store, application=True
                )
            observed = next(item for item in changed if item.kind == kind)
            self.assertFalse(observed.script_matches)
            self.assertIsNone(observed.resources_ok)
            markers.assert_not_called()
        with patch(
            "control_plane.testing_application_capabilities.retained_application_capability_spec",
            side_effect=ValueError("fixture unavailable current capability"),
        ):
            incomplete = inspect_preparation_programs(
                root=self.root, resources=resources, store=self.store, application=True
            )
        self.assertTrue(any(item.kind == "storage_preparation" for item in incomplete))
        self.assert_retained()

    def test_provider_placement_drift_refuses_before_any_new_storage(self) -> None:
        self.fixture.payload["serverId"] = "different-server"
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule"
        ) as schedule:
            with self.assertRaises(SeparationResourcesRefused):
                self.run_preparation()
            schedule.assert_not_called()
        self.assert_retained()

    def test_inspection_uses_readback_history_rules_and_reports_marker_read_status(self) -> None:
        from control_plane.testing_separation_resources import SeparationProgramInspection
        from control_plane.testing_separation_inspection import inspect_program_history
        from control_plane.testing_program_evidence import classify_program_history

        first = {
            "scheduleId": "fixture-history",
            "deploymentId": "first-job",
            "status": "done",
            "startedAt": self.fixture.now(),
            "finishedAt": self.fixture.now(),
        }
        for kind, original in (
            ("storage_preparation", True),
            ("retained_capability", True),
            ("storage_readback", False),
            ("retained_capability_readback", False),
        ):
            for second_status in ("done", "running"):
                rows = [first, {**first, "deploymentId": "second-job", "status": second_status}]
                observed = SeparationProgramInspection.model_validate(
                    {
                        "kind": kind,
                        "status": "unavailable",
                        "schedule_id": "fixture-history",
                        "script_matches": True,
                        "disabled": True,
                    }
                )
                expected = classify_program_history(
                    rows, schedule_id=observed.schedule_id, original=original
                )
                with (
                    self.subTest(kind=kind, second_status=second_status),
                    patch("control_plane.dokploy.api.dokploy_request", return_value=rows),
                    patch(
                        "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers"
                    ) as markers,
                ):
                    inspect_program_history(
                        host="https://provider.invalid",
                        token="fixture",
                        observed=observed,
                        binding=None,
                    )
                self.assertEqual(observed.history_status, expected.status)
                self.assertEqual(observed.history_count, len(rows))
                self.assertFalse(observed.deployment_id)
                self.assertIsNone(observed.resources_ok)
                self.assertEqual(observed.log_read_status, ProgramLogReadStatus.NOT_ATTEMPTED)
                markers.assert_not_called()
        for finished_at in (None, "2000-01-01T00:00:00Z"):
            untimed_rows = [{**first, "finishedAt": finished_at}]
            observed = SeparationProgramInspection(
                kind="storage_preparation",
                status="unavailable",
                schedule_id="fixture-history",
                script_matches=True,
                disabled=True,
            )
            expected = classify_program_history(
                untimed_rows, schedule_id=observed.schedule_id, original=True
            )
            with (
                patch("control_plane.dokploy.api.dokploy_request", return_value=untimed_rows),
                patch(
                    "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers"
                ) as markers,
            ):
                inspect_program_history(
                    host="https://provider.invalid",
                    token="fixture",
                    observed=observed,
                    binding=None,
                )
            self.assertEqual(observed.history_status, expected.status)
            self.assertIsNone(observed.resources_ok)
            self.assertEqual(observed.log_read_status, ProgramLogReadStatus.NOT_ATTEMPTED)
            markers.assert_not_called()
        statuses = []
        for available in (False, True):
            observed = SeparationProgramInspection(
                kind="storage_preparation",
                status="unavailable",
                schedule_id="fixture-history",
                script_matches=True,
                disabled=True,
            )
            with (
                patch("control_plane.dokploy.api.dokploy_request", return_value=[first]),
                patch(
                    "control_plane.dokploy.post_deploy._read_odoo_post_deploy_log_markers",
                    return_value={"log_available": str(available).lower()},
                ) as markers,
            ):
                inspect_program_history(
                    host="https://provider.invalid",
                    token="fixture",
                    observed=observed,
                    binding=None,
                )
            statuses.append(observed.log_read_status)
            self.assertIsNone(observed.resources_ok)
            markers.assert_called_once()
        self.assertEqual(
            statuses, [ProgramLogReadStatus.UNAVAILABLE, ProgramLogReadStatus.AVAILABLE]
        )
        self.assert_retained()

    async def call_http(self, *, key: str = "http-prepare", **changes: object) -> Response:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import BearerIdentityConfig
        from tests.support.auth import (
            StubVerifier,
            identity,
            local_operator_policy,
        )
        from tests.support.http import request

        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=local_operator_policy(actions=()),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

        return await request(
            app,
            "POST",
            ROUTE.replace("{product}", self.product),
            headers={"Authorization": "Bearer fixture", "Idempotency-Key": key},
            payload={**self.body.model_dump(mode="json"), **changes},
        )

    def test_http_plan_and_apply_use_the_same_identity_and_preserve_replay(self) -> None:
        from tests.support.auth import scoped_agent_transport

        call = self.call_http

        plan = asyncio.run(call(mode="dry-run", reviewed_plan_sha256=""))
        self.assertEqual(plan.status_code, 202, plan.text)
        with scoped_agent_transport():
            denied = asyncio.run(call(mode="dry-run"))
        self.assertEqual(denied.status_code, 403, denied.text)
        for injected in (
            {"instance": "prod"},
            {"target_id": "production"},
            {"volumes": {"database": "production-db"}},
            {"credentials": {"password": "production-key"}},
            {"deploy": True},
        ):
            with self.subTest(injected=tuple(injected)):
                rejected = asyncio.run(call(mode="dry-run", **injected))
                self.assertEqual(rejected.status_code, 400, rejected.text)
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=self.prepare,
        ):
            result = asyncio.run(call())
            if self.store.database_dialect_name == "postgresql":
                self.assertEqual(result.status_code, 202, result.text)
                replay = asyncio.run(call())
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertEqual(self.effects, ["new empty storage"])
                stored = result.json()["result"]
                changed = self.retained.model_copy(deep=True)
                changed.env["NEW_CONFIG"] = "different"
                self.store.write_dokploy_target_record(changed)
                receipt = asyncio.run(
                    call(mode="receipt", reason="Read stored original preparation.")
                )
                self.assertEqual(receipt.status_code, 202, receipt.text)
                self.assertEqual(receipt.json()["result"], stored)
                self.store.write_dokploy_target_record(self.retained)
            else:
                self.assertEqual(result.status_code, 503, result.text)
                self.assertFalse(self.effects)
        self.assert_retained()

    def test_http_pending_status_preserves_its_original_reservation(self) -> None:
        call = self.call_http
        missing = asyncio.run(call(mode="receipt", key="http-status"))
        self.assertEqual(missing.status_code, 202, missing.text)
        self.assertEqual(missing.json()["result"]["state"], "not_found")
        no_key = asyncio.run(call(mode="receipt", key=""))
        self.assertEqual(no_key.status_code, 409, no_key.text)
        from control_plane.http_routes.mutation_support import idempotency_scope

        scope = idempotency_scope(
            LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
        )
        reserved = self.store.reserve_mutation(
            scope=scope,
            route_path=ROUTE.format(product=self.product),
            idempotency_key="http-status",
            request_fingerprint="b" * 64,
            lease_owner="launchplane_req_original",
            lease_seconds=300,
            reconciliation_key=self.body.reviewed_plan_sha256,
            provider_target_key=self.adapter.target_key(),
        ).record
        assert reserved is not None
        checkpointed = self.store.checkpoint_mutation_provider_effect(
            reservation=reserved, effect_phase="inspect_retained"
        ).record
        assert checkpointed is not None
        reserved = checkpointed
        for uncertain in (False, True):
            if uncertain:
                updated = self.store.mark_mutation_reconcile_required(
                    reservation=reserved,
                    reconciliation_key=reserved.reconciliation_key,
                ).record
                assert updated is not None
                reserved = updated
            with (
                patch.object(SeparationResourcesAdapter, "observe") as observe,
                patch.object(SeparationResourcesAdapter, "binding") as binding,
            ):
                pending = asyncio.run(call(mode="receipt", key="http-status"))
            self.assertEqual(pending.status_code, 202, pending.text)
            result = pending.json()["result"]
            self.assertEqual(result["state"], reserved.state)
            self.assertEqual(result["lease_expires_at"], reserved.lease_expires_at)
            self.assertEqual(result["plan_sha256"], reserved.reconciliation_key)
            self.assertEqual(result["provider_effect_phase"], reserved.provider_effect_phase)
            self.assertEqual(
                result["provider_effect_started_at"], reserved.provider_effect_started_at
            )
            self.assertNotIn("applied", result)
            self.assertNotIn(reserved.request_fingerprint, pending.text)
            observe.assert_not_called()
            binding.assert_not_called()
            self.assertEqual(
                self.store.read_idempotency_record(
                    scope=scope,
                    route_path=ROUTE.format(product=self.product),
                    idempotency_key="http-status",
                ),
                reserved,
            )
        foreign = asyncio.run(call(mode="receipt", key="http-status", context="foreign"))
        self.assertEqual(foreign.status_code, 409, foreign.text)
        self.assert_retained()

    def test_original_key_inspection_checks_request_and_never_changes_its_reservation(self) -> None:
        from contextlib import redirect_stdout
        import io
        import json
        from pathlib import Path
        import runpy
        import threading
        from pydantic import ValidationError
        from control_plane.testing_separation_resources import (
            SeparationResourcesResult,
            SeparationResourcesInspection,
        )
        from control_plane.http_routes.mutation_support import (
            idempotency_scope,
            request_fingerprint,
        )

        scope = idempotency_scope(
            LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
        )
        fingerprint = request_fingerprint(
            {"product": self.product, **self.body.model_dump(mode="json")}
        )
        reserved = self.store.reserve_mutation(
            scope=scope,
            route_path=ROUTE.format(product=self.product),
            idempotency_key="original-inspection",
            request_fingerprint=fingerprint,
            lease_owner="original-worker",
            lease_seconds=300,
            reconciliation_key=self.body.reviewed_plan_sha256,
            provider_target_key=self.adapter.target_key(),
        ).record
        assert reserved is not None
        programs = [{"kind": "storage_preparation", "status": "missing"}]
        main_thread = threading.get_ident()
        threads = []

        def inspect_programs(_key: str) -> dict[str, object]:
            threads.append(threading.get_ident())
            return {"programs": programs, "inputs_match": True}

        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/stage-testing-separation.py")
        )
        helper = self.root / "launchplane-write-action.py"
        helper.touch()
        review = self.root / "original-review.json"
        receipt = self.root / "original-review.json.receipt.json"
        review_content, receipt_content = "original review", "original receipt"
        review.write_text(review_content)
        receipt.write_text(receipt_content)
        responses = []

        def request_from_client(**kwargs: object) -> dict[str, object]:
            body = kwargs["body"]
            assert isinstance(body, dict)
            response = asyncio.run(self.call_http(key=str(kwargs["idempotency_key"]), **body))
            responses.append(response)
            payload = response.json()
            assert isinstance(payload, dict)
            return payload

        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "fixture",
            },
            "validate_service_url": lambda value: None,
            "request_launchplane": request_from_client,
            "public_trace_id": lambda value: value,
        }
        arguments = [
            "client",
            "resources-inspect",
            "--operator-helper",
            str(helper),
            "--product",
            self.product,
            "--context",
            self.context,
            "--reason",
            self.body.reason,
            "--evidence-file",
            str(review),
            "--idempotency-key",
            reserved.idempotency_key,
        ]
        with patch.object(
            SeparationResourcesAdapter, "inspect_original", side_effect=inspect_programs
        ) as inspect_original:
            output = io.StringIO()
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", arguments),
                redirect_stdout(output),
            ):
                self.assertEqual(client["main"](), 0)
            response = responses[0]
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(json.loads(output.getvalue())["retain_original_key"])
            self.assertEqual(response.json()["result"]["programs"][0]["status"], "missing")
            SeparationResourcesInspection.model_validate(response.json()["result"])
            with self.assertRaises(ValidationError):
                SeparationResourcesResult.model_validate(response.json()["result"])
            inspect_original.assert_called_once()
            self.assertTrue(all(thread != main_thread for thread in threads))
            inspect_original.reset_mock()
            for changes in ({"reason": "different request"}, {"context": "foreign"}):
                with self.subTest(changes=changes):
                    refused = asyncio.run(
                        self.call_http(key=reserved.idempotency_key, mode="inspect", **changes)
                    )
                    self.assertEqual(refused.status_code, 409, refused.text)
            inspect_original.assert_not_called()
        self.assertEqual(review.read_text(), review_content)
        self.assertEqual(receipt.read_text(), receipt_content)
        self.assertNotIn(fingerprint, response.text)
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=scope,
                route_path=ROUTE.format(product=self.product),
                idempotency_key=reserved.idempotency_key,
            ),
            reserved,
        )
        self.assert_retained()
