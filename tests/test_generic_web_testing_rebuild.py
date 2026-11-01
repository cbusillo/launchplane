from contextlib import nullcontext
from collections.abc import Callable
import base64
from datetime import UTC, datetime, timedelta
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane import generic_web_testing_rebuild as rebuild
from control_plane.dokploy import api, source
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductGenericWebTestingState,
)
from control_plane.contracts.testing_application import TestingApplicationState
from control_plane.contracts.testing_activation import TestingActivationRequest
from control_plane.contracts.testing_publication import TestingServingSnapshot
from control_plane.durable_operation_authorization import durable_operation_caller_identity
from control_plane.launchplane_reconcile_authorization import TESTING_TARGET_REPLACEMENT_ACTION
from control_plane.integration_allowances import target_record_sha256
from control_plane.provider_operations import ProviderOperationLease
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_lane_hold import apply_testing_hold_plan
from control_plane import testing_application_runtime as application_runtime
from control_plane.testing_rebuild_service import (
    GenericWebTestingRebuildIntent,
    NativeTestingRebuildPending,
    TestingRebuildRefused,
)
from tests.support.profiles import _generic_site_profile_payload


def observe_application_state_program(
    program: str, payload: dict[str, Any], command: Callable[..., subprocess.CompletedProcess[Any]]
) -> dict[str, Any]:
    output = StringIO()
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    with (
        patch.object(sys, "argv", ["program", encoded, "frame:"]),
        patch.object(subprocess, "run", side_effect=command),
        patch("sys.stdout", output),
    ):
        exec(program, {})
    result = json.loads(output.getvalue().removeprefix("frame:"))
    assert isinstance(result, dict)
    return result


class ApplicationAbsenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.nodes = [{"ID": "local", "Self": True}]
        self.services: dict[str, Any] = {}
        self.containers: dict[str, Any] = {}
        self.tasks: dict[str, Any] = {}
        self.removed: list[str] = []

    def docker(self, *arguments: str) -> str:
        match arguments:
            case ("service", "rm", identifier):
                self.removed.append(identifier)
                self.services.pop(identifier)
                return identifier
            case ("node", "ls", "--format", "json"):
                return "\n".join(json.dumps(row) for row in self.nodes)
            case ("service", "ls", "-q"):
                return "\n".join(identifier[:12] for identifier in self.services)
            case ("service", "inspect", *identifiers) | (
                "inspect",
                "--type",
                "service",
                *identifiers,
            ):
                return json.dumps(
                    [
                        row
                        for identifier in identifiers
                        for key, row in self.services.items()
                        if key.startswith(identifier)
                    ]
                )
            case ("service", "ps", "--no-trunc", "-q", _application):
                return "\n".join(self.tasks)
            case ("inspect", "--type", "task", identifier):
                return json.dumps([self.tasks[identifier]])
            case ("ps", "-aq", "--no-trunc"):
                return "\n".join(self.containers)
            case ("inspect", *identifiers):
                return json.dumps([self.containers[identifier] for identifier in identifiers])
            case _:
                raise AssertionError(arguments)

    def observe(self, mode: str = "absent") -> None:
        payload = {"application": "original-app", "service_id": "original-service", "mode": mode}

        def command(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, self.docker(*arguments[1:]), "")

        frame = observe_application_state_program(
            application_runtime.APPLICATION_RETIREMENT_PROGRAM
            if mode == "retire"
            else application_runtime.APPLICATION_STATE_PROGRAM,
            payload,
            command,
        )
        self.assertEqual(frame, {"type": "complete", **payload})

    def test_retirement_program_removes_only_the_original_stateless_service(self) -> None:
        self.services = {
            "original-service": {
                "ID": "original-service",
                "Spec": {"Name": "original-app", "TaskTemplate": {"ContainerSpec": {"Mounts": []}}},
            },
            "other": {"ID": "other", "Spec": {"Name": "other-app"}},
        }
        self.observe("retire")
        self.assertEqual(self.removed, ["original-service"])
        self.assertEqual(set(self.services), {"other"})
        self.observe("retire")
        self.assertEqual(self.removed, ["original-service"])

    def test_retirement_program_refuses_changed_identity_or_persistent_mounts(self) -> None:
        for identifier, name, mounts in (
            ("original-service", "renamed-app", []),
            ("new-service", "original-app", []),
            ("original-service", "original-app", [{"Source": "retained-data"}]),
        ):
            with self.subTest(identifier=identifier, name=name, mounts=mounts):
                self.services = {
                    identifier: {
                        "ID": identifier,
                        "Spec": {
                            "Name": name,
                            "TaskTemplate": {"ContainerSpec": {"Mounts": mounts}},
                        },
                    }
                }
                with self.assertRaises(ValueError):
                    self.observe("retire")
                self.assertEqual(self.removed, [])

    def test_stateless_proof_checks_service_tasks_and_implicit_container_mounts(self) -> None:
        service: dict[str, Any] = {
            "ID": "original-service",
            "Spec": {"Name": "original-app", "TaskTemplate": {"ContainerSpec": {"Mounts": []}}},
        }
        self.services[service["ID"]] = service
        self.observe("stateless")
        self.tasks["task"] = {
            "ID": "task",
            "ServiceID": service["ID"],
            "Spec": {"ContainerSpec": {"Mounts": [{"Source": "undeclared-state"}]}},
        }
        with self.assertRaises(ValueError):
            self.observe("stateless")
        self.tasks = {}
        self.containers["container"] = {
            "Id": "container",
            "Config": {"Labels": {"com.docker.swarm.service.id": service["ID"]}},
            "Mounts": [{"Name": "implicit-image-volume"}],
        }
        with self.assertRaises(ValueError):
            self.observe("stateless")

    def test_complete_local_absence_preserves_other_applications(self) -> None:
        self.services["other"] = {"ID": "other", "Spec": {"Name": "other-app"}}
        self.containers["other-container"] = {"Id": "other-container", "Config": {"Labels": {}}}
        self.observe()

    def test_either_original_service_identity_or_name_prevents_retirement(self) -> None:
        for identifier, name in (
            ("original-service", "renamed"),
            ("replacement-service", "original-app"),
        ):
            with self.subTest(identifier=identifier):
                self.services = {identifier: {"ID": identifier, "Spec": {"Name": name}}}
                with self.assertRaises(ValueError):
                    self.observe()

    def test_stopped_or_orphaned_original_container_prevents_retirement(self) -> None:
        for labels in (
            {"com.docker.swarm.service.id": "original-service"},
            {"com.docker.swarm.service.name": "original-app"},
        ):
            with self.subTest(labels=labels):
                self.containers = {
                    "retained": {
                        "Id": "retained",
                        "Config": {"Labels": labels},
                        "State": {"Status": "exited"},
                    }
                }
                with self.assertRaises(ValueError):
                    self.observe()

    def test_incomplete_or_nonlocal_inventory_cannot_prove_absence(self) -> None:
        for nodes in (
            [],
            [{"ID": "remote", "Self": False}],
            [*self.nodes, {"ID": "remote", "Self": False}],
        ):
            with (
                self.subTest(nodes=nodes),
                patch.object(self, "nodes", nodes),
                self.assertRaises(ValueError),
            ):
                self.observe()
        self.services["listed"] = {"ID": "different", "Spec": {"Name": "other"}}
        with self.assertRaises(ValueError):
            self.observe()
        self.services = {}
        self.containers["listed"] = {"Id": "different", "Config": {"Labels": {}}}
        with self.assertRaises(ValueError):
            self.observe()


class GenericWebTestingRebuildFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{directory / 'state.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        payload = _generic_site_profile_payload()
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.profile.lanes[0].generic_web_testing_state = ProductGenericWebTestingState(kind="none")
        self.profile = LaunchplaneProductProfileRecord.model_validate(self.profile.model_dump())
        self.store.write_product_profile_record(self.profile)
        self.context = self.profile.lanes[0].context
        stamp = datetime.now(UTC).isoformat()
        self.target = DokployTargetRecord(
            context=self.context,
            instance="testing",
            target_type="application",
            target_name="original",
            updated_at=stamp,
        )
        self.target_id = DokployTargetIdRecord(
            context=self.context,
            instance="testing",
            target_id="original-application",
            updated_at=stamp,
        )
        provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.target, target_id_record=self.target_id
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(provider)
        identity = LocalOperatorIdentity(
            subject="fixture-agent", token_label="fixture", director_agent=True
        )
        authorization = DurableOperationAuthorization(
            grant="director_testing_rebuild",
            action=TESTING_TARGET_REPLACEMENT_ACTION,
            product=self.profile.product,
            context=self.context,
            instances=("testing",),
            caller=durable_operation_caller_identity(identity),
            authorized_at=stamp,
        )
        self.operation = OdooStableTargetReplacementOperationRecord(
            schema_version=2,
            operation_id="generic-rebuild",
            product=self.profile.product,
            context=self.context,
            instance="testing",
            idempotency_key="original-key",
            idempotency_scope="fixture-scope",
            request_fingerprint="original-request",
            request=OdooStableTargetReplacementApplyRequest(
                product=self.profile.product,
                instance="testing",
                strategy="replace-and-cutover",
                data_source_mode="empty",
                allow_empty_data=True,
                artifact_id=self.profile.image.repository + "@sha256:" + "a" * 64,
                source_git_ref="a" * 40,
                reason="Fresh disposable testing copy",
            ),
            authorization=authorization,
            created_at=stamp,
            updated_at=stamp,
        )
        self.intent = GenericWebTestingRebuildIntent(
            operation_id=self.operation.operation_id,
            profile=self.profile,
            before=TestingServingSnapshot(
                target=self.target, target_id=self.target_id, provider=provider
            ),
            state=TestingApplicationState(kind="none"),
        )
        self.authorize = Mock()
        self.adapter = rebuild.GenericWebTestingRebuildAdapter(
            directory, self.store, self.operation, self.intent, self.authorize
        )
        self.lease = Mock(spec=ProviderOperationLease)
        self.execution = rebuild.GenericWebTestingRebuildExecution(self.adapter, self.lease, Mock())

    def current_target(self) -> DokployTargetRecord:
        return self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )


class GenericWebTestingRebuildTests(GenericWebTestingRebuildFixture):
    def test_external_comparison_rechecks_original_authority_before_reading_profiles(self) -> None:
        request = TestingActivationRequest(
            context=self.context,
            preparation_key=self.execution.resources_key,
            interlock_record_sha256=target_record_sha256(self.target),
            source_commit=self.operation.request.source_git_ref,
            volume_keys={},
            port_keys=(),
            reason=self.operation.request.reason,
            application=self.execution.specification,
        )
        adapter = self.execution.application_adapter(request)
        authorize = adapter.authorize_external
        assert authorize is not None
        self.assertEqual(authorize(), self.store.list_product_profile_records())
        self.authorize.side_effect = PermissionError("Original caller authority was removed")
        with patch.object(self.store, "list_product_profile_records") as read:
            with self.assertRaises(PermissionError):
                authorize()
            read.assert_not_called()

    def test_interlock_is_owned_by_the_original_operation_and_is_idempotent(self) -> None:
        self.execution.interlock(hold=True)
        held = self.current_target()
        hold = held.policies.staff_testing_hold
        assert hold is not None
        self.assertEqual(hold.recorded_by, self.execution.actor)
        self.execution.interlock(hold=True)
        self.assertEqual(self.current_target(), held)
        self.execution.interlock(hold=False)
        self.assertEqual(self.current_target().policies, self.target.policies)
        self.assertTrue(self.authorize.called)

    def test_staff_and_foreign_staging_holds_are_never_replaced(self) -> None:
        for purpose, actor in (
            ("staff_testing", self.execution.actor),
            ("isolation_staging", "someone-else"),
        ):
            with self.subTest(purpose=purpose):
                hold = DokployTargetStaffTestingHold.model_validate(
                    {"reason": "Person is testing", "purpose": purpose, "recorded_by": actor}
                )
                target = self.target.model_copy(
                    update={
                        "policies": self.target.policies.model_copy(
                            update={"staff_testing_hold": hold}
                        )
                    }
                )
                self.store.write_dokploy_target_record(target)
                with self.assertRaises((TestingRebuildRefused, NativeTestingRebuildPending)):
                    self.execution.interlock(hold=True)
                self.assertEqual(self.current_target(), target)

    def test_admission_refuses_a_target_shared_with_another_lane(self) -> None:
        for instance in ("prod", "testing", "preview"):
            with self.subTest(instance=instance):
                self.store.write_dokploy_target_id_record(
                    self.target_id.model_copy(
                        update={"context": "other-product", "instance": instance}
                    )
                )
                with self.assertRaises(NativeTestingRebuildPending):
                    self.execution.interlock(hold=True)
                self.assertEqual(self.current_target(), self.target)

    def test_retirement_requires_idle_bound_metadata_terminal_history_and_an_empty_queue(
        self,
    ) -> None:
        original = {
            "applicationId": self.target_id.target_id,
            "appName": "original",
            "serverId": "server",
            "autoDeploy": False,
            "applicationStatus": "done",
            "mounts": [],
            "previewDeployments": [],
        }
        terminal = {
            "deploymentId": "old-run",
            "applicationId": self.target_id.target_id,
            "status": "done",
        }
        active = {"data": {"applicationId": self.target_id.target_id}, "state": "active"}
        cases: list[tuple[dict[str, Any], list[dict[str, Any]], Any, list[dict[str, Any]]]] = [
            ({}, [terminal], [], []),
            ({"applicationStatus": "running"}, [terminal], [], []),
            ({"autoDeploy": True}, [terminal], [], []),
            ({"appName": "another-application"}, [terminal], [], []),
            ({"serverId": "another-server"}, [terminal], [], []),
            ({"previewDeployments": [{"previewDeploymentId": "child"}]}, [terminal], [], []),
            ({}, [{**terminal, "status": "running"}], [], []),
            ({}, [{**terminal, "status": "unknown"}], [], []),
            ({}, [terminal], [active], []),
            ({}, [terminal], {"items": []}, []),
            ({}, [terminal], [{"data": None, "state": "active"}], []),
            (
                {},
                [terminal],
                [],
                [
                    {
                        "scheduleId": "old-schedule",
                        "applicationId": self.target_id.target_id,
                        "enabled": True,
                    }
                ],
            ),
        ]
        for index, (changes, history, queue, schedules) in enumerate(cases):
            with (
                self.subTest(index=index),
                patch.object(
                    api, "fetch_dokploy_target_payload", return_value={**original, **changes}
                ),
                patch.object(api, "list_deployments_for_target", return_value=history),
                patch.object(
                    api,
                    "dokploy_request",
                    side_effect=lambda **kw: (
                        queue if kw["path"] == "/api/deployment.queueList" else schedules
                    ),
                ),
            ):
                if index == 0:
                    self.execution.require_retirement_quiescent(
                        "https://provider.test",
                        "fixture-token",
                        self.target_id.target_id,
                        "original",
                        "server",
                    )
                else:
                    with self.assertRaises(NativeTestingRebuildPending):
                        self.execution.require_retirement_quiescent(
                            "https://provider.test",
                            "fixture-token",
                            self.target_id.target_id,
                            "original",
                            "server",
                        )

    def test_disabled_predecessor_schedule_requires_complete_terminal_execution_history(
        self,
    ) -> None:
        payload = {
            "applicationId": self.target_id.target_id,
            "appName": "original",
            "serverId": "server",
            "autoDeploy": False,
            "applicationStatus": "done",
            "mounts": [],
            "previewDeployments": [],
        }
        schedule = {
            "scheduleId": "original-schedule",
            "applicationId": self.target_id.target_id,
            "enabled": False,
        }
        completed = {
            "deploymentId": "scheduled-run",
            "scheduleId": schedule["scheduleId"],
            "status": "done",
        }
        executions: Any = []

        def transport(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/deployment.queueList":
                return []
            if kwargs["path"] == "/api/schedule.list":
                self.assertEqual(
                    kwargs["query"],
                    {"id": self.target_id.target_id, "scheduleType": "application"},
                )
                return [schedule]
            self.assertEqual(kwargs["path"], "/api/deployment.allByType")
            self.assertEqual(kwargs["query"], {"id": schedule["scheduleId"], "type": "schedule"})
            return executions

        with (
            patch.object(api, "fetch_dokploy_target_payload", return_value=payload),
            patch.object(api, "list_deployments_for_target", return_value=[]),
            patch.object(api, "dokploy_request", side_effect=transport),
        ):
            for executions in ([], [completed], [{**completed, "status": "error"}]):
                with self.subTest(executions=executions):
                    self.execution.require_retirement_quiescent(
                        "https://provider.test",
                        "fixture-token",
                        self.target_id.target_id,
                        "original",
                        "server",
                    )
            for executions in (
                {"items": []},
                [{**completed, "status": "running"}],
                [{**completed, "scheduleId": "another-schedule"}],
                [{"scheduleId": schedule["scheduleId"], "status": "done"}],
            ):
                with self.subTest(executions=executions):
                    with self.assertRaises(NativeTestingRebuildPending):
                        self.execution.require_retirement_quiescent(
                            "https://provider.test",
                            "fixture-token",
                            self.target_id.target_id,
                            "original",
                            "server",
                        )

    def test_parent_executor_fences_concurrent_attempts(self) -> None:
        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            with self.assertRaises(NativeTestingRebuildPending):
                self.adapter.apply("original-provider-key", self.lease)
        self.assertEqual(self.current_target(), self.target)


class GenericWebTestingRebuildHttpTests(
    GenericWebTestingRebuildFixture, unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        super().setUp()
        from control_plane import product_reconcile
        from control_plane import testing_application_capabilities as capabilities
        from control_plane.contracts.deployment_record import DeploymentRecord
        from control_plane.contracts.deploy_target import DeployedTargetReference
        from control_plane.contracts.environment_inventory import EnvironmentInventory
        from control_plane.contracts.promotion_record import (
            ArtifactIdentityReference,
            DeploymentEvidence,
        )
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.dokploy.application_runtime import ApplicationRuntimeObservation
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
        from tests import test_product_reconcile as builds
        from tests import test_odoo_prod_promotion_operation as administrator
        from tests.http_app_test_support import (
            _github_oauth_config,
            _github_human_identity,
            _RejectingVerifier,
            _local_operator_bearer_config,
            _browser_mutation_headers,
        )

        payload = self.profile.model_dump(mode="json")
        payload.update(
            repository=builds.REPOSITORY,
            repository_id=builds.REPOSITORY_ID,
            repository_owner_id="1",
            image={"repository": builds.IMAGE_REPOSITORY},
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.store.write_repository_inventory_record(builds._inventory())
        self.github = builds.FakeGenericWebGitHub()
        self.github.add_run(30, builds.NEWEST)
        self.enterContext(
            patch.object(
                product_reconcile, "resolve_build_provenance_transport", return_value=self.github
            )
        )
        self.image = f"{builds.IMAGE_REPOSITORY}@{builds._digest(builds.NEWEST)}"
        identity = RuntimeIdentity(
            product=self.profile.product,
            context=self.context,
            instance="testing",
            deployment_record_id="original-deployment",
            artifact_id=self.image,
            image_reference=self.image,
            source_git_ref=builds.NEWEST,
        )
        deploy = DeploymentEvidence(
            target_name=self.target.target_name,
            target_type="application",
            deploy_mode="docker-image",
            status="pass",
        )
        artifact = ArtifactIdentityReference(artifact_id=self.image)
        self.store.write_deployment_record(
            DeploymentRecord(
                record_id=identity.deployment_record_id,
                artifact_identity=artifact,
                context=self.context,
                instance="testing",
                source_git_ref=builds.NEWEST,
                deployed_target=DeployedTargetReference(
                    provider_id="dokploy",
                    target_category="application",
                    target_id=self.target_id.target_id,
                    display_name=self.target.target_name,
                    provider_target_type="application",
                ),
                runtime_identity=identity,
                deploy=deploy,
            )
        )
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context=self.context,
                instance="testing",
                artifact_identity=artifact,
                source_git_ref=builds.NEWEST,
                deploy=deploy,
                runtime_identity=identity,
                updated_at=self.target.updated_at,
                deployment_record_id=identity.deployment_record_id,
            )
        )
        runtime = ApplicationRuntimeObservation(
            application_id=self.target_id.target_id,
            app_name=self.target.target_name,
            service_id="original-service",
            service_image=self.image,
            service_deployment_record_id=identity.deployment_record_id,
            desired_replicas=1,
            rollout_state="completed",
            rollout_started_at="",
            running_container_images=(),
            running_container_deployment_record_ids=(),
            running_container_health=(),
            pending_task_count=0,
            server_id="fixture-server",
        )
        self.enterContext(
            patch.object(capabilities, "fetch_application_runtime", return_value=runtime)
        )
        self.provider_payload = {
            "applicationId": self.target_id.target_id,
            "appName": self.target.target_name,
            "serverId": "fixture-server",
            "env": "",
            "mounts": [],
            "previewDeployments": [],
            "environment": {"env": "", "project": {"env": ""}},
        }
        self.enterContext(
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            )
        )
        self.enterContext(
            patch.object(api, "fetch_dokploy_target_payload", return_value=self.provider_payload)
        )
        self.enterContext(patch.object(api, "dokploy_request", return_value=[]))

        def read_state(**kwargs: Any) -> None:
            fixture = ApplicationAbsenceTests()
            fixture.setUp()
            fixture.services["original-service"] = {
                "ID": "original-service",
                "Spec": {
                    "Name": self.target.target_name,
                    "TaskTemplate": {"ContainerSpec": {"Mounts": []}},
                },
            }

            def command(arguments: list[str], **_options: Any) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(arguments, 0, fixture.docker(*arguments[1:]), "")

            kwargs["receive"](
                observe_application_state_program(kwargs["program"], kwargs["payload"], command)
            )

        self.enterContext(patch.object(application_runtime, "_read_remote", side_effect=read_state))
        self.store.seed_authz_policy_if_absent(
            administrator._policy_record(administrator._ADMINISTRATOR_POLICY)
        )
        sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.human_headers = _browser_mutation_headers(
            sessions, sessions.issue(_github_human_identity())
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=administrator._ADMINISTRATOR_POLICY,
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
            human_session_manager=sessions,
            control_plane_root_path=self.adapter.root,
        )

    async def assert_verified_queue(self, headers: dict[str, str]) -> None:
        from tests.support.http import request
        from tests import test_product_reconcile as builds

        url = f"/v1/products/{self.profile.product}/testing-rebuild"
        with patch.object(
            self.store,
            "read_artifact_manifest",
            side_effect=AssertionError("Generic-web must not read an Odoo artifact"),
        ):
            inspected = await request(self.app, "GET", url + "/inputs", headers=headers)
            self.assertEqual(inspected.status_code, 200, inspected.text)
            self.assertEqual(inspected.json()["status"], "pass")
            self.assertEqual(inspected.json()["artifact_id"], self.image)
            submitted = await request(
                self.app,
                "POST",
                url,
                headers={**headers, "Idempotency-Key": "one-action"},
                payload={
                    "data_source_mode": "empty",
                    "artifact_id": self.image,
                    "source_git_ref": builds.NEWEST,
                    "reason": "Rebuild the independent stateless copy",
                },
            )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        operation = self.store.read_odoo_stable_target_replacement_operation_record(
            submitted.json()["operation"]["operation_id"]
        )
        self.assertTrue(operation.is_native_testing_rebuild)
        self.assertEqual(operation.request.artifact_id, self.image)
        self.assertIsNotNone(operation.authorization)
        self.assertEqual(self.current_target(), self.target)

    async def test_signed_in_administrator_queues_the_real_verified_image(self) -> None:
        await self.assert_verified_queue(self.human_headers)

    async def test_volume_without_activation_history_remains_pending_without_provider_reads(
        self,
    ) -> None:
        from tests.support.http import request

        profile = self.store.read_product_profile_record(self.profile.product)
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.generic_web_testing_state = ProductGenericWebTestingState(
            kind="volume", container_path="/app/data"
        )
        self.store.write_product_profile_record(profile)
        operations = self.store.list_odoo_stable_target_replacement_operation_records()
        with patch.object(source, "read_dokploy_config") as provider:
            response = await request(
                self.app,
                "GET",
                f"/v1/products/{profile.product}/testing-rebuild/inputs",
                headers=self.human_headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "pending")
        self.assertTrue(response.json()["restore_applicable"])
        provider.assert_not_called()
        self.assertEqual(
            self.store.list_odoo_stable_target_replacement_operation_records(), operations
        )
        self.assertEqual(self.current_target(), self.target)

    async def test_director_agent_queues_without_an_apply_rule(self) -> None:
        await self.assert_verified_queue({"Authorization": "Bearer local-operator-token"})

    async def test_scoped_agent_cannot_queue_generic_testing_rebuild(self) -> None:
        from tests import test_product_reconcile as builds
        from tests.support.auth import scoped_agent_transport
        from tests.support.http import request

        before = self.store.list_odoo_stable_target_replacement_operation_records()
        with scoped_agent_transport():
            response = await request(
                self.app,
                "POST",
                f"/v1/products/{self.profile.product}/testing-rebuild",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "scoped-rebuild",
                },
                payload={
                    "data_source_mode": "empty",
                    "artifact_id": self.image,
                    "source_git_ref": builds.NEWEST,
                    "reason": "Fresh disposable testing copy",
                },
            )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.list_odoo_stable_target_replacement_operation_records(), before)
        self.assertEqual(self.current_target(), self.target)


class GenericWebTestingRebuildFlowFixture(unittest.TestCase):
    with_unresolved_predecessor = False

    def setUp(self) -> None:
        from tests import test_testing_application_flow as activation

        self.flow = activation.ApplicationFlowTests()
        self.flow.no_original = not self.with_unresolved_predecessor
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.routing = self.flow.addressed()
        document = json.loads((self.routing.root / "old-app.yml").read_text())
        document["http"]["routers"]["old-app-router-fixture"]["middlewares"] = ["redirect-to-https"]
        document["http"].pop("middlewares")
        (self.routing.root / "old-app.yml").write_text(json.dumps(document))
        (self.routing.root / "middlewares.yml").write_text(
            json.dumps(
                {
                    "http": {
                        "middlewares": {
                            "redirect-to-https": {
                                "redirectScheme": {"scheme": "https", "permanent": True}
                            }
                        }
                    }
                }
            )
        )
        self.reload_routes()
        self.store = self.flow.store
        self.flow.payloads[self.flow.old_id]["mounts"] = []
        for service in self.flow.services.values():
            service["Spec"]["TaskTemplate"]["ContainerSpec"]["Mounts"] = []
        for task in self.flow.tasks.values():
            task["Spec"]["ContainerSpec"]["Mounts"] = []
        for container in self.flow.containers.values():
            labels = container.get("Config", {}).get("Labels") or {}
            if labels.get("com.docker.swarm.service.name") == "old-app":
                container["Mounts"] = []
        for binding in self.store.list_secret_bindings(
            integration="runtime_environment", context_name=self.flow.context
        ):
            if binding.instance == "testing":
                self.store.write_secret_binding(binding.model_copy(update={"status": "disabled"}))
        profile = self.store.read_product_profile_record(self.flow.product)
        profile.expected_config.managed_secret_bindings = ()
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.generic_web_testing_state = ProductGenericWebTestingState(kind="none")
        self.store.write_product_profile_record(profile)
        target = self.store.read_dokploy_target_record(
            context_name=self.flow.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={"policies": target.policies.model_copy(update={"staff_testing_hold": None})}
            )
        )
        self.expected_retirement = self.flow.old_id
        self.retained_predecessors: set[str] = set()
        self.predecessors: dict[int, str] = {}
        self.deleted: list[str] = []
        self.keep_predecessor_service = False
        self.lose_delete_reply = False
        self.after_retirement: Callable[[], None] | None = None
        self.authorize = Mock()

        def fetch(**kwargs: Any) -> dict[str, Any]:
            identifier = kwargs["target_id"]
            if identifier not in self.flow.payloads:
                raise api.DokployRequestFailed(
                    method="GET", path="/api/application.one", detail="absent", status_code=404
                )
            result = json.loads(json.dumps(self.flow.payloads[identifier]))
            assert isinstance(result, dict)
            return result

        def delete(**kwargs: Any) -> None:
            identifier = kwargs["application_id"]
            self.assertIn(identifier, {self.expected_retirement, *self.retained_predecessors})
            payload = self.remove_provider_metadata(identifier)
            (self.routing.root / (payload["appName"] + ".yml")).unlink(missing_ok=True)
            self.reload_routes()
            if not self.keep_predecessor_service:
                self.remove_runtime(payload["appName"])
            self.deleted.append(identifier)
            if self.after_retirement is not None:
                self.after_retirement()
            if self.lose_delete_reply:
                self.lose_delete_reply = False
                raise OSError("Lost application delete reply after effects")

        def read_state(**kwargs: Any) -> None:
            if kwargs["program"] not in {
                application_runtime.APPLICATION_STATE_PROGRAM,
                application_runtime.APPLICATION_RETIREMENT_PROGRAM,
            }:
                self.flow.read_runtime(**kwargs)
                return

            def command(arguments: list[str], **_options: Any) -> subprocess.CompletedProcess[str]:
                match arguments[1:]:
                    case ["service", "rm", identifier]:
                        application = next(
                            name
                            for name, row in self.flow.services.items()
                            if row["ID"] == identifier
                        )
                        self.remove_runtime(application)
                        value = identifier
                    case ["node", "ls", "--format", "json"]:
                        value = "\n".join(json.dumps(row) for row in self.flow.nodes)
                    case ["service", "ls", "-q"]:
                        value = "\n".join(row["ID"][:12] for row in self.flow.services.values())
                    case ["service", "inspect", *identifiers] | [
                        "inspect",
                        "--type",
                        "service",
                        *identifiers,
                    ]:
                        value = json.dumps(
                            [
                                next(
                                    row
                                    for name, row in self.flow.services.items()
                                    if name == identifier or row["ID"].startswith(identifier)
                                )
                                for identifier in identifiers
                            ]
                        )
                    case ["service", "ps", "--no-trunc", "-q", application]:
                        value = "\n".join(
                            key
                            for key, row in self.flow.tasks.items()
                            if row["ServiceID"] == self.flow.services[application]["ID"]
                        )
                    case ["inspect", "--type", "task", identifier]:
                        value = json.dumps([self.flow.tasks[identifier]])
                    case ["ps", "-aq", "--no-trunc"]:
                        value = "\n".join(self.flow.containers)
                    case ["inspect", *identifiers]:
                        value = json.dumps(
                            [self.flow.containers[identifier] for identifier in identifiers]
                        )
                    case _:
                        raise AssertionError(arguments)
                return subprocess.CompletedProcess(arguments, 0, value, "")

            def run() -> None:
                kwargs["receive"](
                    observe_application_state_program(kwargs["program"], kwargs["payload"], command)
                )

            if "dispatch_guard" in kwargs:
                kwargs["dispatch_guard"](run)
            else:
                run()

        def storage(**kwargs: Any) -> dict[str, str]:
            kwargs["before_effect"]("fresh-storage")
            self.flow.storage.resources = kwargs["resources"]
            self.flow.storage.containers = {
                key: {"Config": row["Config"], "Mounts": row.get("Mounts", [])}
                for key, row in self.flow.containers.items()
            }
            status, evidence = self.flow.storage.run_program(str(kwargs.get("mode", "prepare")))
            self.assertEqual(status, 0)
            return {**evidence, "schedule_id": "prepared-storage"}

        self.enterContext(patch.object(api, "fetch_dokploy_target_payload", side_effect=fetch))
        self.enterContext(patch.object(api, "delete_dokploy_application", side_effect=delete))
        self.enterContext(patch.object(application_runtime, "_read_remote", side_effect=read_state))
        self.enterContext(
            patch(
                "control_plane.testing_separation_resources.run_preparation_schedule",
                side_effect=storage,
            )
        )

    def reload_routes(self) -> None:
        from copy import deepcopy

        self.routing.raw = {"routers": {}, "services": {}, "middlewares": {}}
        for file in self.routing.root.glob("*.yml"):
            document = json.loads(file.read_text()).get("http", {})
            for section in self.routing.raw:
                for name, value in document.get(section, {}).items():
                    applied = deepcopy(value)
                    if section == "routers":
                        applied["service"] = applied["service"].removesuffix("@file") + "@file"
                        applied["priority"] = applied.get("priority") or len(
                            applied["rule"].encode()
                        )
                        if "middlewares" in applied:
                            applied["middlewares"] = [
                                reference if "@" in reference else reference + "@file"
                                for reference in applied["middlewares"]
                            ]
                    elif section == "services":
                        applied["loadBalancer"].update(
                            strategy="wrr", responseForwarding={"flushInterval": "100ms"}
                        )
                    self.routing.raw[section][name + "@file"] = {**applied, "status": "enabled"}

    def remove_runtime(self, application: str) -> None:
        service = self.flow.services.pop(application)
        self.flow.tasks = {
            key: row for key, row in self.flow.tasks.items() if row["ServiceID"] != service["ID"]
        }
        for key, row in tuple(self.flow.containers.items()):
            labels = row.get("Config", {}).get("Labels") or {}
            if (
                labels.get("com.docker.swarm.service.id") == service["ID"]
                or labels.get("com.docker.swarm.service.name") == application
            ):
                self.flow.containers.pop(key)

    def remove_provider_metadata(self, identifier: str) -> dict[str, Any]:
        payload = self.flow.payloads.pop(identifier)
        for project in self.flow.projects:
            for environment in project["environments"]:
                environment["applications"] = [
                    row for row in environment["applications"] if row["applicationId"] != identifier
                ]
        return payload

    def execute(self, attempt: int) -> Any:
        from control_plane.testing_rebuild_service import execute_native_testing_rebuild

        stamp = datetime.now(UTC).isoformat()
        authorization = DurableOperationAuthorization(
            grant="director_testing_rebuild",
            action=TESTING_TARGET_REPLACEMENT_ACTION,
            product=self.flow.product,
            context=self.flow.context,
            instances=("testing",),
            caller=durable_operation_caller_identity(self.flow.caller_identity),
            authorized_at=stamp,
        )
        operation = OdooStableTargetReplacementOperationRecord(
            schema_version=2,
            operation_id=f"generic-cycle-{attempt}",
            product=self.flow.product,
            context=self.flow.context,
            instance="testing",
            idempotency_key=f"generic-key-{attempt}",
            idempotency_scope=self.flow.scope,
            request_fingerprint=f"generic-request-{attempt}",
            request=OdooStableTargetReplacementApplyRequest(
                product=self.flow.product,
                instance="testing",
                strategy="replace-and-cutover",
                data_source_mode="empty",
                allow_empty_data=True,
                artifact_id="ghcr.io/example/site@sha256:" + "a" * 64,
                source_git_ref="a" * 40,
                reason="Rebuild stateless testing",
            ),
            authorization=authorization,
            created_at=stamp,
            updated_at=stamp,
        )
        try:
            operation = self.store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
        except FileNotFoundError:
            self.store.write_odoo_stable_target_replacement_operation_record(operation)
        self.expected_retirement = self.predecessors.setdefault(
            attempt,
            self.store.read_dokploy_target_id_record(
                context_name=self.flow.context, instance_name="testing"
            ).target_id,
        )
        result = execute_native_testing_rebuild(
            root=self.flow.root,
            store=self.store,
            operation=operation,
            authorize=self.authorize,
            lease_owner="generic-worker",
        )
        passed = result.deploy_status == result.health_status == result.post_deploy_status == "pass"
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(
                update={
                    "status": "pass" if passed else "fail",
                    "phase": "completed" if passed else "failed",
                    "result": result,
                    "error_message": "" if passed else result.error_message,
                    "error_code": "" if passed else result.error_code,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
        )
        return result


class GenericWebTestingRebuildFlowTests(GenericWebTestingRebuildFlowFixture):
    def test_predecessor_owned_authentication_refuses_before_candidate_creation(self) -> None:
        document = json.loads((self.routing.root / "old-app.yml").read_text())
        document["http"]["routers"]["old-app-router-websecure-fixture"]["middlewares"] = [
            "auth-old-app"
        ]
        (self.routing.root / "old-app.yml").write_text(json.dumps(document))
        middlewares = json.loads((self.routing.root / "middlewares.yml").read_text())
        middlewares["http"]["middlewares"]["auth-old-app"] = {"basicAuth": {"users": ["fixture"]}}
        (self.routing.root / "middlewares.yml").write_text(json.dumps(middlewares))
        self.reload_routes()
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.target_id, self.flow.old_id)
        self.assertEqual(self.flow.projects, [])
        self.assertEqual(self.deleted, [])
        self.assertIsNone(
            self.store.read_dokploy_target_record(
                context_name=self.flow.context, instance_name="testing"
            ).policies.staff_testing_hold
        )

    def test_two_rebuilds_and_the_next_ordinary_deployment_preserve_the_address(self) -> None:
        for attempt in (1, 2):
            before = self.store.read_dokploy_target_id_record(
                context_name=self.flow.context, instance_name="testing"
            ).target_id
            result = self.execute(attempt)
            self.assertEqual(result.deploy_status, "pass", result.error_code)
            self.assertNotEqual(result.target_id, before)
            self.assertEqual(self.deleted[-1], before)
            self.assertNotIn(before, self.flow.payloads)
            self.assertIsNone(
                self.store.read_dokploy_target_record(
                    context_name=self.flow.context, instance_name="testing"
                ).policies.staff_testing_hold
            )
            self.assertEqual(self.flow.normal(f"after-rebuild-{attempt}").status, "completed")
        self.assertEqual((self.flow.old_backing / "keep").read_text(), "Retained original data")

    def test_provider_metadata_delete_with_failed_service_cleanup_recovers_original(self) -> None:
        self.keep_predecessor_service = True
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "pass", result.error_code)
        self.assertEqual(result.health_status, "pass")
        self.assertNotIn("old-app", self.flow.services)
        self.assertEqual(self.flow.normal("after-cleanup").status, "completed")

    def test_active_old_provider_job_retains_predecessor_and_resumes_the_original(self) -> None:
        active = True
        provider = api.dokploy_request

        def transport(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/deployment.queueList" and active:
                return [{"data": {"applicationId": self.flow.old_id}, "state": "active"}]
            return provider(**kwargs)

        with patch.object(api, "dokploy_request", side_effect=transport):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(1)
            self.assertEqual(self.deleted, [])
            self.assertIn(self.flow.old_id, self.flow.payloads)
            self.assertFalse(self.flow.payloads[self.flow.old_id]["autoDeploy"])
            self.assertNotEqual(
                self.store.read_dokploy_target_id_record(
                    context_name=self.flow.context, instance_name="testing"
                ).target_id,
                self.flow.old_id,
            )
            active = False
            with patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            ):
                result = self.execute(1)
        self.assertEqual(result.health_status, "pass")
        self.assertEqual(self.deleted, [self.flow.old_id])

    def test_verified_candidate_failure_before_retirement_allows_ordinary_repair(self) -> None:
        from control_plane.contracts.promotion_record import HealthcheckEvidence

        read = rebuild.GenericWebRebuildActivationAdapter.read_edge_health

        def observed(adapter: Any, record: Any, **kwargs: Any) -> Any:
            if record.published:
                return HealthcheckEvidence(
                    verified=True,
                    status="fail",
                    urls=("https://testing.example.invalid/health",),
                    timeout_seconds=5,
                    runtime_identity_status="match",
                    observed_runtime_identity=kwargs.get("expected"),
                )
            return read(adapter, record, **kwargs)

        with patch.object(
            rebuild.GenericWebRebuildActivationAdapter, "read_edge_health", new=observed
        ):
            result = self.execute(1)
        self.assertEqual(result.health_status, "fail")
        self.assertNotEqual(result.target_id, self.flow.old_id)
        self.assertIn(self.flow.old_id, self.flow.payloads)
        self.assertEqual(self.deleted, [])
        self.assertEqual(result.runtime_source["retained_predecessors"], self.flow.old_id)
        self.assertEqual(self.flow.normal("repair-unhealthy-candidate").status, "completed")
        self.retained_predecessors.add(self.flow.old_id)
        repaired = self.execute(2)
        self.assertEqual(repaired.health_status, "pass")
        self.assertNotIn(self.flow.old_id, self.flow.payloads)
        self.assertEqual(set(self.deleted), {result.target_id, self.flow.old_id})
        self.assertEqual(
            self.flow.normal("after-retained-predecessor-retirement").status, "completed"
        )

    def test_lost_delete_reply_then_unhealthy_candidate_finishes_as_failed(self) -> None:
        from control_plane.contracts.promotion_record import HealthcheckEvidence

        self.lose_delete_reply = True
        with self.assertRaises(NativeTestingRebuildPending):
            self.execute(1)
        with (
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            ),
            patch.object(
                rebuild.GenericWebRebuildActivationAdapter,
                "read_edge_health",
                return_value=HealthcheckEvidence(
                    verified=True,
                    status="fail",
                    urls=("https://testing.example.invalid/health",),
                    timeout_seconds=5,
                    runtime_identity_status="match",
                ),
            ),
        ):
            result = self.execute(1)
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.health_status, "fail")
        self.assertEqual(self.deleted, [self.flow.old_id])
        self.assertIsNone(
            self.store.read_dokploy_target_record(
                context_name=self.flow.context, instance_name="testing"
            ).policies.staff_testing_hold
        )

    def test_lost_interlock_clear_reply_settles_after_the_next_ordinary_deployment(self) -> None:
        apply = apply_testing_hold_plan

        def lost_reply(**kwargs: Any) -> Any:
            result = apply(**kwargs)
            if not kwargs["request"].hold:
                raise OSError("Lost interlock-clear reply after commit")
            return result

        with patch.object(rebuild, "apply_testing_hold_plan", side_effect=lost_reply):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(1)
        self.assertEqual(self.flow.normal("after-lost-clear").status, "completed")
        target = self.store.read_dokploy_target_record(
            context_name=self.flow.context, instance_name="testing"
        )
        hold = DokployTargetStaffTestingHold(reason="A new staff test session", recorded_by="staff")
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={"policies": target.policies.model_copy(update={"staff_testing_hold": hold})}
            )
        )
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            result = self.execute(1)
        self.assertEqual(result.deploy_status, "pass", result.error_code)
        self.assertEqual(self.deleted, [self.flow.old_id])
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.flow.context, instance_name="testing"
            ).policies.staff_testing_hold,
            hold,
        )

    def test_health_failure_after_retirement_reports_the_selected_candidate_as_failed(self) -> None:
        from control_plane.contracts.promotion_record import HealthcheckEvidence
        from control_plane.testing_application_activation import ApplicationActivationAdapter

        read = ApplicationActivationAdapter.read_edge_health
        failed = False

        def fail() -> None:
            nonlocal failed
            failed = True

        self.after_retirement = fail

        def observed(adapter: ApplicationActivationAdapter, record: Any, **kwargs: Any) -> Any:
            if failed:
                return HealthcheckEvidence(
                    verified=True,
                    status="fail",
                    urls=("https://testing.example.invalid/health",),
                    timeout_seconds=5,
                    runtime_identity_status="match",
                    observed_runtime_identity=kwargs.get("expected"),
                )
            return read(adapter, record, **kwargs)

        with patch.object(ApplicationActivationAdapter, "read_edge_health", new=observed):
            result = self.execute(1)
        self.assertEqual(result.health_status, "fail")
        self.assertNotEqual(result.target_id, self.flow.old_id)
        self.assertTrue(result.error_code)

    def test_failed_candidate_preserves_the_original_serving_copy(self) -> None:
        self.flow.failed_image_type = "application"
        result = self.execute(1)
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.target_id, self.flow.old_id)
        self.assertEqual(self.deleted, [])
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.flow.context, instance_name="testing"
            ).target_id,
            self.flow.old_id,
        )


class GenericWebTestingRebuildUnknownPredecessorTests(GenericWebTestingRebuildFlowFixture):
    with_unresolved_predecessor = True

    def test_original_unknown_fence_survives_cutover_retirement_and_two_rebuilds(self) -> None:
        original = self.flow.original
        with self.assertRaises(NativeTestingRebuildPending):
            self.execute(1)
        self.assertIn(self.flow.old_id, self.flow.payloads)
        self.assertNotEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.flow.context, instance_name="testing"
            ).target_id,
            self.flow.old_id,
        )
        for history in self.flow.schedule_history.values():
            for row in history:
                if row.get("applicationId") == self.flow.old_id:
                    row.update(status="done", finishedAt=datetime.now(UTC).isoformat())
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            self.assertEqual(self.execute(1).health_status, "pass")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=original.scope,
                route_path=original.route_path,
                idempotency_key=original.idempotency_key,
            ),
            original,
        )
        from control_plane.generic_web_testing_exclusion import (
            require_retained_application_custody,
            TestingExclusionRefused,
        )

        held = self.store.list_held_provider_target_reservations()
        foreign = original.model_copy(
            update={
                "record_id": "unattributed-writer",
                "scope": "foreign-writer",
                "idempotency_key": "foreign-writer",
                "provider_target_key": "unknown-provider-target",
                "reconciliation_key": "unattributed-original",
            }
        )
        with patch.object(
            self.store, "list_held_provider_target_reservations", return_value=(*held, foreign)
        ):
            with self.assertRaises(TestingExclusionRefused):
                require_retained_application_custody(
                    self.store, product=self.flow.product, context=self.flow.context, check=Mock()
                )
        self.assertEqual(self.flow.normal("after-retired-unknown").status, "completed")
        self.assertEqual(self.execute(2).health_status, "pass")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=original.scope,
                route_path=original.route_path,
                idempotency_key=original.idempotency_key,
            ),
            original,
        )
