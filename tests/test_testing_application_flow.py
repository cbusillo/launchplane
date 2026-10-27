"""Normal recorded application effects continue through native independent activation."""

import base64
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from io import StringIO
import os
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Literal
from typing import cast
import unittest
from unittest.mock import patch
from urllib.error import URLError

from sqlalchemy import select

from control_plane import secrets
from control_plane.dokploy import api
from control_plane.dokploy.api import dokploy_request as schedule_transport
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions
from control_plane.contracts.product_profile_record import ProductSecretConfigRequirement
from control_plane.contracts.testing_activation import (
    TestingActivationRequest,
    TestingActivationRecord,
)
from control_plane.contracts.testing_application import (
    TestingApplicationSpecification,
    TestingApplicationState,
    TestingApplicationCredential,
)
from control_plane.event_testing_deploy import event_testing_deploy_request
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.generic_web_deploy_http import GENERIC_WEB_DEPLOY_ROUTE
from control_plane.integration_allowances import target_record_sha256
from control_plane.provider_operations import run_durable_provider_operation, ProviderOperationLease
from control_plane.testing_activation import activation_adapter, activation_record, ROUTE
from control_plane.testing_application_runtime import APPLICATION_RUNTIME_PROGRAM
from control_plane.testing_application_activation import ApplicationActivationAdapter
from control_plane.testing_separation_resources import (
    SeparationResourcesRequest,
    SeparationResourcesAdapter,
    preparation_binding,
    ROUTE as PREPARE_ROUTE,
)
from control_plane.workflows.generic_web_application_lifecycle import dokploy_application_lifecycle
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
)
from tests import test_testing_activation as legacy
from tests import test_testing_event_deploy_recovery as historical
from tests.test_generic_web_deploy import _FakeGenericWebDeployProvider


def build_mount_response(body: dict[str, Any], *, mount_id: str) -> dict[str, Any]:
    mount: dict[str, Any] = dict.fromkeys(
        (
            "hostPath",
            "filePath",
            "content",
            "composeId",
            "postgresId",
            "mysqlId",
            "mariadbId",
            "mongoId",
            "redisId",
            "libsqlId",
        )
    )
    mount.update(body)
    mount["applicationId"] = mount.pop("serviceId")
    mount["mountId"] = mount_id
    return mount


class ApplicationProvider(_FakeGenericWebDeployProvider):
    provider_id = "dokploy"
    delegated_executor = "control-plane.dokploy"
    application_lifecycle = staticmethod(dokploy_application_lifecycle)

    def __init__(self, fixture: "ApplicationFlowTests") -> None:
        super().__init__(
            observation=GenericWebProviderDeploymentObservation(
                outcome="present",
                deployment_id="normal-image",
                deployment_status="done",
                started_at=fixture.old.now(-1),
                finished_at=fixture.old.now(),
            )
        )
        self.fixture = fixture

    def resolve_deploy_target(self, **kwargs: Any) -> Any:
        row = self.fixture.store.read_dokploy_target_id_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.target_id = row.target_id
        self.target_name = self.fixture.payloads[row.target_id]["appName"]
        target = super().resolve_deploy_target(**kwargs)
        target.ship_request.target_category = "application"
        target.ship_request.provider_target_type = "application"
        assert target.deployed_target is not None
        target.deployed_target.target_category = "application"
        target.deployed_target.provider_target_type = "application"
        return target

    def application_execution_history(self, **kwargs: Any) -> Any:
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
        )

        return DokployGenericWebDeployProvider().application_execution_history(**kwargs)

    def execute_artifact_deploy(self, **kwargs: Any) -> None:
        super().execute_artifact_deploy(**kwargs)
        target = kwargs["resolved_deploy_target"]
        identity = kwargs["runtime_identity"]
        payload = self.fixture.payloads[target.resolved_target.target_id]
        payload.update(
            sourceType="docker",
            dockerImage=target.ship_request.artifact_id,
            env="LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity.model_dump_json(),
        )
        self.fixture.install_runtime(payload, identity.model_dump(mode="json"))


class ApplicationFlowTests(unittest.TestCase):
    database_url = ""
    pre_image_unknown = False
    direct_original = False
    no_original = False
    adapter: ApplicationActivationAdapter
    planned: TestingActivationRecord

    def test_resumed_external_conflict_after_route_switch_restores_original(self) -> None:
        from control_plane.testing_external_capabilities import ExternalCapabilityConflict
        from control_plane.dokploy.testing_route import observe_testing_route

        routing, _, _ = self.private_addressed()
        self.unhealthy_retained_runtime(omit_identity_default=True)
        self.prepare()
        interrupted = False

        def compare(record: TestingActivationRecord, check: Any) -> None:
            nonlocal interrupted
            check()
            readiness = record.application_readiness
            if (
                readiness is not None
                and readiness.public_health is not None
                and readiness.public_health.verified
                and readiness.public_health.status == "pass"
            ):
                if not interrupted:
                    interrupted = True
                    raise OSError("Fixture reply unavailable")
                raise ExternalCapabilityConflict()

        with patch.object(self.adapter, "require_production_separation", side_effect=compare):
            self.assertEqual(self.activate().status, "reconcile_required")
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        record = self.adapter.current()
        self.assertFalse(record.published)
        self.assertIsNotNone(record.failure)
        assert (
            record.application_readiness is not None
            and record.application_readiness.route is not None
        )
        self.assertEqual(
            observe_testing_route(
                record.application_readiness.route,
                routing.raw,
                json.loads(routing.route_file.read_text()),
            ),
            record.application_readiness.route.old_backend,
        )
        self.assertEqual(self.activate().status, "replayed")

    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "application-flow-fixture-root"},
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_application_runtime.observation_window_seconds",
                return_value=0,
            )
        )
        self.old = historical.TestingEventDeployRecoveryTests()
        self.old.database_url = self.database_url
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)
        self.store, self.root, self.context = self.old.store, self.old.root, self.old.context
        self.product = self.old.profile.product
        self.actor = "local:local-owner-agent:local-owner-write"
        from control_plane.http_app import launchplane_identity_actor
        from control_plane.service_auth import LocalOperatorIdentity

        self.caller_identity = LocalOperatorIdentity(
            subject="local-owner-agent", token_label="local-owner-write"
        )
        self.actor = launchplane_identity_actor(self.caller_identity)
        self.scope = legacy.ActivationTests.caller_scope()
        profile = self.old.profile.model_copy(deep=True)
        profile.public_website = "none"
        profile.runtime_port, profile.health_path = 8000, "/health"
        for lane in profile.lanes:
            if lane.instance == "testing":
                lane.base_url, lane.health_url = "", ""
        self.store.write_product_profile_record(profile)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.target_type, target.target_name, target.project_name = (
            "application",
            "old-app",
            "old-project",
        )
        self.store.write_dokploy_target_record(target)
        self.old_id = self.old.target.resolved_target.target_id
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=target,
                target_id_record=self.store.read_dokploy_target_id_record(
                    context_name=self.context, instance_name="testing"
                ),
            )
        )
        self.payloads: dict[str, dict[str, Any]] = {
            self.old_id: self.fresh(self.old_id, "old-app", "old-environment", "old-project")
        }
        self.projects: list[dict[str, Any]] = []
        self.schedules: dict[str, dict[str, Any]] = {}
        self.schedule_history: dict[str, list[dict[str, Any]]] = {}
        self.schedule_runs: list[str] = []
        self.nodes = [{"ID": "fixture-node", "Self": True}]
        self.services: dict[str, dict[str, Any]] = {}
        self.cli_container_defaults: dict[str, Any] = {}
        self.stored_service_reads = 0
        self.stored_service_view: Any = None
        self.after_stored_service_read: Any = None
        self.service_id_resolution: Any = None
        self.before_service_list: Any = None
        self.tasks: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.images: dict[str, str] = {}
        self.image_environment: list[str] = []
        self.writes: list[tuple[str, Any]] = []
        self.deployments: dict[str, list[dict[str, Any]]] = {}
        from tests.test_odoo_testing_resources import TestingResourceProgramTests

        self.storage = TestingResourceProgramTests()
        self.storage.setUp()
        self.addCleanup(self.storage.doCleanups)
        self.old_backing = self.root / "retained-data"
        self.old_backing.mkdir()
        (self.old_backing / "keep").write_text("Retained original data")
        self.payloads[self.old_id]["mounts"] = [
            build_mount_response(
                {
                    "type": "volume",
                    "volumeName": "retained-state",
                    "mountPath": "/var/lib/example",
                    "serviceType": "application",
                    "serviceId": self.old_id,
                },
                mount_id="retained-mount",
            )
        ]
        self.lose_command = False
        self.lose_create = False
        self.enterContext(
            patch(
                "control_plane.dokploy.source.read_dokploy_config",
                return_value=("https://provider.test", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=lambda **kw: deepcopy(self.payloads[kw["target_id"]]),
            )
        )
        self.enterContext(
            patch("control_plane.dokploy.api.dokploy_request", side_effect=self.provider)
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.api.list_deployments_for_target",
                side_effect=lambda **kw: deepcopy(self.deployments.get(kw["target_id"], [])),
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.api.deployment_for_target_by_title",
                side_effect=lambda **kw: next(
                    (
                        deepcopy(row)
                        for row in self.deployments.get(kw["target_id"], [])
                        if row["title"] == kw["title"]
                    ),
                    None,
                ),
            )
        )
        self.schedule_runner = api.run_dokploy_schedule
        self.enterContext(
            patch("control_plane.dokploy.api.run_dokploy_schedule", side_effect=self.command)
        )
        self.enterContext(
            patch(
                "control_plane.testing_application_host.post_deploy.run_manual_data_schedule",
                side_effect=self.host_schedule,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_application_runtime._read_remote",
                side_effect=self.read_runtime,
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.compose.fetch_dokploy_converted_compose_file",
                side_effect=lambda **kw: self.payloads[kw["compose_id"]].get("converted", ""),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_application_activation.verified_product_image",
                return_value="ghcr.io/example/site@sha256:" + "a" * 64,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_application_capabilities.fetch_application_runtime",
                side_effect=self.observed_application,
            )
        )
        if self.store.database_dialect_name != "postgresql":
            self.enterContext(
                patch.object(
                    self.store, "serialize_disposable_testing_resources", side_effect=nullcontext
                )
            )
        self.deploy_provider = ApplicationProvider(self)
        self.assertEqual(self.normal("baseline").status, "completed")
        # Use a real lifecycle command and a lost provider execution reply. The
        # ordinary adapter/runner own the actual reservation and durable snapshot.
        profile = self.store.read_product_profile_record(self.product)
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.generic_web_extensions = GenericWebLaneExtensions.model_validate(
            {
                "maintenance": [
                    {
                        "name": "migrate-fixture",
                        "command": "fixture-maintenance",
                        "timeout_seconds": 30,
                    }
                ]
            }
        )
        if self.pre_image_unknown:
            from control_plane.contracts.generic_web_extensions import GenericWebApplicationSchedule
            from control_plane.workflows.generic_web_application_lifecycle import schedule_payload

            declared = GenericWebApplicationSchedule(
                name="fixture-recurring", command="fixture-maintenance", cron_expression="0 * * * *"
            )
            lane.generic_web_extensions.schedules = (declared,)
            self.schedules["retained-schedule"] = {
                **schedule_payload(declared, self.old_id, enabled=True),
                "scheduleId": "retained-schedule",
            }
        self.store.write_product_profile_record(profile)
        self.lose_command = False
        if not self.no_original:
            self.lose_command = True
            with patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-3600)
            ):
                self.assertEqual(self.normal("original").status, "reconcile_required")
            self.lose_command = False
            self.original = next(
                row
                for row in self.store.list_held_provider_target_reservations()
                if row.route_path == GENERIC_WEB_DEPLOY_ROUTE
            )
            self.assertEqual(
                self.original.provider_effect_phase,
                "application_schedule_restore"
                if self.pre_image_unknown
                else "application_maintenance_execute",
            )
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            reason="Explicit isolation staging.",
            purpose="isolation_staging",
            recorded_by=self.actor,
        )
        self.store.write_dokploy_target_record(target)
        self.retained = target
        # Seed real encrypted local inputs after the first image-only baseline.
        seed = legacy.ActivationTests()
        seed.store, seed.context = self.store, self.context
        cast(Any, seed).preparation = SimpleNamespace(fixture=self.old)
        seed.seed_runtime_fixture()
        old_version = self.store.read_secret_version("testing-version")
        ciphertext, key_id = secrets._encrypt_secret_value("retained-only-password")
        self.store.write_secret_version(
            old_version.model_copy(update={"ciphertext": ciphertext, "key_id": key_id})
        )
        profile = self.store.read_product_profile_record(self.product)
        profile.expected_config.managed_secret_bindings = (
            ProductSecretConfigRequirement(
                binding_key="DB_PASSWORD", context=self.context, instance="testing"
            ),
        )
        self.store.write_product_profile_record(profile)
        self.specification = TestingApplicationSpecification(
            runtime_port=8000,
            health_path="/health",
            network_name="dokploy-network",
            state=TestingApplicationState(kind="volume", container_path="/var/lib/example"),
            local_credentials=(
                TestingApplicationCredential(binding_key="DB_PASSWORD", encoding="base64"),
            ),
        )
        self.lost_image_reply = False
        self.failed_image_type = ""

    def observed_application(self, **kwargs: Any) -> Any:
        from control_plane.dokploy.application_runtime import ApplicationRuntimeObservation

        payload = self.payloads[kwargs["application_id"]]
        service = self.services[payload["appName"]]
        configuration = service["Spec"]["TaskTemplate"]["ContainerSpec"]
        values = dict(value.split("=", 1) for value in configuration["Env"])
        identity = json.loads(values["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])
        return ApplicationRuntimeObservation(
            application_id=payload["applicationId"],
            app_name=payload["appName"],
            server_id=payload["serverId"],
            service_id=service["ID"],
            service_image=configuration["Image"],
            service_deployment_record_id=identity["deployment_record_id"],
            desired_replicas=1,
            rollout_state="completed",
            rollout_started_at="",
            running_container_images=(),
            running_container_deployment_record_ids=(),
            running_container_health=(),
            pending_task_count=0,
        )

    def fresh(self, identifier: str, name: str, environment: str, project: str) -> dict[str, Any]:
        return {
            "applicationId": identifier,
            "name": name,
            "appName": name,
            "environmentId": environment,
            "environment": {
                "environmentId": environment,
                "projectId": project,
                "env": "",
                "project": {"projectId": project, "env": ""},
            },
            "serverId": "server",
            "env": "",
            "sourceType": "github",
            "autoDeploy": True,
            "mounts": [],
            "domains": [],
            "replicas": 1,
            "ports": [],
            "security": [],
            "redirects": [],
            "deployments": [],
            "previewDeployments": [],
            "applicationStatus": "idle",
        }

    def normal(self, suffix: str) -> Any:
        from control_plane.product_reconcile import reconcile_reservation_scope, RECONCILE_SOURCE

        profile = self.store.read_product_profile_record(self.product)
        request = event_testing_deploy_request(
            product=self.product,
            image_reference=(
                profile.image.repository if self.direct_original else "ghcr.io/example/site"
            )
            + "@sha256:"
            + "a" * 64,
            source_commit="a" * 40,
        )
        adapter = GenericWebDeployProviderMutationAdapter(
            control_plane_root=self.root,
            record_store=self.store,
            deploy_request=request,
            profile=profile,
            lane=next(lane for lane in profile.lanes if lane.instance == "testing"),
            trace_id="normal-" + suffix,
            deploy_provider=self.deploy_provider,
            recorded_artifact=not self.direct_original,
            retain_event_request=not self.direct_original,
        )
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.http_app import idempotency_request_fingerprint

        self.normal_payload = request.model_dump(mode="json")
        return run_durable_provider_operation(
            store=self.store,
            scope=idempotency_scope(self.caller_identity)
            if self.direct_original
            else reconcile_reservation_scope(self.product),
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key="native-" + suffix
            if self.direct_original
            else f"{RECONCILE_SOURCE}:{self.product}:{self.context}:testing:{suffix}",
            request_fingerprint=idempotency_request_fingerprint(
                route_path=GENERIC_WEB_DEPLOY_ROUTE, payload=self.normal_payload
            )
            if self.direct_original
            else hashlib.sha256(suffix.encode()).hexdigest(),
            lease_owner="normal-worker",
            response_trace_id="normal-" + suffix,
            adapter=adapter,
        )

    def command(self, **kwargs: Any) -> str:
        # The real helper owns guard, persisted intent, acknowledgement and history order.
        return self.schedule_runner(**kwargs)

    def install_runtime(self, payload: dict[str, Any], identity: dict[str, Any]) -> None:
        name = payload["appName"]
        service, task, container = (
            hashlib.sha256((name + "-service").encode()).hexdigest()[:25],
            name + "-task",
            name + "-container",
        )
        values = [
            key + "=" + value
            for key, value in __import__(
                "control_plane.dokploy.api", fromlist=["parse_dokploy_env_text"]
            )
            .parse_dokploy_env_text(payload["env"])
            .items()
        ]
        configuration = {
            "Image": identity["artifact_id"],
            "Env": values,
            "Mounts": [
                {"Type": "volume", "Source": row["volumeName"], "Target": row["mountPath"]}
                for row in payload["mounts"]
            ],
        }
        self.services[name] = {
            "ID": service,
            "Version": {"Index": 1},
            "Spec": {
                "Name": name,
                "Mode": {"Replicated": {"Replicas": 1}},
                "TaskTemplate": {
                    "ContainerSpec": deepcopy(configuration),
                    "Networks": [{"Target": "fixture-network-id", "Aliases": [name]}],
                },
            },
            "Endpoint": {},
        }
        self.tasks[task] = {
            "ID": task,
            "ServiceID": service,
            "NodeID": self.nodes[0]["ID"],
            "DesiredState": "running",
            "Spec": {"ContainerSpec": configuration},
            "Status": {"State": "running", "ContainerStatus": {"ContainerID": container}},
        }
        self.images["sha256:" + "b" * 64] = identity["artifact_id"]
        self.containers[container] = {
            "Id": container,
            "Image": "sha256:" + "b" * 64,
            "Name": "/" + container,
            "State": {"Status": "running"},
            "Config": {
                "Image": identity["artifact_id"],
                "Env": list(values),
                "Labels": {
                    "com.docker.swarm.service.name": name,
                    "com.docker.swarm.service.id": service,
                    "com.docker.swarm.task.id": task,
                },
            },
            "HostConfig": {},
            "Mounts": [
                {
                    "Type": "volume",
                    "RW": True,
                    "Name": row["volumeName"],
                    "Destination": row["mountPath"],
                    "Source": str(self.old_backing)
                    if row["volumeName"] == "retained-state"
                    else str(self.storage.volumes[row["volumeName"]]["Mountpoint"]),
                }
                for row in payload["mounts"]
            ],
            "NetworkSettings": {
                "Networks": {
                    "dokploy-network": {
                        "IPAddress": "10.0.0." + str(2 + len(self.projects)),
                        "NetworkID": "fixture-network-id",
                        "Aliases": [name],
                    }
                }
            },
        }

    def lost_schedule_reply(self, **kwargs: Any) -> Any:
        with patch.object(api, "urlopen", side_effect=URLError("Lost provider execution reply")):
            return schedule_transport(**kwargs)

    def provider(self, **kwargs: Any) -> Any:
        path, body = kwargs["path"], kwargs.get("payload", {})
        if kwargs.get("method", "GET") == "GET":
            if path == "/api/server.one":
                return {"serverId": "server", "name": "Fixture server"}
            if path in {"/api/application.one", "/api/compose.one"}:
                return deepcopy(self.payloads[next(iter(kwargs["query"].values()))])
            if path == "/api/project.all":
                return deepcopy(self.projects)
            if path == "/api/schedule.list":
                query = kwargs["query"]
                return [
                    deepcopy(row)
                    for row in self.schedules.values()
                    if row.get("applicationId") == query.get("id")
                ]
            if path == "/api/deployment.queueList":
                return []
            if path == "/api/deployment.allByType":
                query = kwargs["query"]
                return (
                    deepcopy(self.schedule_history.get(query["id"], []))
                    if query["type"] == "schedule"
                    else []
                )
            raise AssertionError(kwargs)
        self.writes.append((path, deepcopy(body)))
        if path == "/api/schedule.runManually":
            identifier = body["scheduleId"]
            schedule = self.schedules[identifier]
            self.schedule_runs.append(identifier)
            row = {
                "deploymentId": "schedule-execution-" + str(len(self.schedule_runs)),
                "scheduleId": identifier,
                "applicationId": schedule["applicationId"],
                "status": "running" if self.lose_command else "done",
                "startedAt": self.old.now(-1),
                "finishedAt": "" if self.lose_command else self.old.now(),
            }
            self.schedule_history.setdefault(identifier, []).append(row)
            if self.lose_command:
                return self.lost_schedule_reply(**kwargs)
            return True
        if path == "/api/schedule.create":
            identifier = "schedule-" + str(len(self.schedules))
            self.schedules[identifier] = {**body, "scheduleId": identifier}
            return self.schedules[identifier]
        if path == "/api/schedule.update":
            self.schedules[body["scheduleId"]].update(body)
            if self.pre_image_unknown and self.lose_command and body.get("enabled") is False:
                return self.lost_schedule_reply(**kwargs)
            return deepcopy(self.schedules[body["scheduleId"]])
        if path == "/api/schedule.delete":
            self.schedules.pop(body["scheduleId"])
            return {}
        if path == "/api/project.create":
            identifier = "new-project-" + str(len(self.projects))
            self.projects.append(
                {"projectId": identifier, "name": body["name"], "environments": []}
            )
            return {"projectId": identifier}
        if path == "/api/environment.create":
            identifier = "new-environment-" + str(len(self.projects))
            from control_plane.dokploy_inventory import TARGET_COLLECTIONS

            next(row for row in self.projects if row["projectId"] == body["projectId"])[
                "environments"
            ].append(
                {
                    "environmentId": identifier,
                    "name": body["name"],
                    **{key: [] for key in TARGET_COLLECTIONS},
                }
            )
            return {"environmentId": identifier}
        if path == "/api/application.create":
            identifier = "new-application-" + str(len(self.projects))
            project = self.projects[-1]
            environment = project["environments"][-1]
            self.payloads[identifier] = self.fresh(
                identifier, body["appName"], environment["environmentId"], project["projectId"]
            )
            environment["applications"].append(
                {"applicationId": identifier, "name": body["name"], "appName": body["appName"]}
            )
            if self.lose_create:
                self.lose_create = False
                raise OSError("Lost application create reply")
            return deepcopy(self.payloads[identifier])
        if path == "/api/mounts.create":
            mount = build_mount_response(body, mount_id="new-mount")
            self.payloads[body["serviceId"]]["mounts"].append(mount)
            return deepcopy(mount)
        if path in {
            "/api/application.update",
            "/api/application.saveDockerProvider",
            "/api/application.saveEnvironment",
        }:
            self.payloads[body["applicationId"]].update(body)
            return {}
        if path == "/api/application.deploy":
            identifier = body["applicationId"]
            payload = self.payloads[identifier]
            from control_plane.dokploy.api import parse_dokploy_env_text

            identity = json.loads(
                parse_dokploy_env_text(payload["env"])["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]
            )
            self.install_runtime(payload, identity)
            row = {
                "deploymentId": "activation-image-" + str(len(self.projects)),
                "title": body["title"],
                "status": "done",
                "startedAt": self.old.now(-1),
                "finishedAt": self.old.now(),
                "applicationId": identifier,
            }
            self.deployments.setdefault(identifier, []).append(row)
            payload["deployments"] = deepcopy(self.deployments[identifier])
            payload["applicationStatus"] = "done"
            if self.failed_image_type == "application":
                from control_plane.dokploy.api import DokployDeploymentFailed

                row["status"] = "error"
                payload["deployments"] = deepcopy(self.deployments[identifier])
                payload["applicationStatus"] = "error"
                if not self.lost_image_reply:
                    raise DokployDeploymentFailed(
                        deployment_id=row["deploymentId"],
                        deployment_status=row["status"],
                        message_prefix="Fresh candidate failed",
                        deployment=deepcopy(row),
                    )
            if self.lost_image_reply:
                self.lost_image_reply = False
                raise OSError("Lost the original fresh image deployment reply")
            return {}
        raise AssertionError(kwargs)

    def docker(self, arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        args = arguments[1:]
        if args[:2] == ["node", "ls"]:
            value: Any = "\n".join(json.dumps(node) for node in self.nodes)
        elif args[:2] == ["service", "inspect"]:
            displayed = deepcopy(self.services[args[2]])
            configuration = displayed["Spec"]["TaskTemplate"]["ContainerSpec"]
            for key, default in self.cli_container_defaults.items():
                configuration.setdefault(key, deepcopy(default))
            value = [displayed]
        elif args[:1] == ["version"]:
            value = "1.47"
        elif args[:2] == ["volume", "inspect"]:
            value = [self.storage.volumes[args[2]]]
        elif args[:2] == ["service", "ps"]:
            value = "\n".join(
                key
                for key, row in self.tasks.items()
                if row["ServiceID"] == self.services[args[-1]]["ID"]
            )
        elif args[:2] == ["ps", "-aq"]:
            name = args[-1].split("=", 2)[-1]
            value = "\n".join(
                key
                for key, row in self.containers.items()
                if (row["Config"].get("Labels") or {}).get("com.docker.swarm.service.name") == name
            )
        elif args[:2] == ["image", "inspect"]:
            value = [
                {
                    "Id": args[2],
                    "RepoDigests": [self.images[args[2]]],
                    "Config": {"Env": self.image_environment},
                }
            ]
        elif args[:3] == ["inspect", "--type", "task"]:
            value = [self.tasks[args[3]]]
        elif args[0] == "inspect" and args[1] in self.containers:
            value = [self.containers[args[1]]]
        elif args[:3] == ["ps", "-q", "--filter"]:
            value = "proxy"
        elif args == ["inspect", "proxy"]:
            value = [
                {
                    "Id": "proxy",
                    "State": {"Status": "running"},
                    "NetworkSettings": {"Networks": {"dokploy-network": {}}},
                }
            ]
        elif args[:4] == ["exec", "-i", "proxy", "nc"]:
            address = args[-2]
            container = next(
                row
                for row in self.containers.values()
                if any(
                    network.get("IPAddress") == address
                    for network in row["NetworkSettings"]["Networks"].values()
                )
            )
            env = dict(value.split("=", 1) for value in container["Config"]["Env"])
            body = json.dumps(
                {"runtime_identity": json.loads(env["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])}
            ).encode()
            return self.http_response(arguments, body=body)
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, value if isinstance(value, str) else json.dumps(value), ""
        )

    @staticmethod
    def http_response(
        arguments: list[str],
        *,
        body: bytes = b"",
        status: int = 200,
        declared_length: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        length = len(body) if declared_length is None else declared_length
        return subprocess.CompletedProcess(
            arguments,
            0,
            (
                "HTTP/1.1 "
                + str(status)
                + " Fixture\r\nContent-Length: "
                + str(length)
                + "\r\nConnection: close\r\n\r\n"
            ).encode()
            + body,
            b"",
        )

    def host_schedule(self, **kwargs: Any) -> dict[str, str]:
        kwargs["before_provider_mutation"]("host-capability-proof")
        script = kwargs["schedule_payload"]["script"]
        encoded = script.split("python3 - ", 1)[1].split(" ", 1)[0]
        spec = json.loads(base64.b64decode(encoded))
        from control_plane.testing_application_capabilities import (
            retained_application_capability_program,
        )

        self.assertEqual(script, retained_application_capability_program(spec))
        output = StringIO()
        delimiter = "LP_RETAINED_CAPABILITY"
        header = script.split("<<'" + delimiter + "'\n", 1)[1].rsplit("\n" + delimiter, 1)[0]
        with (
            patch.object(sys, "argv", ["program", encoded]),
            patch.object(subprocess, "run", side_effect=self.docker),
            patch("sys.stdout", output),
        ):
            try:
                exec(compile(header, "<stdin>", "exec"), {})
            except SystemExit as error:
                if error.code != 1:
                    raise
        return dict(line.split("=", 1) for line in output.getvalue().splitlines() if "=" in line)

    def read_runtime(self, **kwargs: Any) -> None:
        self.assertEqual(kwargs["program"], APPLICATION_RUNTIME_PROGRAM)
        output = StringIO()
        encoded = base64.b64encode(json.dumps(kwargs["payload"]).encode()).decode()
        with (
            patch.object(sys, "argv", ["program", encoded, "frame:"]),
            patch.object(subprocess, "run", side_effect=self.docker),
            patch("sys.stdout", output),
        ):
            try:
                exec(kwargs["program"], {})
            except SystemExit as error:
                # The real remote reader reports a refused/nonzero program as
                # an unavailable proof, rather than terminating this test worker.
                raise ValueError("Runtime program did not supply a completed proof") from error
        kwargs["receive"](json.loads(output.getvalue().removeprefix("frame:")))

    def prepare(self, key: str = "prepare") -> None:
        body = SeparationResourcesRequest(
            context=self.context,
            application=self.specification,
            mode="apply",
            reason="Prepare isolated application state.",
        )
        body.reviewed_plan_sha256 = preparation_binding(
            self.store, self.product, self.context, self.actor, self.root, self.specification
        )[0]
        resources_adapter = SeparationResourcesAdapter(
            self.root, self.store, self.product, body, self.actor, "prepare-flow"
        )

        def storage(**kwargs: Any) -> dict[str, str]:
            kwargs["before_effect"]("fresh-storage")
            self.storage.resources = kwargs["resources"]
            self.storage.containers = {
                key: {"Config": value["Config"], "Mounts": value.get("Mounts", [])}
                for key, value in self.containers.items()
            }
            status, evidence = self.storage.run_program(str(kwargs.get("mode", "prepare")))
            self.assertEqual(status, 0)
            return {**evidence, "schedule_id": "prepared-storage"}

        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=storage,
        ):
            result = run_durable_provider_operation(
                store=self.store,
                scope=self.scope,
                route_path=PREPARE_ROUTE.format(product=self.product),
                idempotency_key=key,
                request_fingerprint="a" * 64,
                lease_owner="prepare-worker",
                response_trace_id="prepare-flow",
                adapter=resources_adapter,
            )
        self.assertEqual(result.status, "completed", result)
        self.request = TestingActivationRequest(
            context=self.context,
            preparation_key=key,
            interlock_record_sha256=target_record_sha256(self.retained),
            source_commit="a" * 40,
            volume_keys={},
            port_keys=(),
            reason="Activate isolated application.",
            application=self.specification,
        )
        adapter = activation_adapter(
            self.root,
            self.store,
            self.product,
            self.request,
            self.actor,
            self.scope,
            "application-flow",
        )
        assert isinstance(adapter, ApplicationActivationAdapter)
        self.adapter = adapter
        self.planned = self.adapter.current()
        self.request.mode, self.request.reviewed_plan_sha256 = "apply", self.planned.plan_sha256

    def activate(self) -> Any:
        return run_durable_provider_operation(
            store=self.store,
            scope=self.scope,
            route_path=ROUTE.format(product=self.product),
            idempotency_key=getattr(self, "activation_key", "activation"),
            request_fingerprint="b" * 64,
            lease_owner="activation-worker",
            response_trace_id="application-flow",
            adapter=self.adapter,
            original_intent={"testing_activation": self.planned.model_dump(mode="json")},
        )

    def prepare_external_discord(self) -> None:
        from control_plane.contracts.testing_application import TestingApplicationExternalCredential

        profile = self.store.read_product_profile_record(self.product)
        profile.expected_config.managed_secret_bindings = (
            ProductSecretConfigRequirement(
                binding_key="DB_PASSWORD",
                context=self.context,
                instance="testing",
                external_provider="discord_bot",
            ),
        )
        self.store.write_product_profile_record(profile)
        version = self.store.read_secret_version("testing-version")
        ciphertext, key_id = secrets._encrypt_secret_value("MTIz.fixture.signature")
        self.store.write_secret_version(
            version.model_copy(
                update={"ciphertext": ciphertext, "key_id": key_id, "value_origin": None}
            )
        )
        self.specification = self.specification.model_copy(
            update={
                "local_credentials": (),
                "external_credentials": (
                    TestingApplicationExternalCredential(
                        binding_key="DB_PASSWORD", guild_ids=("10",)
                    ),
                ),
            }
        )
        self.prepare()
        self.adapter.authorize_external = self.store.list_product_profile_records

    def test_external_admission_persists_once_across_original_restart(self) -> None:
        from tests.test_testing_application_activation import ApplicationCredentialTests

        self.prepare_external_discord()
        with patch(
            "control_plane.discord_external_resources.request_public_http",
            side_effect=ApplicationCredentialTests().discord_response,
        ) as discord:
            with patch.object(
                self.adapter, "preflight_application_route", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    self.activate()
            admitted = self.adapter.current()
            self.assertTrue(admitted.external_capabilities)
            self.assertFalse(admitted.project_id)
            self.assertEqual(admitted.phase, "prepared")
            self.assertEqual(admitted.plan_sha256, self.planned.plan_sha256)
            reads = discord.call_count
            self.assertGreater(reads, 0)
            preflight = self.adapter.preflight_application_route

            def inspect(record: TestingActivationRecord) -> Any:
                proof = record.external_capabilities[0]
                for proofs in (
                    (),
                    (proof.model_copy(update={"version_id": "foreign-version"}),),
                    (proof, proof),
                ):
                    with self.subTest(proofs=proofs), self.assertRaises(ValueError):
                        self.adapter.save(record, external_capabilities=proofs)
                    self.assertEqual(self.adapter.current(), record)
                return preflight(record)

            with patch.object(self.adapter, "preflight_application_route", side_effect=inspect):
                result = self.activate()
            self.assertEqual(result.status, "completed", result)
            self.assertEqual(
                self.adapter.current().external_capabilities, admitted.external_capabilities
            )
            self.assertEqual(discord.call_count, reads)

    def test_released_external_admission_reobserves_under_same_key(self) -> None:
        from tests.test_testing_application_activation import ApplicationCredentialTests
        from control_plane.contracts.testing_activation import TestingActivationRefusal

        self.prepare_external_discord()
        with patch(
            "control_plane.discord_external_resources.request_public_http",
            side_effect=ApplicationCredentialTests().discord_response,
        ) as discord:
            with patch.object(
                self.adapter,
                "preflight_application_route",
                side_effect=TestingActivationRefusal("Fixture pre-effect refusal"),
            ):
                with self.assertRaises(TestingActivationRefusal):
                    self.activate()
            self.assertFalse(self.adapter.current().external_capabilities)
            reads = discord.call_count
            self.assertGreater(reads, 0)
            result = self.activate()
            self.assertEqual(result.status, "completed", result)
            self.assertGreater(discord.call_count, reads)
            self.assertTrue(self.adapter.current().external_capabilities)
            self.assertEqual(self.adapter.current().plan_sha256, self.planned.plan_sha256)

    def test_external_admission_refuses_foreign_or_combined_progress(self) -> None:
        from tests.test_testing_application_activation import ApplicationCredentialTests
        from control_plane.testing_external_capabilities import observe_activation_discord

        self.prepare_external_discord()

        def observe(*args: Any, **kwargs: Any) -> Any:
            proof = observe_activation_discord(*args, **kwargs)
            record = self.adapter.current()
            for changes in (
                {"external_capabilities": (proof,), "phase": "create_project"},
                {"external_capabilities": (proof,), "project_id": "foreign-project"},
                {
                    "external_capabilities": (
                        proof.model_copy(update={"activation_plan_sha256": "f" * 64}),
                    )
                },
                {"external_capabilities": (proof.model_copy(update={"binding_key": "OTHER"}),)},
                {"external_capabilities": (proof.model_copy(update={"guild_ids": ("20",)}),)},
                {"external_capabilities": (proof, proof)},
            ):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    self.adapter.save(record, **changes)
                self.assertEqual(self.adapter.current(), record)
            late = self.adapter.save(record, phase="create_project")
            with self.assertRaises(ValueError):
                self.adapter.save(late, external_capabilities=(proof,))
            self.assertEqual(self.adapter.current(), late)
            raise KeyboardInterrupt

        with (
            patch(
                "control_plane.discord_external_resources.request_public_http",
                side_effect=ApplicationCredentialTests().discord_response,
            ),
            patch(
                "control_plane.testing_external_capabilities.observe_activation_discord",
                side_effect=observe,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.activate()

    def inspect_current_private_runtime(self) -> Any:
        from control_plane.testing_application_runtime import inspect_application_runtime
        from control_plane.testing_activation import preparation_resources

        record = self.adapter.current()
        readiness = record.application_readiness
        assert readiness is not None
        resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        return inspect_application_runtime(
            host="https://provider.test",
            token="fixture-token",
            server_id="server",
            application=record.app_name,
            runtime_port=self.specification.runtime_port,
            network=self.specification.network_name,
            image=record.image_reference,
            identity=readiness.runtime_identity,
            values=self.adapter.application_values(record),
            volume=resources.volumes.get("state", ""),
            volume_binding=resources.binding_sha256,
            mount_path=self.specification.state.container_path,
            health_path=self.specification.health_path,
        )

    def test_bounded_private_health_warmup_canonical_variants_and_unknown_tail(self) -> None:
        import itertools
        from control_plane.testing_application_runtime import VerifiedApplicationRuntimeFailure

        self.prepare()
        self.assertEqual(self.activate().status, "completed")
        readiness = self.adapter.current().application_readiness
        assert readiness is not None
        identity = readiness.runtime_identity.model_dump(mode="json")
        variants = [
            {"runtime_identity": {**identity, "deployed_at": self.old.now()}},
            {
                "launchplane_runtime_identity": {
                    key: value
                    for key, value in identity.items()
                    if key not in {"schema_version", "image_reference"}
                }
            },
            {"runtime_identity": None, "launchplaneRuntimeIdentity": identity},
            {"runtime_identity": json.dumps(identity)},
        ]
        fixture = self.docker
        for payload in variants:
            with self.subTest(payload=payload):
                replies = iter(
                    [
                        self.http_response([], status=503),
                        self.http_response([], body=json.dumps(payload).encode()),
                    ]
                )
                calls = []

                def warmup(arguments: list[str], **kwargs: Any) -> Any:
                    if arguments[1:5] == ["exec", "-i", "proxy", "nc"]:
                        calls.append(arguments)
                        return next(replies)
                    return fixture(arguments, **kwargs)

                ticks = itertools.count()
                with (
                    patch.object(self, "docker", side_effect=warmup),
                    patch(
                        "control_plane.testing_application_runtime.observation_window_seconds",
                        return_value=2,
                    ),
                    patch(
                        "control_plane.testing_application_runtime.observation_clock",
                        side_effect=lambda: float(next(ticks)),
                    ),
                    patch("control_plane.testing_application_runtime.pause_observation"),
                ):
                    self.assertFalse(self.inspect_current_private_runtime()["verified_failure"])
                self.assertEqual(len(calls), 2)
        for body, expected_status, exception in (
            (b"{}", "missing", VerifiedApplicationRuntimeFailure),
            (b'{"runtime_identity":null}', "malformed", VerifiedApplicationRuntimeFailure),
            (b"[]", None, ValueError),
            (b"{", None, ValueError),
        ):
            with self.subTest(body=body):

                def reply(arguments: list[str], **kwargs: Any) -> Any:
                    if arguments[1:5] == ["exec", "-i", "proxy", "nc"]:
                        return self.http_response(arguments, body=body)
                    return fixture(arguments, **kwargs)

                with (
                    patch.object(self, "docker", side_effect=reply),
                    self.assertRaises(exception) as raised,
                ):
                    self.inspect_current_private_runtime()
                if expected_status:
                    assert isinstance(raised.exception, VerifiedApplicationRuntimeFailure)
                    self.assertEqual(raised.exception.evidence["identity_status"], expected_status)
        calls = []

        def unavailable_tail(arguments: list[str], **kwargs: Any) -> Any:
            if arguments[1:5] == ["exec", "-i", "proxy", "nc"]:
                calls.append(arguments)
                if len(calls) == 1:
                    return self.http_response(arguments, status=503)
                raise OSError("A later sample is unavailable")
            return fixture(arguments, **kwargs)

        ticks = itertools.count()
        with (
            patch.object(self, "docker", side_effect=unavailable_tail),
            patch(
                "control_plane.testing_application_runtime.observation_window_seconds",
                return_value=2,
            ),
            patch(
                "control_plane.testing_application_runtime.observation_clock",
                side_effect=lambda: float(next(ticks)),
            ),
            patch("control_plane.testing_application_runtime.pause_observation"),
            self.assertRaises(ValueError) as raised,
        ):
            self.inspect_current_private_runtime()
        self.assertNotIsInstance(raised.exception, VerifiedApplicationRuntimeFailure)
        self.assertEqual(len(calls), 2)

    def test_healthy_replacement_wins_over_failed_history_and_pending_stays_unknown(self) -> None:
        from control_plane.testing_application_runtime import VerifiedApplicationRuntimeFailure

        self.prepare()
        self.assertEqual(self.activate().status, "completed")
        readiness = self.adapter.current().application_readiness
        assert readiness is not None
        task, container = self.tasks[readiness.task_id], self.containers[readiness.container_id]
        for suffix in ("-old-a", "-old-b"):
            old_task = deepcopy(task)
            old_task.update(ID=task["ID"] + suffix, DesiredState="shutdown")
            old_task["Status"] = {
                "State": "failed",
                "ContainerStatus": {"ContainerID": container["Id"] + suffix},
            }
            old_container = deepcopy(container)
            old_container["Id"] += suffix
            old_container["State"]["Status"] = "exited"
            old_container["Config"]["Labels"]["com.docker.swarm.task.id"] = old_task["ID"]
            self.tasks[old_task["ID"]] = old_task
            self.containers[old_container["Id"]] = old_container
        self.assertFalse(self.inspect_current_private_runtime()["verified_failure"])
        task["Status"]["State"] = "preparing"
        with self.assertRaises(ValueError) as raised:
            self.inspect_current_private_runtime()
        self.assertNotIsInstance(raised.exception, VerifiedApplicationRuntimeFailure)
        task["Status"]["State"] = "failed"
        task["DesiredState"] = "shutdown"
        container["State"]["Status"] = "exited"
        with self.assertRaises(VerifiedApplicationRuntimeFailure):
            self.inspect_current_private_runtime()

    def assert_guarded_checkpoint_gap(self, phase: str, *, correction: bool) -> None:
        from control_plane.provider_operations import _ReservationHeartbeat

        self.prepare()
        original = _ReservationHeartbeat.checkpoint_effect
        changed: list[str] = []

        def checkpoint(lease: Any, admitted_phase: str) -> None:
            if admitted_phase == phase and not changed:
                changed.append(admitted_phase)
                if correction:
                    (runtime,) = self.store.list_runtime_environment_records(
                        context_name=self.context, instance_name="testing"
                    )
                    runtime.env["APPLICATION_MODE"] = "reviewed-correction"
                    self.store.write_runtime_environment_record(runtime)
                else:
                    raise OSError("Checkpoint unavailable before dispatch")
            original(lease, admitted_phase)

        with (
            patch.object(_ReservationHeartbeat, "checkpoint_effect", checkpoint),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            outcome = self.activate()
        if correction:
            self.assertEqual(outcome.status, "completed", outcome)
            self.assertIsNotNone(self.adapter.current().failure)
            self.assertFalse(self.adapter.current().published)
            self.assertFalse(self.deployments.get(self.adapter.current().target_id))
        else:
            self.assertEqual(outcome.status, "reconcile_required", outcome)
            self.assertIn(self.activate().status, {"completed", "adopted"})
            self.assertEqual(len(self.deployments[self.adapter.current().target_id]), 1)

    def test_mount_checkpoint_unavailable_can_continue_original(self) -> None:
        self.assert_guarded_checkpoint_gap("create_application_mount", correction=False)

    def test_mount_checkpoint_correction_retains_unused_original(self) -> None:
        self.assert_guarded_checkpoint_gap("create_application_mount", correction=True)

    def test_deploy_checkpoint_correction_retains_unused_original(self) -> None:
        self.assert_guarded_checkpoint_gap("deploy_application_deployment_id", correction=True)

    def test_actual_image_admission_survives_readonly_checkpoint_overwrite(self) -> None:
        self.prepare()
        self.lost_image_reply = True
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        with (
            patch("control_plane.dokploy.api.deployment_for_target_by_title", return_value=None),
            patch.object(self.store, "_database_mutation_timestamp", return_value=self.old.now(1)),
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        record = self.adapter.current()
        self.assertIsNone(record.failure)
        self.assertEqual(record.phase, "deploy_application_deployment_id")
        self.assertIn(self.activate().status, {"completed", "adopted"})
        self.assertEqual(
            len([path for path, _ in self.writes if path == "/api/application.deploy"]), 1
        )

    def test_private_edge_disable_after_switch_restores_without_reenabling(self) -> None:
        from control_plane.dokploy.testing_route import observe_testing_route

        routing, ingress, _ = self.private_addressed()
        self.prepare()
        publish = self.adapter.publish_application

        def disable(record: Any, checkpoint: Any) -> Any:
            ingress.client.disable_proxy_host(ingress.client.proxy_hosts[0].id)
            return publish(record, checkpoint)

        with patch.object(self.adapter, "publish_application", side_effect=disable):
            outcome = self.activate()
        self.assertEqual(outcome.status, "completed", outcome)
        record = self.adapter.current()
        self.assertIsNotNone(record.failure)
        self.assertFalse(record.published)
        self.assertFalse(ingress.client.proxy_hosts[0].enabled)
        assert (
            record.application_readiness is not None
            and record.application_readiness.route is not None
        )
        self.assertEqual(
            observe_testing_route(
                record.application_readiness.route,
                routing.raw,
                json.loads(routing.route_file.read_text()),
            ),
            record.application_readiness.route.old_backend,
        )

    def test_private_reservation_insert_before_publication_cas_keeps_old_canonical(self) -> None:
        _, ingress, _ = self.private_addressed()
        self.prepare()
        write = self.store.write_testing_activation
        inserted: list[Any] = []

        def publish(**kwargs: Any) -> None:
            if kwargs["replacement"]["published"] and not inserted:
                inserted.append(
                    self.store.reserve_mutation(
                        scope="private-race",
                        route_path="/private-race",
                        idempotency_key="private-race",
                        request_fingerprint="private-race",
                        lease_owner="private-race",
                        reconciliation_key="private-race",
                        provider_target_key=ingress.target_key(),
                    )
                )
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=publish):
            outcome = self.activate()
        self.assertEqual(outcome.status, "reconcile_required", outcome)
        record = self.adapter.current()
        self.assertFalse(record.published)
        self.assertIsNone(record.failure)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ).target_id,
            self.old_id,
        )
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertTrue(
            any(
                row.provider_target_key == ingress.target_key()
                for row in self.store.list_held_provider_target_reservations()
            )
        )

    def test_final_private_failure_is_recovery_only_after_busy_lock(self) -> None:
        self.prepare()
        fixture_docker, lock = self.docker, self.store.testing_replacement_execution_lock
        locks = []

        def closed(*args: Any, **kwargs: Any) -> Any:
            locks.append(args)
            return lock(*args, **kwargs) if len(locks) == 1 else nullcontext(None)

        def failure(arguments: list[str], **kwargs: Any) -> Any:
            if arguments[1:5] == ["exec", "-i", "proxy", "nc"]:
                return self.http_response(arguments, status=503)
            return fixture_docker(arguments, **kwargs)

        with (
            patch.object(self, "docker", side_effect=failure),
            patch.object(self.store, "testing_replacement_execution_lock", side_effect=closed),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            outcome = self.activate()
        self.assertEqual(outcome.status, "reconcile_required", outcome)
        record = self.adapter.current()
        assert (
            record.application_readiness is not None
            and record.application_readiness.private_health is not None
        )
        self.assertEqual(record.application_readiness.private_health.status, "fail")
        with patch.object(
            self.adapter,
            "qualify_application",
            side_effect=AssertionError(
                "A finalized failure must not reenter forward qualification"
            ),
        ):
            self.assertIn(self.activate().status, {"completed", "adopted"})
        record = self.adapter.current()
        self.assertFalse(record.published)
        self.assertIsNotNone(record.failure)
        assert (
            record.application_readiness is not None
            and record.application_readiness.private_health is not None
        )
        self.assertEqual(record.application_readiness.private_health.status, "fail")

    def test_unreadable_configuration_after_correction_keeps_original_unknown(self) -> None:
        self.prepare()
        provider = self.provider
        unavailable = []

        def uncertain(**kwargs: Any) -> Any:
            result = provider(**kwargs)
            if kwargs["path"] == "/api/application.saveDockerProvider":
                self.correct_runtime()
                unavailable.append(True)
                raise OSError("Lost configuration response")
            return result

        read = __import__(
            "control_plane.dokploy.api", fromlist=["fetch_dokploy_target_payload"]
        ).fetch_dokploy_target_payload

        def fetch(**kwargs: Any) -> Any:
            if unavailable and kwargs["target_id"] != self.old_id:
                raise OSError("Original candidate configuration cannot be read")
            return read(**kwargs)

        with (
            patch("control_plane.dokploy.api.dokploy_request", side_effect=uncertain),
            patch("control_plane.dokploy.api.fetch_dokploy_target_payload", side_effect=fetch),
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        self.assertIsNone(self.adapter.current().failure)
        self.assertEqual(self.adapter.current().phase, "configure_application")
        self.assertFalse(self.deployments.get(self.adapter.current().target_id))

    def unhealthy_retained_runtime(self, *, omit_identity_default: bool = False) -> None:
        """Exact exited original plus same-template prospective replacement."""
        name = self.payloads[self.old_id]["appName"]
        task = self.tasks[name + "-task"]
        task["DesiredState"] = "shutdown"
        task["Status"]["State"] = "failed"
        container = self.containers[name + "-container"]
        container["State"]["Status"] = "exited"
        container["NetworkSettings"]["Networks"] = {}
        prospective = deepcopy(task)
        prospective.update(ID=name + "-prospective", DesiredState="running")
        prospective["Status"] = {"State": "preparing", "ContainerStatus": {}}
        self.tasks[prospective["ID"]] = prospective
        if omit_identity_default:
            configs = [
                self.services[name]["Spec"]["TaskTemplate"]["ContainerSpec"],
                *(
                    row["Spec"]["ContainerSpec"]
                    for row in self.tasks.values()
                    if row["ServiceID"] == self.services[name]["ID"]
                ),
                container["Config"],
            ]
            for configuration in configs:
                environment = dict(item.split("=", 1) for item in configuration["Env"])
                identity = json.loads(environment["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])
                identity.pop("release_tuple_id", None)
                environment["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"] = json.dumps(identity)
                configuration["Env"] = [key + "=" + value for key, value in environment.items()]

    def test_retained_stored_inputs_survive_cli_defaults_and_original_reentry(self) -> None:
        from control_plane.contracts.canonical_json import canonical_json_sha256

        self.cli_container_defaults = {"DNSConfig": {}, "StopGracePeriod": 17}
        routing, _, _ = self.private_addressed()
        self.prepare()
        witness = self.adapter.preflight_application_route(self.planned)
        assert witness is not None
        displayed = deepcopy(self.services["old-app"]["Spec"]["TaskTemplate"])
        displayed["ContainerSpec"].update(self.cli_container_defaults)
        self.assertEqual(witness.task_template_sha256, canonical_json_sha256(displayed))
        original = self.planned.model_copy(update={"predecessor": witness})
        self.assertEqual(self.adapter.preflight_application_route(original), witness)
        result = self.activate()
        self.assertEqual(result.status, "completed", result)
        self.assertTrue(self.adapter.current().published)
        self.assertGreater(routing.effect_count, 0)
        effects = routing.effect_count
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(routing.effect_count, effects)

    def test_retained_stored_service_witness_and_complete_task_inputs_are_required(self) -> None:
        self.cli_container_defaults = {"DNSConfig": {}, "StopGracePeriod": 17}
        routing, _, _ = self.private_addressed()
        self.prepare()
        for configuration in (
            self.services["old-app"]["Spec"]["TaskTemplate"]["ContainerSpec"],
            self.tasks["old-app-task"]["Spec"]["ContainerSpec"],
            self.containers["old-app-container"]["Config"],
        ):
            configuration["Env"].append("FIXTURE_INPUT=fixture-private-value")
        witness = self.adapter.preflight_application_route(self.planned)
        assert witness is not None
        record = self.planned.model_copy(update={"predecessor": witness})
        self.adapter.route_admission(record)
        service = deepcopy(self.services["old-app"])
        tasks = deepcopy(self.tasks)
        for problem in (
            "id",
            "name",
            "version",
            "boolean-version",
            "missing",
            "display-drift",
            "race",
        ):
            with self.subTest(problem=problem):
                self.services["old-app"] = deepcopy(service)
                self.stored_service_view = None
                self.after_stored_service_read = None

                def change(raw: Any) -> Any:
                    if problem == "id":
                        raw["ID"] = "foreign-service"
                    elif problem == "name":
                        raw["Spec"]["Name"] = "foreign-name"
                    elif problem in {"version", "boolean-version"}:
                        raw["Version"]["Index"] = True if problem == "boolean-version" else 2
                    elif problem == "missing":
                        return None
                    elif problem == "display-drift":
                        raw["Spec"]["TaskTemplate"]["ContainerSpec"]["Command"] = ["foreign"]
                    return raw

                self.stored_service_view = change
                if problem == "race":
                    self.after_stored_service_read = lambda: self.services["old-app"][
                        "Version"
                    ].update(Index=2)
                with self.assertRaises(ValueError):
                    self.adapter.route_admission(record)
                self.assertEqual(routing.effect_count, 0)
        self.stored_service_view = self.after_stored_service_read = None
        for problem in (
            "explicit-display-values",
            "shutdown-mismatch",
            "env-order",
            "unknown",
            "null",
        ):
            with self.subTest(problem=problem):
                self.services["old-app"] = deepcopy(service)
                self.tasks = deepcopy(tasks)
                configuration = self.services["old-app"]["Spec"]["TaskTemplate"]["ContainerSpec"]
                task = self.tasks["old-app-task"]
                task_configuration = task["Spec"]["ContainerSpec"]
                if problem in {"explicit-display-values", "shutdown-mismatch"}:
                    configuration.update(self.cli_container_defaults)
                    if problem == "shutdown-mismatch":
                        task["DesiredState"] = "shutdown"
                elif problem == "env-order":
                    task_configuration["Env"].reverse()
                elif problem == "unknown":
                    task_configuration["FutureInput"] = "fixture-private-value"
                else:
                    task_configuration["DNSConfig"] = None
                with self.assertRaises(ValueError):
                    self.adapter.route_admission(record)
                self.assertEqual(routing.effect_count, 0)
        self.services["old-app"] = service
        self.tasks = tasks
        self.assertEqual(self.adapter.preflight_application_route(record), witness)

    def test_complete_service_inventory_resolves_prefixes_and_refuses_incomplete_views(
        self,
    ) -> None:
        routing, _, _ = self.private_addressed()
        self.prepare()
        neighbor = deepcopy(self.services["old-app"])
        neighbor["ID"] = hashlib.sha256(b"neighbor-service").hexdigest()[:25]
        neighbor["Spec"]["Name"] = "neighbor"
        neighbor["Spec"]["TaskTemplate"]["Networks"][0]["Aliases"] = ["neighbor"]
        self.services["neighbor"] = neighbor
        self.adapter.preflight_application_route(self.planned)
        for problem in ("missing", "reordered", "duplicate", "foreign", "malformed", "reread"):

            def resolve(rows: list[Any]) -> list[Any]:
                if problem == "missing":
                    return rows[:-1]
                if problem == "reordered":
                    return list(reversed(rows))
                if problem == "duplicate":
                    return [rows[0], rows[0]]
                if problem == "foreign":
                    return ["foreign-service", *rows[1:]]
                if problem == "malformed":
                    return [{"id": rows[0]}, *rows[1:]]
                return rows

            with self.subTest(problem=problem):
                self.service_id_resolution = resolve
                if problem == "reread":
                    list_calls = 0

                    def change_inventory() -> None:
                        nonlocal list_calls
                        list_calls += 1
                        if list_calls == 2:
                            neighbor["ID"] = hashlib.sha256(b"changed-neighbor").hexdigest()[:25]

                    self.before_service_list = change_inventory
                with self.assertRaises(ValueError):
                    self.adapter.preflight_application_route(self.planned)
                self.assertEqual(routing.effect_count, 0)
                self.before_service_list = None
                neighbor["ID"] = hashlib.sha256(b"neighbor-service").hexdigest()[:25]
        self.service_id_resolution = None
        original = neighbor["ID"]
        neighbor["ID"] = self.services["old-app"]["ID"][:12] + "x" * 13
        with self.assertRaises(ValueError):
            self.adapter.preflight_application_route(self.planned)
        self.assertEqual(routing.effect_count, 0)
        neighbor["ID"] = original
        self.adapter.preflight_application_route(self.planned)

    def test_cli_defaults_do_not_replace_actual_immutable_image_proof(self) -> None:
        self.cli_container_defaults = {"DNSConfig": {}, "StopGracePeriod": 17}
        routing, _, _ = self.private_addressed()
        self.prepare()
        configuration = self.services["old-app"]["Spec"]["TaskTemplate"]["ContainerSpec"]
        configuration["Image"] = "ghcr.io/example/site:retained-tag"
        task = self.tasks["old-app-task"]
        task["Spec"]["ContainerSpec"] = deepcopy(configuration)
        self.containers["old-app-container"]["Config"]["Image"] = configuration["Image"]
        self.adapter.preflight_application_route(self.planned)
        task["Status"] = {"State": "ready", "ContainerStatus": {}}
        self.containers.pop("old-app-container")
        with self.assertRaises(ValueError) as caught:
            self.adapter.preflight_application_route(self.planned)
        self.assertEqual(getattr(caught.exception, "container_fields", None), ())
        self.assertGreater(self.stored_service_reads, 0)
        self.assertEqual(routing.effect_count, 0)

    def test_retained_template_refusal_reports_fields_without_input_values(self) -> None:
        from control_plane.http_error_evidence import log_handled_error_cause

        routing, _, _ = self.private_addressed()
        self.prepare()
        private = "private-retained-input-sentinel"
        template = self.services["old-app"]["Spec"]["TaskTemplate"]["ContainerSpec"]
        template["DNSConfig"] = {}
        task = self.tasks["old-app-task"]["Spec"]["ContainerSpec"]
        task["Env"].append("PRIVATE_FIXTURE=" + private)
        task["Command"] = [private]
        task[private] = private
        for desired_state in ("running", "shutdown"):
            with self.subTest(desired_state=desired_state):
                self.tasks["old-app-task"]["DesiredState"] = desired_state
                with self.assertRaises(ValueError) as raised:
                    self.adapter.preflight_application_route(self.planned)
                with self.assertLogs("control_plane.http_error_evidence", level="WARNING") as logs:
                    log_handled_error_cause(raised.exception, "retained-template-test")
                text = logs.records[-1].getMessage()
                fields = json.loads(text)["native_programs"][0]["container_fields"]
                self.assertEqual(
                    {item["field"]: item["difference"] for item in fields},
                    {
                        "DNSConfig": "service_only",
                        "Command": "task_only",
                        "Env": "unequal",
                        "other": "unequal",
                    },
                )
                self.assertNotIn(private, text)
                self.assertEqual(routing.effect_count, 0)

    def test_retained_native_template_scope_and_execution_controls(self) -> None:
        routing, _, _ = self.private_addressed()
        self.prepare()
        witness = self.adapter.preflight_application_route(self.planned)
        assert witness is not None
        record = self.planned.model_copy(update={"predecessor": witness})
        original_service = deepcopy(self.services["old-app"])
        original_tasks = deepcopy(self.tasks)
        original_containers = deepcopy(self.containers)
        original_nodes = deepcopy(self.nodes)
        original_images = deepcopy(self.images)

        def reset() -> None:
            self.services = {"old-app": deepcopy(original_service)}
            self.tasks = deepcopy(original_tasks)
            self.containers.clear()
            self.containers.update(deepcopy(original_containers))
            self.nodes = deepcopy(original_nodes)
            self.images = deepcopy(original_images)
            self.image_environment = []

        # Immutable image defaults are real merged container inputs, not a
        # change to TaskSpec. Order is irrelevant; overriding a task key is not.
        self.image_environment = ["PATH=/usr/local/bin:/usr/bin"]
        self.containers["old-app-container"]["Config"]["Env"].append(self.image_environment[0])
        self.containers["old-app-container"]["Config"]["Env"].reverse()
        self.adapter.route_admission(record)
        self.containers["old-app-container"]["Config"]["Env"].append("UNPROVEN_DEFAULT=foreign")
        with self.assertRaises(ValueError):
            self.adapter.route_admission(record)
        reset()
        self.unhealthy_retained_runtime()
        admission = self.adapter.route_admission(record)
        assert admission is not None
        transport, args = admission
        self.assertEqual(set(args["old_backend_addresses"]), {args["old_backend"]})
        self.assertEqual(transport.old_runtime_identity, witness.runtime_identity)
        # Replacement between complete observations is ordinary progress.
        pending = self.tasks.pop("old-app-prospective")
        pending["ID"] += "-replacement"
        self.tasks[pending["ID"]] = pending
        self.adapter.route_admission(record)

        for problem in (
            "recreated-service",
            "template-mounts",
            "template-env",
            "template-command",
            "template-network",
            "missing-template",
            "unmapped-identity",
            "mixed-task",
            "image",
            "foreign-alias",
            "foreign-service-alias",
            "no-shared-network",
            "multi-node",
            "nonlocal-node",
            "foreign-task-node",
            "actual-image",
            "tag-only-prospective",
        ):
            with self.subTest(problem=problem):
                reset()
                service = self.services["old-app"]
                template = service["Spec"]["TaskTemplate"]
                if problem == "recreated-service":
                    service["ID"] = "recreated-service"
                elif problem.startswith("template-"):
                    key = problem.removeprefix("template-")
                    if key == "network":
                        template["Networks"] = [{"Target": "foreign-network"}]
                    elif key == "mounts":
                        template["ContainerSpec"]["Mounts"] = []
                    elif key == "env":
                        template["ContainerSpec"]["Env"].append("EXTRA_INPUT=changed")
                    else:
                        template["ContainerSpec"]["Command"] = ["different-entrypoint"]
                elif problem == "missing-template":
                    service["Spec"].pop("TaskTemplate")
                elif problem == "unmapped-identity":
                    template["ContainerSpec"]["Env"] = [
                        "LAUNCHPLANE_RUNTIME_IDENTITY_JSON="
                        + json.dumps({"deployment_record_id": "foreign"})
                    ]
                elif problem == "mixed-task":
                    self.tasks["old-app-task"]["Spec"]["ContainerSpec"]["Mounts"] = []
                elif problem == "image":
                    template["ContainerSpec"]["Image"] = "foreign/app@sha256:" + "c" * 64
                elif problem == "foreign-alias":
                    container = deepcopy(self.containers["old-app-container"])
                    container["Id"] = "foreign-container"
                    container["Config"]["Labels"] = {}
                    self.containers[container["Id"]] = container
                elif problem == "foreign-service-alias":
                    other = deepcopy(service)
                    other["ID"], other["Spec"]["Name"] = "foreign-service", "other"
                    self.services["other"] = other
                elif problem == "no-shared-network":
                    self.containers["proxy"]["NetworkSettings"]["Networks"]["dokploy-network"][
                        "NetworkID"
                    ] = "other"
                elif problem == "multi-node":
                    self.nodes.append({"ID": "other", "Self": False})
                elif problem == "nonlocal-node":
                    self.nodes[0]["Self"] = False
                elif problem == "foreign-task-node":
                    self.tasks["old-app-task"]["NodeID"] = "other"
                elif problem == "tag-only-prospective":
                    template["ContainerSpec"]["Image"] = "ghcr.io/example/site:immutable-tag"
                    self.tasks["old-app-task"]["Spec"]["ContainerSpec"] = deepcopy(
                        template["ContainerSpec"]
                    )
                    self.tasks["old-app-task"]["Status"] = {"ContainerStatus": {}}
                    self.containers.pop("old-app-container")
                else:
                    self.unhealthy_retained_runtime()
                    self.images[self.containers["old-app-container"]["Image"]] = (
                        "example/other@sha256:" + "c" * 64
                    )
                observation = (
                    self.planned
                    if problem
                    in {"unmapped-identity", "image", "missing-template", "tag-only-prospective"}
                    else record
                )
                with self.assertRaises(ValueError):
                    self.adapter.route_admission(observation)
                self.assertFalse(routing.route_file.exists())
                self.assertFalse(self.projects)
        reset()
        docker = self.docker
        inserted = False

        def insert_during_snapshot(arguments: list[str], **kwargs: Any) -> Any:
            nonlocal inserted
            result = docker(arguments, **kwargs)
            if arguments[1:3] == ["image", "inspect"] and not inserted:
                inserted = True
                task = deepcopy(self.tasks["old-app-task"])
                task["ID"] = "foreign-input-task"
                task["Spec"]["ContainerSpec"]["Mounts"] = []
                self.tasks[task["ID"]] = task
            return result

        with (
            patch.object(self, "docker", side_effect=insert_during_snapshot),
            self.assertRaises(ValueError),
        ):
            self.adapter.route_admission(record)
        self.assertTrue(inserted)
        reset()
        node_reads = 0

        def change_scope(arguments: list[str], **kwargs: Any) -> Any:
            nonlocal node_reads
            if arguments[1:3] == ["node", "ls"]:
                node_reads += 1
                if node_reads > 1:
                    self.nodes.append({"ID": "new-node", "Self": False})
            return docker(arguments, **kwargs)

        with patch.object(self, "docker", side_effect=change_scope), self.assertRaises(ValueError):
            self.adapter.route_admission(record)

    def unfinished_private_forward(self, problem: str) -> tuple[Any, Any, Any]:
        """Stop the actual original child at a positively undispatched boundary."""
        import fcntl

        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.testing_route_effects import (
            ROUTE_PREPARING,
            ROUTE_WRITING,
            TestingRouteEffects,
            TestingRouteFenceController,
        )

        routing, _, _ = self.private_addressed()
        self.prepare()
        injected: list[bool] = []
        checkpoints: list[Any] = []
        held: list[Any] = []
        apply = TestingRouteEffects.apply
        ensure = TestingRouteFenceController.ensure
        checkpoint = self.store.checkpoint_mutation_provider_effect
        path = ROUTE.format(product=self.product) + ":route"

        def remember(**kwargs: Any) -> Any:
            result = checkpoint(**kwargs)
            if kwargs["reservation"].route_path == path and result.record is not None:
                checkpoints.append(result.record)
            return result

        def empty(effects: Any, lease: Any) -> Any:
            if not effects.inverse and not injected:
                effects.controller.guard()
                injected.append(True)
                raise NativeSourceUnavailable("Original guard answered before preparing checkpoint")
            return apply(effects, lease)

        def preparing(controller: Any, *args: Any, **kwargs: Any) -> Any:
            if not injected:
                injected.append(True)
                raise NativeSourceUnavailable(
                    "Original preparing read unavailable before initializer"
                )
            return ensure(controller, *args, **kwargs)

        def busy() -> None:
            def hold_original_lock() -> None:
                handle = (routing.root / ("." + routing.route_file.name + ".lock")).open("rb")
                held.append(handle)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                injected.append(True)

            # The real native writer runs after its guarded WRITING checkpoint.
            routing.before_native = hold_original_lock

        routing.before_publish = busy if problem == "busy" else None
        try:
            with (
                patch.object(TestingRouteEffects, "apply", empty if problem == "empty" else apply),
                patch.object(
                    TestingRouteFenceController,
                    "ensure",
                    preparing if problem == "preparing" else ensure,
                ),
                patch.object(
                    self.store, "checkpoint_mutation_provider_effect", side_effect=remember
                ),
                patch.object(
                    self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
                ),
            ):
                self.assertEqual(self.activate().status, "reconcile_required")
        finally:
            routing.before_publish = None
            for handle in held:
                handle.close()
        self.assertTrue(injected)
        record = self.adapter.current()
        child = self.store.read_idempotency_record(
            scope=record.scope, route_path=path, idempotency_key=record.plan_sha256 + ":route"
        )
        assert child is not None
        self.assertEqual(child.state, "reconcile_required")
        self.assertEqual(
            child.provider_effect_phase,
            {"empty": "", "preparing": ROUTE_PREPARING, "busy": ROUTE_WRITING}[problem],
        )
        self.assertFalse(routing.route_file.exists())
        return routing, child, checkpoints[-1] if checkpoints else None

    def assert_unfinished_private_forward_recovers_and_successor(self, problem: str) -> None:
        from control_plane.testing_application_activation import (
            completed_application_forward_exclusion,
            completed_application_inverse,
        )
        from control_plane.testing_route_effects import ROUTE_WRITING

        routing, child, stale = self.unfinished_private_forward(problem)
        intent = self.store.read_original_mutation_intent(child)
        effects = routing.effect_count
        projects = deepcopy(self.projects)
        self.correct_runtime()
        result = self.activate()
        self.assertIn(result.status, {"completed", "adopted"}, result)
        refused = self.adapter.current()
        assert refused.failure is not None
        self.assertEqual(refused.failure.kind, "publication_refused")
        self.assertTrue(completed_application_forward_exclusion(self.store, refused))
        self.assertTrue(completed_application_inverse(self.store, refused))
        self.assertFalse(routing.route_file.exists())
        self.assertEqual(routing.effect_count, effects)
        self.assertEqual(self.projects, projects)
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )
        completed = self.store.read_idempotency_record(
            scope=child.scope, route_path=child.route_path, idempotency_key=child.idempotency_key
        )
        assert completed is not None
        self.assertEqual(completed.attempt, child.attempt)
        self.assertEqual(self.store.read_original_mutation_intent(completed), intent)
        if stale is not None:
            checkpoint = self.store.checkpoint_mutation_provider_effect(
                reservation=stale, effect_phase=ROUTE_WRITING
            )
            self.assertNotEqual(checkpoint.status, "updated")
        self.assertEqual(self.activate().status, "replayed")
        self.activation_key = "after-unfinished-forward"
        self.prepare(self.activation_key)
        self.assertEqual(self.activate().status, "completed")
        self.assertTrue(self.adapter.current().published)
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=child.scope,
                route_path=child.route_path,
                idempotency_key=child.idempotency_key,
            ),
            completed,
        )

    def test_unfinished_private_forward_recovery_and_fresh_successor(self) -> None:
        for problem in ("empty", "preparing", "busy"):
            with self.subTest(problem=problem):
                outer_runs, outer_writes = list(self.schedule_runs), list(self.writes)
                fixture = ApplicationFlowTests()
                fixture.setUp()
                try:
                    fixture.assert_unfinished_private_forward_recovers_and_successor(problem)
                finally:
                    fixture.doCleanups()
                self.assertEqual(self.schedule_runs, outer_runs)
                self.assertEqual(self.writes, outer_writes)

    def test_unfinished_forward_adoption_loses_to_actual_new_writing_attempt(self) -> None:
        from control_plane.testing_route_effects import ROUTE_WRITING

        for timing in ("active", "before-observe", "before-adopt"):
            with self.subTest(timing=timing):
                fixture = ApplicationFlowTests()
                fixture.setUp()
                try:
                    routing, child, _ = fixture.unfinished_private_forward("busy")
                    fixture.correct_runtime()
                    effects = routing.effect_count
                    reserve = fixture.store.reserve_mutation
                    adopt = fixture.store.adopt_reconciled_mutation
                    advanced = []
                    adoptions = []

                    def advance() -> None:
                        retried = fixture.store.retry_reconciled_mutation(
                            reservation=child, lease_owner="competing-original-worker"
                        )
                        fixture.assertEqual(retried.status, "acquired")
                        assert retried.record is not None
                        writing = fixture.store.checkpoint_mutation_provider_effect(
                            reservation=retried.record, effect_phase=ROUTE_WRITING
                        )
                        fixture.assertEqual(writing.status, "updated")
                        assert writing.record is not None
                        current = writing.record
                        if timing != "active":
                            fenced = fixture.store.mark_mutation_reconcile_required(
                                reservation=current, reconciliation_key=current.reconciliation_key
                            )
                            fixture.assertEqual(fenced.status, "updated")
                            assert fenced.record is not None
                            current = fenced.record
                        advanced.append(current)

                    def racing_reserve(**kwargs: Any) -> Any:
                        if kwargs["route_path"] == child.route_path and not advanced:
                            advance()
                        return reserve(**kwargs)

                    def racing_adopt(**kwargs: Any) -> Any:
                        if kwargs["reservation"].route_path == child.route_path:
                            if not advanced:
                                advance()
                            result = adopt(**kwargs)
                            adoptions.append(result)
                            return result
                        return adopt(**kwargs)

                    with (
                        patch.object(fixture.store, "reserve_mutation", side_effect=racing_reserve)
                        if timing in {"active", "before-observe"}
                        else nullcontext(),
                        patch.object(
                            fixture.store, "adopt_reconciled_mutation", side_effect=racing_adopt
                        ),
                    ):
                        fixture.assertEqual(fixture.activate().status, "reconcile_required")
                    fixture.assertTrue(advanced)
                    fixture.assertEqual(bool(adoptions), timing == "before-adopt")
                    for result in adoptions:
                        fixture.assertEqual(result.status, "reservation_mismatch")
                    current = fixture.store.read_idempotency_record(
                        scope=child.scope,
                        route_path=child.route_path,
                        idempotency_key=child.idempotency_key,
                    )
                    fixture.assertEqual(current, advanced[0])
                    fixture.assertFalse(routing.route_file.exists())
                    fixture.assertEqual(routing.effect_count, effects)
                    fixture.assertIsNone(
                        fixture.store.read_idempotency_record(
                            scope=child.scope,
                            route_path=child.route_path + "-inverse",
                            idempotency_key=child.idempotency_key + "-inverse",
                        )
                    )
                    fixture.assertEqual(fixture.canonical_password(), "retained-only-password")
                finally:
                    fixture.doCleanups()

    def test_private_candidate_must_remain_running_at_native_dispatch(self) -> None:
        for problem in ("no-container", "exited-container"):
            with self.subTest(problem=problem):
                fixture = ApplicationFlowTests()
                fixture.setUp()
                try:
                    fixture.assert_excluded_private_forward_refuses_and_successor(problem)
                finally:
                    fixture.doCleanups()

    def assert_excluded_private_forward_refuses_and_successor(self, problem: str) -> None:
        from control_plane.testing_application_activation import (
            completed_application_forward_exclusion,
            completed_application_inverse,
        )
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE
        from control_plane.testing_route_effects import ROUTE_WRITER_RECEIPT
        from control_plane.dokploy.testing_route import route_document
        from control_plane.contracts.canonical_json import canonical_json_sha256
        from control_plane.dokploy.testing_route_execution import TestingRouteExecutionIntent

        routing, _, _ = self.private_addressed()
        self.unhealthy_retained_runtime()
        self.prepare()
        changed = []

        def change_candidate() -> None:
            record = self.adapter.current()
            name = record.app_name
            changed.append(routing.effect_count)
            if problem == "no-container":
                self.tasks[name + "-task"]["Status"] = {"State": "preparing", "ContainerStatus": {}}
            else:
                self.containers[name + "-container"]["State"]["Status"] = "exited"

        routing.before_publish = change_candidate
        result = self.activate()
        self.assertEqual(result.status, "completed", result)
        refused = self.adapter.current()
        assert refused.failure is not None and refused.application_readiness is not None
        self.assertEqual(refused.failure.kind, "publication_refused")
        self.assertFalse(refused.published)
        self.assertTrue(changed)
        self.assertFalse(routing.route_file.exists())
        self.assertEqual(routing.effect_count, changed[0])
        self.assertTrue(completed_application_forward_exclusion(self.store, refused))
        self.assertTrue(completed_application_inverse(self.store, refused))
        readiness = refused.application_readiness
        assert readiness.route is not None and readiness.route_execution is not None
        forward = self.store.read_idempotency_record(
            scope=refused.scope,
            route_path=ROUTE.format(product=self.product) + ":route",
            idempotency_key=refused.plan_sha256 + ":route",
        )
        assert forward is not None
        physical = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_WRITER_RECEIPT,
            idempotency_key=readiness.route_execution.key(attempt=forward.attempt),
        )
        assert physical is not None
        self.assertEqual(physical.response_payload.get("writer_outcome"), "excluded")
        inverse = self.store.read_idempotency_record(
            scope=refused.scope,
            route_path=ROUTE.format(product=self.product) + ":route-inverse",
            idempotency_key=refused.plan_sha256 + ":route-inverse",
        )
        assert inverse is not None
        restored = TestingRouteExecutionIntent.model_validate(
            self.store.read_original_mutation_intent(inverse)["route_execution"]
        )
        expected = readiness.route_execution.model_copy(
            update={
                "backend": readiness.route.old_backend,
                "document_sha256": canonical_json_sha256(
                    route_document(readiness.route, readiness.route.old_backend)
                ),
            }
        )
        self.assertEqual(
            restored.model_dump(exclude={"custody"}), expected.model_dump(exclude={"custody"})
        )
        from control_plane.testing_activation_inspection import inspect_application_route_receipts

        inspected = inspect_application_route_receipts(self.store, refused)
        self.assertEqual(inspected[1].status, "bound")
        self.assertEqual(inspected[1].state, inverse.state)
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(routing.effect_count, changed[0])
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )
        # Eligibility is bound to the exact original receipt and intent, never
        # a busy/unknown outcome, merely truthy marker or foreign operation.
        read = self.store.read_idempotency_record
        for updates in (
            {"state": "reconcile_required"},
            {"response_payload": {"route_write_not_started": 1}},
            {"response_payload": {"writer_outcome": "busy"}},
            {"request_fingerprint": "foreign"},
            {"reconciliation_key": "foreign"},
            {"provider_target_key": "foreign"},
            {"scope": "foreign"},
            {"route_path": "foreign"},
            {"idempotency_key": "foreign"},
        ):
            with self.subTest(receipt=updates):
                with patch.object(
                    self.store,
                    "read_idempotency_record",
                    return_value=forward.model_copy(update=updates),
                ):
                    self.assertFalse(completed_application_forward_exclusion(self.store, refused))
        for intent in (
            {},
            {
                "route_execution": readiness.route_execution.model_copy(
                    update={"operation_id": "foreign"}
                ).model_dump(mode="json")
            },
        ):
            with patch.object(self.store, "read_original_mutation_intent", return_value=intent):
                self.assertFalse(completed_application_forward_exclusion(self.store, refused))
        routing.before_publish = None
        self.activation_key = "after-excluded-forward"
        self.prepare("after-excluded-forward")
        self.assertEqual(self.activate().status, "completed")
        self.assertTrue(self.adapter.current().published)
        self.assertEqual(
            read(
                scope=forward.scope,
                route_path=forward.route_path,
                idempotency_key=forward.idempotency_key,
            ),
            forward,
        )
        self.assertEqual(
            read(
                scope=physical.scope,
                route_path=physical.route_path,
                idempotency_key=physical.idempotency_key,
            ),
            physical,
        )

    def test_excluded_forward_lost_receipts_and_staff_wait_keep_original_custody(self) -> None:
        from control_plane.testing_application_activation import completed_application_inverse
        from control_plane.testing_route_effects import ROUTE_WRITER_RECEIPT

        for loss in ("physical", "logical"):
            with self.subTest(loss=loss):
                fixture = ApplicationFlowTests()
                fixture.setUp()
                try:
                    routing, _, _ = fixture.private_addressed()
                    fixture.unhealthy_retained_runtime()
                    fixture.prepare()
                    complete = fixture.store.complete_mutation_reservation
                    lost: list[Any] = []

                    def change_candidate() -> None:
                        name = fixture.adapter.current().app_name
                        fixture.containers[name + "-container"]["State"]["Status"] = "exited"

                    def lose(**kwargs: Any) -> Any:
                        completion = kwargs["completion"]
                        result = complete(**kwargs)
                        matching = (
                            completion.route_path == ROUTE_WRITER_RECEIPT
                            and completion.response_payload.get("writer_outcome") == "excluded"
                            if loss == "physical"
                            else completion.route_path
                            == ROUTE.format(product=fixture.product) + ":route"
                            and completion.response_payload.get("route_write_not_started") is True
                        )
                        if matching and not lost:
                            lost.append(completion)
                            fixture.record_staff_usage("in_use")
                            raise OSError("Lost actual completed exclusion reply")
                        return result

                    routing.before_publish = change_candidate
                    with (
                        patch.object(
                            fixture.store, "complete_mutation_reservation", side_effect=lose
                        ),
                        patch.object(
                            fixture.store,
                            "_database_mutation_timestamp",
                            return_value=fixture.old.now(-600),
                        ),
                    ):
                        self.assertEqual(fixture.activate().status, "reconcile_required")
                    self.assertTrue(lost)
                    pending = fixture.adapter.current()
                    assert pending.application_readiness is not None
                    private = pending.application_readiness.private_health
                    effects = routing.effect_count
                    self.assertFalse(completed_application_inverse(fixture.store, pending))
                    self.assertEqual(fixture.activate().status, "reconcile_required")
                    self.assertEqual(routing.effect_count, effects)
                    fixture.record_staff_usage("unused")
                    routing.before_publish = None
                    self.assertIn(fixture.activate().status, {"completed", "adopted"})
                    refused = fixture.adapter.current()
                    assert refused.failure is not None and refused.application_readiness is not None
                    self.assertEqual(refused.failure.kind, "publication_refused")
                    self.assertEqual(refused.application_readiness.private_health, private)
                    self.assertTrue(completed_application_inverse(fixture.store, refused))
                    self.assertEqual(routing.effect_count, effects)
                    self.assertFalse(routing.route_file.exists())
                finally:
                    fixture.doCleanups()

    def test_lost_successful_forward_candidate_death_keeps_original_private_pass(self) -> None:
        from control_plane.outbound_http import PublicHttpResponse
        from control_plane.testing_application_activation import (
            completed_application_forward_exclusion,
            completed_application_inverse,
        )
        from control_plane.contracts.testing_activation import TestingActivationRefusal
        from control_plane.dokploy.testing_route import observe_testing_route

        routing, _, endpoint = self.private_addressed()
        self.prepare()
        native = routing.command
        lost: list[bool] = []

        def lose(arguments: list[str], **kwargs: Any) -> Any:
            file_was_present = routing.route_file.exists()
            result = native(arguments, **kwargs)
            if not lost and not file_was_present and routing.route_file.exists():
                lost.append(True)
                raise subprocess.TimeoutExpired(arguments, 1)
            return result

        with (
            patch.object(routing, "command", side_effect=lose),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        admitted = self.adapter.current()
        readiness = admitted.application_readiness
        assert (
            readiness is not None
            and readiness.private_health is not None
            and readiness.route is not None
        )
        self.assertFalse(completed_application_forward_exclusion(self.store, admitted))
        self.assertEqual(readiness.private_health.status, "pass")
        # A persisted route cannot substitute missing or forged private admission.
        for health in (
            None,
            readiness.private_health.model_copy(update={"verified": False}),
            readiness.private_health.model_copy(update={"observed_runtime_identity": None}),
        ):
            invalid = admitted.model_copy(
                update={
                    "application_readiness": readiness.model_copy(update={"private_health": health})
                }
            )
            with self.assertRaises(TestingActivationRefusal):
                self.adapter.qualify_application(invalid)
        name = admitted.app_name
        self.tasks[name + "-task"]["DesiredState"] = "shutdown"
        self.tasks[name + "-task"]["Status"]["State"] = "failed"
        self.containers[name + "-container"]["State"]["Status"] = "exited"
        self.containers[name + "-container"]["NetworkSettings"]["Networks"] = {}
        from control_plane.testing_application_runtime import VerifiedApplicationRuntimeFailure

        with self.assertRaises(VerifiedApplicationRuntimeFailure):
            self.inspect_current_private_runtime()
        qualify = self.adapter.qualify_application
        reentered = []

        def retain_admission(record: Any) -> Any:
            self.assertFalse(completed_application_forward_exclusion(self.store, record))
            assert record.application_readiness is not None
            self.assertIsNotNone(record.application_readiness.route)
            reentered.append(record)
            return qualify(record)

        with (
            patch(
                "control_plane.testing_private_health.request_private_edge_health",
                return_value=PublicHttpResponse(503, endpoint.url, 0, (), b"{}"),
            ),
            patch.object(self.adapter, "qualify_application", side_effect=retain_admission),
        ):
            result = self.activate()
        self.assertTrue(reentered)
        self.assertIn(result.status, {"completed", "adopted"}, result)
        refused = self.adapter.current()
        assert refused.failure is not None and refused.application_readiness is not None
        self.assertEqual(refused.failure.kind, "publication_refused")
        self.assertEqual(refused.application_readiness.private_health, readiness.private_health)
        self.assertTrue(completed_application_inverse(self.store, refused))
        self.assertEqual(
            observe_testing_route(
                readiness.route, routing.raw, json.loads(routing.route_file.read_text())
            ),
            readiness.route.old_backend,
        )

    def test_pre_forward_authority_refusal_without_owned_file_admits_fresh_generation(self) -> None:
        from control_plane.testing_application_activation import completed_application_inverse

        routing, _, _ = self.private_addressed()
        self.prepare()
        effect = self.adapter.route_effect
        corrected: list[bool] = []

        def correct(*args: Any, **kwargs: Any) -> Any:
            if not kwargs["inverse"] and not corrected:
                corrected.append(True)
                self.correct_runtime()
            return effect(*args, **kwargs)

        with patch.object(self.adapter, "route_effect", side_effect=correct):
            self.assertEqual(self.activate().status, "completed")
        refused = self.adapter.current()
        assert refused.failure is not None
        self.assertEqual(refused.failure.kind, "publication_refused")
        self.assertTrue(completed_application_inverse(self.store, refused))
        self.assertFalse(routing.route_file.exists())
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=refused.scope,
                route_path=ROUTE.format(product=self.product) + ":route",
                idempotency_key=refused.plan_sha256 + ":route",
            )
        )
        self.prepare("after-pre-forward-refusal")
        self.activation_key = "after-pre-forward-refusal"
        self.assertEqual(self.activate().status, "completed")
        self.assertTrue(self.adapter.current().published)

    def test_excluded_forward_foreign_candidate_job_keeps_custody_after_inverse(self) -> None:
        from control_plane.testing_application_activation import completed_application_inverse

        routing, _, _ = self.private_addressed()
        self.prepare()

        def foreign_job() -> None:
            record = self.adapter.current()
            self.containers[record.app_name + "-container"]["State"]["Status"] = "exited"
            self.deployments[record.target_id].append(
                {"deploymentId": "foreign", "title": "Unrelated job", "status": "done"}
            )

        routing.before_publish = foreign_job
        self.assertEqual(self.activate().status, "reconcile_required")
        current = self.adapter.current()
        self.assertEqual(current.phase, "restoring_application_route")
        self.assertIsNone(current.failure)
        self.assertFalse(current.published)
        self.assertFalse(routing.route_file.exists())
        self.assertTrue(completed_application_inverse(self.store, current))
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )

    def test_predecessor_is_captured_before_effects_and_cannot_be_rebound(self) -> None:
        self.private_addressed()
        self.prepare()
        from control_plane.contracts.canonical_json import canonical_json_sha256

        from control_plane.dokploy import api

        provider = api.dokploy_request

        def lose_first_create(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/project.create":
                current = self.adapter.current()
                self.assertIsNotNone(current.predecessor)
                self.assertIsNone(current.application_readiness)
                self.assertFalse(self.projects)
                provider(**kwargs)
                raise OSError("Lost the actual first create reply after original witness capture")
            return provider(**kwargs)

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=lose_first_create):
            self.assertEqual(self.activate().status, "reconcile_required")
        captured = self.adapter.current()
        witness = captured.predecessor
        assert witness is not None
        self.assertEqual(witness.service_id, self.services["old-app"]["ID"])
        self.assertEqual(
            witness.task_template_sha256,
            canonical_json_sha256(self.services["old-app"]["Spec"]["TaskTemplate"]),
        )
        projects_before = deepcopy(self.projects)
        for problem in ("service-id", "template"):
            original = deepcopy(self.services["old-app"])
            try:
                with self.subTest(problem=problem):
                    if problem == "service-id":
                        self.services["old-app"]["ID"] += "-recreated"
                    else:
                        self.services["old-app"]["Spec"]["TaskTemplate"]["ContainerSpec"][
                            "Mounts"
                        ] = []
                    self.assertEqual(self.activate().status, "reconcile_required")
                    self.assertEqual(self.adapter.current().predecessor, witness)
                    self.assertEqual(self.projects, projects_before)
            finally:
                self.services["old-app"] = original
        current = self.adapter.current()
        with self.assertRaises(ValueError):
            self.store.write_testing_activation(
                expected=current.model_dump(mode="json"),
                replacement=current.model_copy(update={"predecessor": None}).model_dump(
                    mode="json"
                ),
            )
        self.assertEqual(self.activate().status, "completed")
        readiness = self.adapter.current().application_readiness
        assert readiness is not None
        self.assertEqual(readiness.old_runtime_identity, witness.runtime_identity)

    def assert_private_unhealthy_origin(self, kind: str, *, pending: bool = False) -> None:
        import control_plane.testing_private_health as private
        from control_plane.outbound_http import PublicHttpResponse

        route, _, endpoint = self.private_addressed()
        self.unhealthy_retained_runtime()
        self.prepare()
        original = getattr(private, "request_private_edge_health")
        corrected = [not pending]

        def response(*args: Any, **kwargs: Any) -> Any:
            if not route.route_file.exists() or not corrected[0]:
                return PublicHttpResponse(
                    503 if kind == "http503" else 200, endpoint.url, 0, (), b"{}"
                )
            return original(*args, **kwargs)

        with patch.object(private, "request_private_edge_health", side_effect=response):
            first = self.activate()
            if pending:
                self.assertEqual(first.status, "reconcile_required", first)
                self.assertFalse(self.adapter.current().published)
                self.assertIsNone(self.adapter.current().failure)
                before = (
                    route.route_file.read_bytes(),
                    route.route_file.stat().st_ino,
                    route.effect_count,
                )
                effects = tuple(self.writes)
                corrected[0] = True
                self.assertIn(self.activate().status, {"completed", "adopted"})
                self.assertEqual(
                    (
                        route.route_file.read_bytes(),
                        route.route_file.stat().st_ino,
                        route.effect_count,
                    ),
                    before,
                )
                self.assertEqual(tuple(self.writes), effects)
            else:
                self.assertEqual(first.status, "completed", first)
        current = self.adapter.current()
        assert (
            current.application_readiness is not None
            and current.application_readiness.predecessor_health is not None
        )
        self.assertTrue(current.published)
        predecessor = current.application_readiness.predecessor_health
        self.assertFalse(
            predecessor.status == "pass" and predecessor.runtime_identity_status == "match"
        )
        assert current.application_readiness.public_health is not None
        self.assertEqual(current.application_readiness.public_health.status, "pass")
        self.assertEqual(
            current.application_readiness.public_health.runtime_identity_status, "match"
        )
        self.assertEqual(len(self.deployments[current.target_id]), 1)

    def test_private_unhealthy_http_origin_can_recover_independently(self) -> None:
        self.assert_private_unhealthy_origin("http503")

    def test_private_origin_missing_health_identity_can_recover_independently(self) -> None:
        self.assert_private_unhealthy_origin("missing-identity")

    def test_unqualified_origin_and_edge_remain_pending_until_same_key_qualifies(self) -> None:
        self.assert_private_unhealthy_origin("http503", pending=True)

    def private_addressed(self) -> tuple[Any, Any, Any]:
        from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
        from control_plane.contracts.private_health_endpoint_record import (
            PrivateHealthEndpointRecord,
        )
        from control_plane.testing_private_ingress import (
            TestingPrivateIngressAdapter,
            TestingPrivateIngressRequest,
        )
        from control_plane.dokploy.application_domains import private_application_domain_payload
        from control_plane.outbound_http import PublicHttpResponse
        from tests.test_testing_private_ingress import EdgeClient
        from unittest.mock import Mock
        import yaml

        routing = self.addressed()
        profile = self.store.read_product_profile_record(self.product)
        profile.public_website = "none"
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.base_url = lane.health_url = ""
        self.store.write_product_profile_record(profile)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.domains = target.public_hosts = ()
        self.store.write_dokploy_target_record(target)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=target,
                target_id_record=self.store.read_dokploy_target_id_record(
                    context_name=self.context, instance_name="testing"
                ),
            )
        )
        self.payloads[self.old_id]["domains"] = []
        hostname = routing.plan.host
        endpoint = PrivateHealthEndpointRecord(
            endpoint_key="private-testing",
            product=self.product,
            context=self.context,
            instance="testing",
            url="https://" + hostname + "/health",
            updated_at=profile.updated_at,
        )
        self.store.write_private_health_endpoint_record(endpoint)
        edge = EdgeEndpointRecord(
            endpoint_key="private-http",
            server_name="Fixture server",
            upstream_host="10.20.30.40",
            upstream_scheme="http",
            upstream_port=80,
            updated_at=profile.updated_at,
        )
        self.store.write_edge_endpoint_record(edge)
        from control_plane.npmplus import NpmplusClient
        from control_plane.workflows.ingress_provider import NPMPLUS_BASE_URL_ENV_KEY

        self.enterContext(patch.dict(os.environ, {NPMPLUS_BASE_URL_ENV_KEY: "http://10.21.0.9:81"}))
        client = EdgeClient()
        client.certificate = client.certificate.model_copy(
            update={"domain_names": ("*.example.invalid",)}
        )
        original_provider = self.provider

        def provider(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/server.one":
                return {
                    "serverId": "server",
                    "name": edge.server_name,
                    "ipAddress": edge.upstream_host,
                }
            if kwargs["path"] == "/api/domain.create":
                payload = kwargs["payload"]
                self.payloads[payload["applicationId"]]["domains"].append(
                    {"domainId": "original-private-origin", **payload}
                )
                self.writes.append((kwargs["path"], deepcopy(payload)))
                return self.payloads[payload["applicationId"]]["domains"][-1]
            return original_provider(**kwargs)

        self.enterContext(patch("control_plane.dokploy.api.dokploy_request", side_effect=provider))
        self.enterContext(
            patch(
                "control_plane.dokploy.api.fetch_dokploy_application_domains",
                side_effect=lambda **kwargs: deepcopy(
                    self.payloads[kwargs["application_id"]]["domains"]
                ),
            )
        )
        self.enterContext(
            patch(
                "control_plane.workflows.ingress_provider.build_npmplus_ingress_client_from_env",
                return_value=client,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_private_health.build_npmplus_ingress_client_from_env",
                return_value=client,
            )
        )
        request = TestingPrivateIngressRequest(
            context=self.context,
            endpoint_key=endpoint.endpoint_key,
            edge_endpoint_key=edge.endpoint_key,
            certificate_id=client.certificate.id,
            access_list_id=1,
            reason="Preserve the original private testing authority.",
        )
        ingress = TestingPrivateIngressAdapter(
            self.root,
            self.store,
            self.product,
            request,
            "private-flow",
            cast(NpmplusClient, client),
        )
        digest = ingress.plan()["plan_sha256"]
        ingress.request = request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": digest}
        )
        ingress.apply("original-private-ingress", Mock(spec=ProviderOperationLease))
        # Existing edge keeps TLS; the Dokploy origin is a single plaintext root route.
        doc = json.loads((routing.root / "old-app.yml").read_text())
        doc["http"]["routers"].pop("old-app-router-websecure-fixture")
        doc["http"]["routers"]["old-app-router-fixture"].pop("middlewares")
        doc["http"]["middlewares"] = {}
        (routing.root / "old-app.yml").write_text(json.dumps(doc))
        routing.raw["routers"].pop("old-app-router-websecure-fixture@file")
        routing.raw["routers"]["old-app-router-fixture@file"].pop("middlewares")
        routing.raw["middlewares"] = {}
        self.retained = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )

        def health(*args: Any, **kwargs: Any) -> Any:
            application = "old-app"
            if routing.route_file.exists():
                route = yaml.safe_load(routing.route_file.read_text())
                backend = next(iter(route["http"]["services"].values()))["loadBalancer"]["servers"][
                    0
                ]["url"]
                from urllib.parse import urlsplit

                application = urlsplit(backend).hostname
            container = self.containers[str(application) + "-container"]
            values = dict(item.split("=", 1) for item in container["Config"]["Env"])
            body = json.dumps(
                {"runtime_identity": json.loads(values["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])}
            ).encode()
            return PublicHttpResponse(200, endpoint.url, 0, (), body)

        self.enterContext(
            patch(
                "control_plane.testing_private_health.request_private_edge_health",
                side_effect=health,
            )
        )
        self.assertEqual(
            self.payloads[self.old_id]["domains"][0],
            {
                "domainId": "original-private-origin",
                **private_application_domain_payload(
                    hostname=hostname, runtime_port=profile.runtime_port, application_id=self.old_id
                ),
            },
        )
        return routing, ingress, endpoint

    def test_private_same_address_monitor_recreate_and_second_generation(self) -> None:
        from control_plane.testing_private_health import fetch_testing_private_health
        from control_plane.contracts.private_health_endpoint_record import (
            private_health_endpoint_record_sha256,
        )

        routing, ingress, endpoint = self.private_addressed()
        origin_id = self.old_id
        original_domain = deepcopy(self.payloads[origin_id]["domains"])
        edge_before = deepcopy(ingress.client.proxy_hosts)
        self.unhealthy_retained_runtime()
        self.prepare()
        self.assertEqual(self.activate().status, "completed")
        first = self.adapter.current()
        self.assertTrue(first.published)
        assert first.published_target is not None and first.application_readiness is not None
        self.assertEqual(first.published_target.domains, ())
        self.assertIsNone(first.published_route_binding)
        self.assertIsNotNone(first.application_readiness.route)

        def fetch() -> Any:
            return fetch_testing_private_health(
                root=self.root,
                store=self.store,
                product=self.product,
                context=self.context,
                instance="testing",
                endpoint_key=endpoint.endpoint_key,
                endpoint_sha256=private_health_endpoint_record_sha256(endpoint),
                url=endpoint.url,
                timeout_seconds=3,
            )

        # Superseded actual Swarm resources may be retired independently; the
        # completed lineage still owns the exact selected file/raw route.
        retired_service = self.services.pop("old-app")
        self.tasks = {
            key: value
            for key, value in self.tasks.items()
            if value["ServiceID"] != retired_service["ID"]
        }
        self.containers.pop("old-app-container")
        observed = fetch()
        self.assertEqual(
            json.loads(observed.body)["runtime_identity"]["deployment_record_id"],
            first.application_readiness.runtime_identity.deployment_record_id,
        )
        # Recreate is an owned no-op, not a duplicate application hostname.
        ingress.request = ingress.request.model_copy(
            update={"reviewed_plan_sha256": ingress.plan()["plan_sha256"]}
        )
        ingress.apply(
            "private-noop-after-migration",
            __import__("unittest.mock", fromlist=["Mock"]).Mock(spec=ProviderOperationLease),
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).domains,
            (),
        )
        self.assertEqual(ingress.client.proxy_hosts, edge_before)
        self.lose_command = True
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.old.now(-3600)
        ):
            self.assertEqual(self.normal("second-private-original").status, "reconcile_required")
        self.lose_command = False
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.policies.staff_testing_hold = self.retained.policies.staff_testing_hold
        self.store.write_dokploy_target_record(target)
        self.retained, self.old_id = target, first.target_id
        self.activation_key = "second-private-activation"
        self.prepare("second-private-preparation")
        self.assertEqual(self.activate().status, "completed")
        second = self.adapter.current()
        assert second.application_readiness is not None
        self.assertEqual(
            json.loads(fetch().body)["runtime_identity"]["deployment_record_id"],
            second.application_readiness.runtime_identity.deployment_record_id,
        )
        self.assertEqual(self.payloads[origin_id]["domains"], original_domain)
        self.assertEqual(ingress.client.proxy_hosts, edge_before)
        self.assertFalse(self.payloads[first.target_id]["domains"])
        self.assertFalse(self.payloads[second.target_id]["domains"])
        from control_plane.testing_application_private_route import published_application_lineage

        origin, chain = published_application_lineage(
            self.store, self.product, self.context, second.target_id, routing.plan.host
        )
        self.assertEqual(origin, origin_id)
        self.assertEqual(
            [record.target_id for record in chain], [second.target_id, first.target_id]
        )
        with patch.object(
            self.store,
            "list_testing_application_activations",
            return_value=[second.model_dump(mode="json")],
        ):
            from control_plane.outbound_http import PublicHttpDestinationError

            with self.assertRaises(PublicHttpDestinationError):
                fetch()

    def test_admission_refuses_original_route_contradictions_before_candidate_effects(self) -> None:
        routing = self.addressed()
        self.payloads[self.old_id]["domains"][0]["port"] += 1
        self.prepare()
        before = tuple(self.writes)
        from control_plane.contracts.testing_activation import TestingActivationRefusal

        with self.assertRaises(TestingActivationRefusal):
            self.activate()
        self.assertEqual(tuple(self.writes), before)
        self.assertFalse(self.projects)
        self.assertFalse(routing.route_file.exists())

    def test_late_proven_route_contradiction_retains_closed_candidate(self) -> None:
        self.addressed()
        self.prepare()
        qualify = self.adapter.qualify_application

        def change(record: Any) -> Any:
            result = qualify(record)
            self.payloads[self.old_id]["domains"][0]["path"] = "/changed"
            return result

        with patch.object(self.adapter, "qualify_application", side_effect=change):
            outcome = self.activate()
        self.assertEqual(outcome.status, "completed", outcome)
        self.assertFalse(self.adapter.current().published)
        failure = self.adapter.current().failure
        assert failure is not None
        self.assertEqual(failure.kind, "publication_refused")

    def test_lost_project_then_inspector_fault_retains_adopted_original_custody(self) -> None:
        self.prepare()
        provider = self.provider

        def lost(**kwargs: Any) -> Any:
            value = provider(**kwargs)
            if kwargs["path"] == "/api/project.create":
                raise OSError("Lost actual project creation reply")
            return value

        with (
            patch("control_plane.dokploy.api.dokploy_request", side_effect=lost),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        from control_plane.testing_application_capabilities import (
            inspect_retained_application_capabilities,
        )

        def inspector(**kwargs: Any) -> Any:
            inspect_retained_application_capabilities(**kwargs)
            raise ValueError("Readonly proof unavailable after its actual checkpoint")

        with (
            patch(
                "control_plane.testing_application_capabilities.inspect_retained_application_capabilities",
                side_effect=inspector,
            ),
            patch.object(self.store, "_database_mutation_timestamp", return_value=self.old.now(1)),
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        current = self.adapter.current()
        self.assertIsNone(current.failure)
        self.assertEqual(current.project_id, self.projects[0]["projectId"])
        self.assertEqual(len([path for path, _ in self.writes if path == "/api/project.create"]), 1)
        self.assertIn(self.activate().status, {"completed", "adopted"})

    def test_refused_deploy_checkpoint_does_not_admit_an_image(self) -> None:
        self.prepare()
        from control_plane.provider_operations import _ReservationHeartbeat

        checkpoint = _ReservationHeartbeat.checkpoint_effect

        def unavailable(lease: Any, phase: str) -> None:
            if phase == "deploy_application_deployment_id":
                raise OSError("Local checkpoint unavailable before dispatch")
            checkpoint(lease, phase)

        with (
            patch.object(_ReservationHeartbeat, "checkpoint_effect", unavailable),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        self.assertEqual(self.adapter.current().phase, "configure_application")
        self.assertFalse(self.deployments.get(self.adapter.current().target_id))
        self.assertIn(self.activate().status, {"completed", "adopted"})
        self.assertEqual(len(self.deployments[self.adapter.current().target_id]), 1)

    def assert_direct_http_original_replay(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.service_auth import LaunchplaneAuthzPolicy
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_operators": [
                    {
                        "subjects": [self.caller_identity.subject],
                        "token_labels": [self.caller_identity.token_label],
                        "products": [self.product],
                        "contexts": [self.context],
                        "instances": ["testing"],
                        "actions": [
                            "generic_web_deploy.execute",
                            "product_config.plan",
                            "product_config.apply",
                        ],
                    }
                ],
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(
                token_label=self.caller_identity.token_label
            ),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        reserve = self.store.reserve_mutation
        captured = []

        def capture(**kwargs: Any) -> Any:
            if kwargs["route_path"] == self.original.route_path:
                captured.append(kwargs)
            return reserve(**kwargs)

        runs = list(self.schedule_runs)
        original = self.normal_execution_record(self.original)
        with (
            patch(
                "control_plane.generic_web_deploy_provider_adapter.default_generic_web_deploy_provider",
                return_value=self.deploy_provider,
            ),
            patch.object(self.store, "reserve_mutation", side_effect=capture),
        ):
            response = asyncio.run(
                request(
                    app,
                    "POST",
                    self.original.route_path,
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": self.original.idempotency_key,
                    },
                    payload=self.normal_payload,
                )
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "mutation_reconciliation_required")
        self.assertTrue(captured)
        for actual in captured:
            self.assertEqual(actual["scope"], idempotency_scope(self.caller_identity))
            self.assertEqual(actual["scope"], self.original.scope)
            self.assertEqual(actual["idempotency_key"], self.original.idempotency_key)
            self.assertEqual(actual["request_fingerprint"], self.original.request_fingerprint)
        self.assertEqual(self.schedule_runs, runs)
        self.assertEqual(self.normal_execution_record(self.original), original)

    def normal_execution_record(self, reservation: Any) -> Any:
        from control_plane.provider_operations import build_provider_operation_key
        from control_plane.workflows.generic_web_application_snapshot import (
            generic_web_provider_deployment_record_id,
        )

        operation = build_provider_operation_key(
            scope=reservation.scope,
            route_path=reservation.route_path,
            idempotency_key=reservation.idempotency_key,
            request_fingerprint=reservation.request_fingerprint,
            reconciliation_key=reservation.reconciliation_key,
        )
        return self.store.read_deployment_record(
            generic_web_provider_deployment_record_id(
                operation, context=self.context, instance="testing"
            )
        )

    def test_normal_lost_maintenance_native_activation_and_ordinary_successor(self) -> None:
        from control_plane.workflows.generic_web_execution import execution_receipt

        original_deployment = self.normal_execution_record(self.original)
        original_receipt = execution_receipt(original_deployment)
        assert original_receipt is not None
        if not self.pre_image_unknown:
            unknown_runs = [
                entry for entry in original_receipt.entries if entry.kind == "schedule_run"
            ]
            self.assertTrue(
                unknown_runs, "Actual original schedule dispatch must retain its run intent"
            )
            self.assertTrue(
                all(
                    not entry.terminal and entry.acknowledgement == "none" for entry in unknown_runs
                )
            )
        else:
            quiesce, restore = original_receipt.entries
            self.assertEqual(quiesce.kind, "schedule_write")
            self.assertFalse(quiesce.terminal)
            self.assertEqual(restore.kind, "schedule_write")
            self.assertEqual(restore.terminal, "write_completed")
            self.assertEqual(quiesce.name, restore.name)
        before_runs = list(self.schedule_runs)
        self.assertEqual(self.normal("original").status, "reconcile_required")
        self.assertEqual(self.normal("same-target-before-separation").status, "target_busy")
        self.assertEqual(self.schedule_runs, before_runs)
        self.prepare()
        publish = self.adapter.publish_application

        def observed_publication(record: Any, checkpoint: Any) -> Any:
            from control_plane.testing_application_credentials import staged_runtime_values

            self.assertEqual(self.canonical_password(), "retained-only-password")
            staged = staged_runtime_values(self.store, record)
            self.assertNotEqual(staged["DB_PASSWORD"], self.canonical_password())
            self.assertEqual(
                self.store.read_dokploy_target_id_record(
                    context_name=self.context, instance_name="testing"
                ).target_id,
                self.old_id,
            )
            replacement = publish(record, checkpoint)
            self.assertEqual(self.canonical_password(), staged["DB_PASSWORD"])
            return replacement

        with patch.object(self.adapter, "publish_application", side_effect=observed_publication):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        record = activation_record(self.store, self.product, self.context)
        assert record is not None and record.application_readiness is not None
        self.assertNotEqual(record.target_id, self.old_id)
        self.assertEqual((self.old_backing / "keep").read_text(), "Retained original data")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )
        candidate = self.store.read_deployment_record(
            record.application_readiness.runtime_identity.deployment_record_id
        )
        from control_plane.contracts.deployment_record import deployment_record_passed

        self.assertFalse(deployment_record_passed(candidate))
        self.assertEqual(self.activate().status, "replayed")
        # The exact native lift remains a separate explicit caller action.
        from control_plane.testing_lane_hold import (
            TestingHoldApplyRequest,
            build_testing_hold_plan,
            apply_testing_hold_plan,
        )
        from control_plane.testing_activation import require_activation_hold_lift

        lift = TestingHoldApplyRequest(
            product=self.product,
            context=self.context,
            instance="testing",
            hold=False,
            reason="Use the isolated recipient.",
            activation_plan_sha256=record.plan_sha256,
            purpose="isolation_staging",
        )
        self.assertTrue(require_activation_hold_lift(self.store, lift, self.actor, self.root))
        lift.reviewed_plan_sha256 = build_testing_hold_plan(
            record_store=self.store, request=lift, actor=self.actor
        )[0].plan_sha256
        lift.mode = "apply"
        apply_testing_hold_plan(
            record_store=self.store,
            request=lift,
            actor=self.actor,
            required_context_owner=(self.product, self.context),
            activation_verified=True,
        )
        successor = self.normal("successor")
        self.assertEqual(successor.status, "completed", successor)
        self.assertEqual(successor.response_payload["result"]["post_deploy_status"], "pass")
        from control_plane.workflows.generic_web_application_snapshot import (
            recorded_application_lifecycle,
        )

        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        successor_deployment = self.store.read_deployment_record(inventory.deployment_record_id)
        self.assertEqual(successor_deployment, self.normal_execution_record(successor.record))
        successor_receipt = execution_receipt(successor_deployment)
        assert successor_receipt is not None
        runs = [entry for entry in successor_receipt.entries if entry.kind == "schedule_run"]
        self.assertTrue(
            runs, "Successor qualification must have its own actual schedule-run receipt"
        )
        self.assertTrue(all(entry.terminal == "done" for entry in runs))
        for entry in runs:
            self.assertEqual(self.schedules[entry.target_id]["applicationId"], record.target_id)
        self.assertEqual(self.normal_execution_record(self.original), original_deployment)
        receipt = recorded_application_lifecycle(successor_deployment)
        assert receipt is not None
        self.assertEqual(receipt.target_id, record.target_id)
        self.assertFalse(receipt.resume_from_hold)
        self.assertFalse(receipt.previous_artifact_id)
        self.assertFalse(receipt.previous_source_git_ref)
        self.assertTrue(all(value is None for value in receipt.originals.values()))
        completed_runs = list(self.schedule_runs)
        self.assertEqual(self.normal("successor").status, "replayed")
        self.assertEqual(self.normal("original").status, "reconcile_required")
        self.assertEqual(self.schedule_runs, completed_runs)
        self.assertEqual(self.normal_execution_record(self.original), original_deployment)
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )

    def canonical_password(self) -> str:
        from control_plane.runtime_environments import resolve_site_runtime_environment_from_store

        return resolve_site_runtime_environment_from_store(
            record_store=self.store,
            context_name=self.context,
            instance_name="testing",
            target_environment={},
        ).values["DB_PASSWORD"]

    def interrupt_unanswered_mount(self) -> dict[str, Any]:
        self.prepare()
        pending: dict[str, Any] = {}
        provider = self.provider

        def interrupted(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/mounts.create":
                pending.update(deepcopy(kwargs))
                raise OSError("Fixture mount reply unavailable")
            return provider(**kwargs)

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=interrupted):
            self.assertEqual(self.activate().status, "reconcile_required")
        self.assertTrue(pending)
        self.assertEqual(self.adapter.current().phase, "create_application_mount")
        return pending

    def test_explicit_unanswered_mount_refusal_retains_custody_and_allows_new_generation(
        self,
    ) -> None:
        pending = self.interrupt_unanswered_mount()
        before = self.adapter.current()
        password = self.canonical_password()
        self.assertEqual(self.activate().status, "reconcile_required")
        writes = deepcopy(self.writes)
        self.request.mode = "refuse"
        refused = self.activate()
        self.assertEqual(refused.status, "completed", refused)
        self.assertEqual(refused.response_payload["result"]["outcome"], "refused")
        self.assertFalse(refused.response_payload["result"]["deployment_requested"])
        self.assertEqual(self.writes, writes)
        self.assertEqual(self.canonical_password(), password)
        after = self.adapter.current()
        self.assertEqual(after.state_recipient, before.state_recipient)
        self.assertEqual(after.target_id, before.target_id)
        self.assertEqual(after.preparation_record_id, before.preparation_record_id)
        self.provider(**pending)
        self.request.mode = "apply"
        writes = deepcopy(self.writes)
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(self.writes, writes)
        self.assertFalse(self.deployments.get(before.target_id))
        self.prepare("after-unanswered-refusal")
        self.activation_key = "after-unanswered-refusal"
        self.assertEqual(self.activate().status, "completed")
        self.assertNotEqual(self.adapter.current().target_id, before.target_id)
        self.assertNotEqual(self.adapter.current().namespace, before.namespace)
        self.assertIn(before.target_id, self.payloads)

    def test_late_mount_after_durable_refusal_cannot_resume_forward_apply(self) -> None:
        pending = self.interrupt_unanswered_mount()
        original = self.adapter.save
        decision_saved = False

        def crash_after_decision(record: Any, **updates: Any) -> Any:
            nonlocal decision_saved
            saved = original(record, **updates)
            if updates.get("phase") == "refusing_private_candidate":
                decision_saved = True
                self.provider(**pending)
                raise OSError("Fixture process ended after durable decision")
            return saved

        self.request.mode = "refuse"
        with patch.object(self.adapter, "save", side_effect=crash_after_decision):
            self.assertEqual(self.activate().status, "reconcile_required")
        self.assertTrue(decision_saved)
        self.request.mode = "apply"
        writes = deepcopy(self.writes)
        completed = self.activate()
        self.assertIn(completed.status, {"completed", "adopted"}, completed)
        self.assertEqual(completed.response_payload["result"]["outcome"], "refused")
        self.assertEqual(self.writes, writes)
        self.assertFalse(self.deployments.get(self.adapter.current().target_id))

    def test_foreign_mount_after_refusal_decision_retains_the_reservation(self) -> None:
        pending = self.interrupt_unanswered_mount()
        self.provider(**pending)
        candidate = self.adapter.current()
        mount = deepcopy(self.payloads[candidate.target_id]["mounts"][0])
        self.payloads[candidate.target_id]["mounts"][0]["applicationId"] = "foreign"
        self.request.mode = "refuse"
        self.assertEqual(self.activate().status, "reconcile_required")
        self.assertIsNone(self.adapter.current().failure)
        self.assertEqual(self.adapter.current().phase, "refusing_private_candidate")
        self.payloads[candidate.target_id]["mounts"] = [mount]
        self.request.mode = "apply"
        completed = self.activate()
        self.assertEqual(completed.status, "completed", completed)
        self.assertEqual(completed.response_payload["result"]["outcome"], "refused")

    def test_unanswered_refusal_rejects_stale_forward_progress_in_storage(self) -> None:
        self.interrupt_unanswered_mount()
        self.request.mode = "refuse"
        original = self.adapter.save
        fenced = False

        def attempt_forward(record: Any, **updates: Any) -> Any:
            nonlocal fenced
            saved = original(record, **updates)
            if updates.get("phase") == "refusing_private_candidate":
                with self.assertRaises(ValueError):
                    original(saved, phase="configure_application")
                fenced = True
            return saved

        with patch.object(self.adapter, "save", side_effect=attempt_forward):
            self.assertEqual(self.activate().status, "completed")
        self.assertTrue(fenced)

    def test_unanswered_refusal_cannot_replace_a_live_original_lease(self) -> None:
        self.interrupt_unanswered_mount()
        parent = self.store.read_testing_application_activation_operation(
            self.product, self.context, self.scope, self.request.preparation_key
        )
        assert parent is not None
        acquired = self.store.retry_reconciled_mutation(
            reservation=parent,
            lease_owner="still-running-original",
            lease_seconds=300,
            preserve_effect_checkpoint=True,
        )
        self.assertEqual(acquired.status, "acquired")
        before = deepcopy((self.writes, self.adapter.current()))
        self.request.mode = "refuse"
        self.assertEqual(self.activate().status, "in_progress")
        self.assertEqual((self.writes, self.adapter.current()), before)

    def test_plain_authority_changed_apply_cannot_abandon_an_unanswered_mount(self) -> None:
        self.interrupt_unanswered_mount()
        record = self.adapter.current()
        self.correct_runtime()
        for status in ("idle", "running"):
            with self.subTest(status=status):
                self.payloads[record.target_id]["applicationStatus"] = status
                writes = deepcopy(self.writes)
                self.assertEqual(self.activate().status, "reconcile_required")
                self.assertEqual(self.adapter.current().phase, "create_application_mount")
                self.assertIsNone(self.adapter.current().failure)
                self.assertEqual(self.writes, writes)

    def test_lost_negative_completion_adopts_the_refusal_without_forward_work(self) -> None:
        self.interrupt_unanswered_mount()
        self.request.mode = "refuse"
        with (
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
            patch.object(
                self.store,
                "complete_mutation_reservation",
                side_effect=OSError("Fixture completion save unavailable"),
            ),
            self.assertRaises(OSError),
        ):
            self.activate()
        self.assertIsNotNone(self.adapter.current().failure)
        self.request.mode = "apply"
        writes = deepcopy(self.writes)
        adopted = self.activate()
        self.assertEqual(adopted.status, "adopted", adopted)
        self.assertEqual(adopted.response_payload["result"]["outcome"], "refused")
        self.assertEqual(self.writes, writes)

    def test_unanswered_refusal_keeps_unknown_or_automatic_candidate_execution_fenced(self) -> None:
        self.interrupt_unanswered_mount()
        record = self.adapter.current()
        live = self.payloads[record.target_id]
        original = deepcopy(live)
        self.request.mode = "refuse"
        for changes in (
            {"sourceType": "docker", "dockerImage": record.image_reference, "autoDeploy": False},
            {"sourceType": None, "autoDeploy": True},
            {"branch": "main"},
            {"domains": [{"host": "foreign.invalid"}]},
            {"applicationStatus": "running"},
        ):
            with self.subTest(changes=changes):
                live.clear()
                live.update(deepcopy(original) | changes)
                writes = deepcopy(self.writes)
                self.assertEqual(self.activate().status, "reconcile_required")
                self.assertEqual(self.writes, writes)
                self.assertIsNone(self.adapter.current().failure)
        live.clear()
        live.update(original)
        completed = self.activate()
        self.assertEqual(completed.status, "completed", completed)

    def assert_http_unanswered_refusal(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        self.prepare()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=legacy.activation_hold_policy(self.product, self.context),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

        def post(mode: str, key: str = "activation", **changes: Any) -> Any:
            return asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": key,
                    },
                    payload=self.request.model_dump(mode="json") | {"mode": mode} | changes,
                )
            )

        writes = deepcopy(self.writes)
        self.assertEqual(post("refuse").status_code, 409)
        self.assertEqual(self.writes, writes)
        provider = self.provider

        def interrupted(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/mounts.create":
                raise OSError("Fixture unanswered mount")
            return provider(**kwargs)

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=interrupted):
            self.assertEqual(post("apply").status_code, 409)
        before = self.adapter.current()
        writes = deepcopy(self.writes)
        for key, changes in (
            ("foreign-key", {}),
            ("activation", {"reason": "Changed original intent"}),
            ("activation", {"reviewed_plan_sha256": "f" * 64}),
            ("activation", {"application": None}),
        ):
            with self.subTest(key=key, changes=changes):
                self.assertEqual(post("refuse", key, **changes).status_code, 409)
                self.assertEqual(self.adapter.current(), before)
                self.assertEqual(self.writes, writes)
        from threading import get_ident

        handler_thread = get_ident()
        lookup = self.store.read_testing_application_activation_operation

        def worker_lookup(*args: Any, **kwargs: Any) -> Any:
            self.assertNotEqual(get_ident(), handler_thread)
            return lookup(*args, **kwargs)

        with patch.object(
            self.store, "read_testing_application_activation_operation", side_effect=worker_lookup
        ):
            refused = post("refuse")
        self.assertEqual(refused.status_code, 202, refused.text)
        self.assertEqual(refused.json()["result"]["outcome"], "refused")
        self.assertEqual(self.writes, writes)
        for mode in ("refuse", "apply", "receipt", "readback"):
            replayed = post(mode)
            self.assertEqual(replayed.status_code, 202, replayed.text)
            self.assertEqual(replayed.json()["result"], refused.json()["result"])
            self.assertEqual(self.writes, writes)

    def test_lost_mount_reply_adopts_provider_row_once_and_fences_conflicts(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        self.prepare()
        interrupted = False

        def lose_mount_reply(**kwargs: Any) -> Any:
            nonlocal interrupted
            response = self.provider(**kwargs)
            if kwargs["path"] == "/api/mounts.create" and not interrupted:
                interrupted = True
                raise OSError("Lost mount create reply")
            return response

        retained_password = self.canonical_password()
        with (
            patch("control_plane.dokploy.api.dokploy_request", side_effect=lose_mount_reply),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        record = self.adapter.current()
        self.assertEqual(record.phase, "create_application_mount")
        original = deepcopy(self.payloads[record.target_id]["mounts"])
        self.assertEqual(len(original), 1)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=legacy.activation_hold_policy(self.product, self.context),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        original_row = self.store.read_idempotency_record(
            scope=self.scope,
            route_path=ROUTE.format(product=self.product),
            idempotency_key="activation",
        )
        before = deepcopy((self.writes, self.schedules, self.schedule_history))

        def inspect_original() -> Any:
            return asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "activation",
                    },
                    payload=self.request.model_dump(mode="json") | {"mode": "inspect"},
                )
            )

        with (
            patch(
                "control_plane.testing_activation_inspection.inspect_preparation_programs",
                return_value=(),
            ),
            patch.object(
                ApplicationActivationAdapter,
                "candidate_refusal_ready",
                side_effect=ValueError("private-observer-cause"),
            ),
            self.assertLogs("control_plane.http_error_evidence", level="WARNING") as causes,
        ):
            inspected = inspect_original()
        self.assertEqual(inspected.status_code, 202, inspected.text)
        cause = json.loads(causes.records[-1].getMessage())
        self.assertEqual(cause["trace_id"], inspected.json()["trace_id"])
        self.assertTrue(cause["sites"])
        self.assertNotIn("private-observer-cause", " ".join(causes.output) + inspected.text)
        with (
            patch(
                "control_plane.testing_activation_inspection.inspect_preparation_programs",
                return_value=(),
            ),
            self.assertLogs(
                "control_plane.testing_activation_inspection", level="WARNING"
            ) as outcomes,
        ):
            inspected = inspect_original()
        self.assertEqual(inspected.status_code, 202, inspected.text)
        outcome = json.loads(outcomes.records[-1].getMessage())
        self.assertEqual(outcome["trace_id"], inspected.json()["trace_id"])
        self.assertEqual(outcome["outcome"], "absent")
        self.assertEqual((self.writes, self.schedules, self.schedule_history), before)
        self.assertEqual(self.adapter.current(), record)
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.scope,
                route_path=ROUTE.format(product=self.product),
                idempotency_key="activation",
            ),
            original_row,
        )
        mount = original[0]
        conflicts = [("absent", []), ("duplicate", [mount, deepcopy(mount)])]
        conflicts.extend(
            (key, [{**mount, key: value}])
            for key, value in (
                ("applicationId", "foreign-application"),
                ("serviceType", "compose"),
                ("volumeName", "foreign-volume"),
                ("mountPath", "/other-state"),
                ("type", "bind"),
                ("mountId", None),
                ("hostPath", "/host-state"),
                ("filePath", "state-file"),
                ("content", "foreign-content"),
                ("composeId", "foreign-compose"),
            )
        )
        for reason, mounts in conflicts:
            with self.subTest(reason=reason):
                self.payloads[record.target_id]["mounts"] = mounts
                self.assertEqual(self.activate().status, "reconcile_required")
                self.assertFalse(self.adapter.current().published)
                self.assertFalse(self.deployments.get(record.target_id))
                self.assertEqual(self.canonical_password(), retained_password)
        self.payloads[record.target_id]["mounts"] = original
        completed = self.activate()
        self.assertIn(completed.status, {"completed", "adopted"}, completed)
        self.assertEqual(self.adapter.current().state_recipient["mount_verified"], "true")
        self.assertEqual(len(self.payloads[record.target_id]["mounts"]), 1)
        self.assertEqual(len([path for path, _ in self.writes if path == "/api/mounts.create"]), 1)
        self.assertEqual(len(self.deployments[record.target_id]), 1)

    def test_lost_application_create_reply_adopts_exact_original_without_recreation(self) -> None:
        self.prepare()
        self.lose_create = True
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required")
        self.assertEqual(self.canonical_password(), "retained-only-password")
        completed = self.activate()
        self.assertIn(completed.status, {"completed", "adopted"}, completed)
        self.assertEqual(
            len([path for path, _ in self.writes if path == "/api/application.create"]), 1
        )

    def test_lost_fresh_image_reply_is_read_back_without_redeploying(self) -> None:
        self.prepare()
        self.lost_image_reply = True
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required")
        self.assertEqual(self.canonical_password(), "retained-only-password")
        completed = self.activate()
        self.assertIn(completed.status, {"completed", "adopted"}, completed)
        self.assertEqual(
            len([path for path, _ in self.writes if path == "/api/application.deploy"]), 1
        )

    def test_lost_deployment_is_adopted_before_external_conflict_refusal(self) -> None:
        from control_plane.testing_external_capabilities import ExternalCapabilityConflict

        self.prepare()
        self.lost_image_reply = True
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required")

        def conflict(record: TestingActivationRecord, check: Any) -> None:
            check()
            raise ExternalCapabilityConflict()

        with patch.object(self.adapter, "require_production_separation", side_effect=conflict):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        record = self.adapter.current()
        self.assertFalse(record.published)
        self.assertIsNotNone(record.failure)
        self.assertEqual(
            record.state_recipient["application_deployment_id"],
            api.deployment_key(self.deployments[record.target_id][0]),
        )
        self.assertIn(
            api.deployment_status(self.deployments[record.target_id][0]),
            api._DEPLOYMENT_SUCCESS_STATUSES,
        )
        self.assertEqual(
            len([path for path, _ in self.writes if path == "/api/application.deploy"]), 1
        )
        before = tuple(self.writes)
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(tuple(self.writes), before)

    def test_external_conflict_before_configure_keeps_credentials_and_candidate_unused(
        self,
    ) -> None:
        from control_plane.testing_external_capabilities import ExternalCapabilityConflict

        self.prepare()
        original_password = self.canonical_password()

        def conflict(record: TestingActivationRecord, check: Any) -> None:
            check()
            raise ExternalCapabilityConflict()

        with (
            patch.object(self.adapter, "require_production_separation", side_effect=conflict),
            patch.object(api, "update_dokploy_target_env") as env,
            patch.object(api, "trigger_deployment") as deploy,
        ):
            result = self.activate()
            env.assert_not_called()
            deploy.assert_not_called()
        self.assertEqual(result.status, "completed", result)
        record = self.adapter.current()
        self.assertFalse(record.published)
        self.assertIsNotNone(record.failure)
        self.assertEqual(self.canonical_password(), original_password)
        before = tuple(self.writes)
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(tuple(self.writes), before)

    def test_failed_atomic_publication_keeps_old_delivery_and_reuses_staged_generation(
        self,
    ) -> None:
        self.prepare()

        def fail(step: str) -> None:
            if step == "write_provider_target":
                raise RuntimeError("Injected publication transaction failure")

        with (
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
            patch.object(self.store, "_after_product_authority_bundle_step", side_effect=fail),
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required")
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ).target_id,
            self.old_id,
        )
        from control_plane.testing_application_credentials import staged_runtime_values

        record = self.adapter.current()
        staged, versions = staged_runtime_values(self.store, record), dict(record.state_recipient)
        self.assertFalse(record.published)
        completed = self.activate()
        self.assertIn(completed.status, {"completed", "adopted"}, completed)
        published = self.adapter.current()
        self.assertEqual(published.state_recipient, versions)
        self.assertEqual(self.canonical_password(), staged["DB_PASSWORD"])

    def assert_stale_final_execution_refused(self, *, expire: bool) -> None:
        from control_plane.storage.postgres import PostgresRecordStore

        self.prepare()
        second = PostgresRecordStore(
            database_url=self.store._engine.url.render_as_string(hide_password=False)
        )
        self.addCleanup(second.close)
        write = self.store.write_testing_activation
        changed = []

        def stale(**kwargs: Any) -> None:
            if kwargs["replacement"]["published"]:
                current = second.read_idempotency_record(
                    scope=self.scope,
                    route_path=ROUTE.format(product=self.product),
                    idempotency_key="activation",
                )
                assert current is not None
                replacement = current.model_copy(
                    update={"lease_expires_at": self.old.now(-1), "updated_at": self.old.now(-2)}
                    if expire
                    else {"attempt": current.attempt + 1, "lease_owner": "replacement-worker"}
                )
                # Model a concurrent expiry or takeover after the dispatch-side
                # check, before the transaction that publishes target+secrets.
                with second._session_factory() as session:
                    session.merge(second._idempotency_row(replacement))
                    session.commit()
                changed.append(replacement)
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=stale):
            result = self.activate()
        self.assertTrue(changed)
        self.assertNotIn(result.status, {"completed", "adopted", "replayed"})
        self.assertFalse(self.adapter.current().published)
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ).target_id,
            self.old_id,
        )

    def test_final_publication_expired_execution_rolls_back_target_and_delivery(self) -> None:
        self.assert_stale_final_execution_refused(expire=True)

    def test_final_publication_replaced_execution_rolls_back_target_and_delivery(self) -> None:
        self.assert_stale_final_execution_refused(expire=False)

    def test_later_supported_hold_correction_refuses_final_publication(self) -> None:
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.testing_lane_hold import (
            TestingHoldApplyRequest,
            apply_testing_hold_plan,
            build_testing_hold_plan,
        )

        self.prepare()
        second = PostgresRecordStore(
            database_url=self.store._engine.url.render_as_string(hide_password=False)
        )
        self.addCleanup(second.close)
        write = self.store.write_testing_activation
        corrected: list[DokployTargetRecord] = []

        def correction(**kwargs: Any) -> None:
            if kwargs["replacement"]["published"] and not corrected:
                request = TestingHoldApplyRequest(
                    product=self.product,
                    context=self.context,
                    instance="testing",
                    hold=True,
                    purpose="isolation_staging",
                    reason="A later reviewed staging correction.",
                )
                request.reviewed_plan_sha256 = build_testing_hold_plan(
                    record_store=second, request=request, actor=self.actor
                )[0].plan_sha256
                request.mode = "apply"
                applied = apply_testing_hold_plan(
                    record_store=second,
                    request=request,
                    actor=self.actor,
                    required_context_owner=(self.product, self.context),
                )
                self.assertTrue(applied.applied and applied.read_back_matches)
                corrected.append(
                    second.read_dokploy_target_record(
                        context_name=self.context, instance_name="testing"
                    )
                )
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=correction):
            result = self.activate()
        self.assertTrue(corrected)
        self.assertEqual(result.status, "completed")
        self.assertFalse(result.response_payload["result"]["applied"])
        self.assertFalse(result.response_payload["result"]["separation_verified"])
        self.assertFalse(self.adapter.current().published)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            corrected[0],
        )
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ).target_id,
            self.old_id,
        )

    def test_new_staff_use_refuses_before_fresh_creation(self) -> None:
        from datetime import datetime, timedelta, timezone
        from control_plane.contracts.disposable_lane import DisposableLaneSafety
        from control_plane.contracts.testing_activation import TestingActivationRefusal
        from control_plane.disposable_lanes import resolve_disposable_observation_binding
        from control_plane.storage.postgres import PostgresRecordStore

        self.prepare()
        second = PostgresRecordStore(
            database_url=self.store._engine.url.render_as_string(hide_password=False)
        )
        self.addCleanup(second.close)
        write = self.store.write_testing_activation
        observed: list[DisposableLaneSafety] = []

        def correction(**kwargs: Any) -> None:
            if kwargs["replacement"]["phase"] == "create_project" and not observed:
                binding = resolve_disposable_observation_binding(
                    second, product=self.product, context=self.context, instance="testing"
                )
                now = datetime.now(timezone.utc)
                record = DisposableLaneSafety(
                    product=self.product,
                    context=self.context,
                    instance="testing",
                    observed_by=self.actor,
                    observer_kind="director_agent",
                    binding_sha256=binding.binding_sha256,
                    usage="in_use",
                    usage_evidence="A fresh staff-use observation.",
                    observed_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
                self.assertTrue(second.publish_collected_disposable_lane_safety(record))
                observed.append(record)
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=correction):
            result = self.activate()
        self.assertTrue(observed)
        self.assertEqual(result.status, "completed")
        self.assertFalse(result.response_payload["result"]["separation_verified"])
        self.assertFalse(result.response_payload["result"]["applied"])
        self.assertFalse(self.adapter.current().published)
        self.assertFalse(self.projects)
        self.assertEqual(
            second.read_disposable_lane_safety(self.product, self.context, "testing"), observed[0]
        )
        with self.assertRaises(TestingActivationRefusal):
            self.adapter.require_unchanged(self.adapter.current())
        self.assertEqual(self.canonical_password(), "retained-only-password")

    def test_pre_checkpoint_review_drift_releases_for_a_fresh_review_and_key(self) -> None:
        from control_plane.contracts.testing_activation import TestingActivationRefusal

        self.prepare()
        inventory = self.store.read_environment_inventory(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(update={"updated_at": self.old.now(1)})
        )
        with self.assertRaises(TestingActivationRefusal):
            self.activate()
        self.assertFalse(self.projects)
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=self.scope,
                route_path=ROUTE.format(product=self.product),
                idempotency_key="activation",
            )
        )
        self.planned = self.adapter.current()
        self.request.reviewed_plan_sha256 = self.planned.plan_sha256
        self.activation_key = "fresh-reviewed-key"
        self.assertEqual(self.activate().status, "completed")

    def test_private_ready_candidate_configuration_refusal_allows_fresh_generation(self) -> None:
        self.prepare()
        write = self.store.write_testing_activation
        corrected: list[Any] = []

        def change(**kwargs: Any) -> None:
            if kwargs["replacement"]["published"] and not corrected:
                (runtime,) = self.store.list_runtime_environment_records(
                    context_name=self.context, instance_name="testing"
                )
                runtime.env["APPLICATION_MODE"] = "permanent-correction"
                self.store.write_runtime_environment_record(runtime)
                corrected.append(runtime)
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=change):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        self.assertEqual(result.response_payload["result"]["reason"], "publication_refused")
        original_candidate = self.adapter.current().target_id
        self.prepare("private-ready-successor")
        self.activation_key = "private-ready-successor"
        self.assertEqual(self.activate().status, "completed")
        self.assertNotEqual(self.adapter.current().target_id, original_candidate)
        self.assertEqual(
            self.store.list_runtime_environment_records(
                context_name=self.context, instance_name="testing"
            ),
            tuple(corrected),
        )

    def assert_answered_before_deploy_refuses(self, boundary: str) -> None:
        if boundary == "placement":
            self.specification = self.specification.model_copy(
                update={"state": TestingApplicationState(kind="none")}
            )
        self.prepare()
        changed = False
        method = (
            "save_state"
            if boundary in {"staged", "mount"}
            else "create_placement"
            if boundary == "placement"
            else "configure_application"
        )
        original = getattr(self.adapter, method)

        def correction(record: Any, *args: Any, **values: str) -> Any:
            nonlocal changed
            result = original(record, *args, **values)
            marker = "registry_sha256" if boundary == "staged" else "mount_verified"
            if not changed and (boundary in {"placement", "configured"} or marker in values):
                changed = True
                self.correct_runtime()
            return result

        with patch.object(self.adapter, method, side_effect=correction):
            result = self.activate()
        self.assertTrue(changed)
        self.assertEqual(result.status, "completed", result)
        record = self.adapter.current()
        assert record.failure is not None
        self.assertEqual(record.failure.kind, "publication_refused")
        self.assertFalse(result.response_payload["result"]["deployment_requested"])
        self.assertFalse(any(path.endswith(".deploy") for path, _ in self.writes))
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )
        self.prepare("after-answered-" + boundary)
        self.activation_key = "after-answered-" + boundary
        self.assertEqual(self.activate().status, "completed")

    def test_answered_placement_correction_retains_unused_candidate(self) -> None:
        self.assert_answered_before_deploy_refuses("placement")

    def test_staged_private_inputs_correction_retains_unused_candidate(self) -> None:
        self.assert_answered_before_deploy_refuses("staged")

    def test_answered_mount_correction_retains_unused_candidate(self) -> None:
        self.assert_answered_before_deploy_refuses("mount")

    def test_answered_configuration_correction_retains_unused_candidate(self) -> None:
        self.assert_answered_before_deploy_refuses("configured")

    def test_answered_unused_configuration_readback_retains_after_lost_reply(self) -> None:
        self.prepare()
        provider = self.provider

        def uncertain(**kwargs: Any) -> Any:
            result = provider(**kwargs)
            if kwargs["path"] == "/api/application.saveDockerProvider":
                self.correct_runtime()
                raise OSError("The original source update reply was lost")
            return result

        with patch("control_plane.dokploy.api.dokploy_request", side_effect=uncertain):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        record = self.adapter.current()
        self.assertEqual(record.phase, "refused")
        self.assertNotIn("application_configured", record.state_recipient)
        self.assertIsNotNone(record.failure)
        self.assertFalse(any(path.endswith(".deploy") for path, _ in self.writes))

    def test_terminal_private_candidate_correction_does_not_require_passing_health(self) -> None:
        self.prepare()
        qualify = self.adapter.qualify_application

        def corrected(record: Any) -> Any:
            self.correct_runtime()
            return qualify(record)

        with patch.object(self.adapter, "qualify_application", side_effect=corrected):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        completed = self.adapter.current()
        assert completed.failure is not None
        self.assertEqual(completed.failure.kind, "publication_refused")
        assert completed.application_readiness is not None
        self.assertIsNone(completed.application_readiness.private_health)
        self.prepare("corrected-private-successor")
        self.activation_key = "corrected-private-successor"
        self.assertEqual(self.activate().status, "completed")

    def correct_runtime(self) -> None:
        (runtime,) = self.store.list_runtime_environment_records(
            context_name=self.context, instance_name="testing"
        )
        runtime.env["APPLICATION_MODE"] = (
            str(runtime.env.get("APPLICATION_MODE", "")) + "-corrected"
        )
        self.store.write_runtime_environment_record(runtime)

    def corrected_publication(self, record: Any, checkpoint: Any) -> Any:
        self.correct_runtime()
        self.adapter.require_unchanged(record)
        raise AssertionError("The actual correction must refuse publication")

    def record_staff_usage(self, usage: Literal["in_use", "unused"]) -> None:
        from datetime import datetime, timedelta, timezone
        from control_plane.contracts.disposable_lane import DisposableLaneSafety
        from control_plane.disposable_lanes import resolve_disposable_observation_binding

        binding = resolve_disposable_observation_binding(
            self.store, product=self.product, context=self.context, instance="testing"
        )
        if usage == "unused":
            from control_plane.disposable_lanes import disposable_lane_observation_sha256

            previous = self.store.read_disposable_lane_safety(self.product, self.context, "testing")
            self.store.clear_reviewed_disposable_lane_safety(
                product=self.product,
                context=self.context,
                instance="testing",
                preview_id="",
                expected_binding_sha256=binding.binding_sha256,
                expected_observation_sha256=disposable_lane_observation_sha256(previous),
            )
        now = datetime.now(timezone.utc)
        self.assertTrue(
            self.store.publish_collected_disposable_lane_safety(
                DisposableLaneSafety(
                    product=self.product,
                    context=self.context,
                    instance="testing",
                    observed_by=self.actor,
                    observer_kind="director_agent",
                    binding_sha256=binding.binding_sha256,
                    usage=usage,
                    usage_evidence="An explicit current staff-use observation.",
                    observed_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
            )
        )

    def test_addressed_staff_use_pauses_then_original_key_publishes_without_inverse(self) -> None:
        routing = self.addressed()
        self.prepare()
        publish = self.adapter.publish_application

        def staff_use(record: Any, checkpoint: Any) -> Any:
            self.record_staff_usage("in_use")
            return publish(record, checkpoint)

        with (
            patch.object(self.adapter, "publish_application", side_effect=staff_use),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required", first)
        self.assertIsNone(self.adapter.current().failure)
        inode, content = routing.route_file.stat().st_ino, routing.route_file.read_bytes()
        self.record_staff_usage("unused")
        self.assertIn(self.activate().status, {"completed", "adopted"})
        self.assertEqual(
            (routing.route_file.stat().st_ino, routing.route_file.read_bytes()), (inode, content)
        )

    def assert_public_health_outcome(
        self, *, unavailable: bool = False, mismatch: bool = False
    ) -> None:
        from control_plane.contracts.promotion_record import HealthcheckEvidence
        from control_plane.dokploy.testing_route import observe_testing_route

        routing = self.addressed()
        self.prepare()

        def health(*_args: Any) -> Any:
            if not routing.route_file.exists():
                from control_plane.contracts.runtime_identity import parse_runtime_identity_payload

                env = dict(
                    item.split("=", 1)
                    for item in self.containers["old-app-container"]["Config"]["Env"]
                )
                return HealthcheckEvidence(
                    status="pass",
                    verified=True,
                    timeout_seconds=5,
                    urls=("https://" + routing.plan.host + "/health",),
                    observed_runtime_identity=parse_runtime_identity_payload(
                        json.loads(env["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])
                    ),
                )
            readiness = self.adapter.current().application_readiness
            assert readiness is not None
            return HealthcheckEvidence(
                status="pending" if unavailable else "pass" if mismatch else "fail",
                verified=not unavailable,
                timeout_seconds=5,
                urls=("https://" + routing.plan.host + "/health",),
                observed_runtime_identity=readiness.runtime_identity.model_copy(
                    update={"artifact_id": "wrong-image"}
                )
                if mismatch
                else None,
            )

        with (
            patch(
                "control_plane.testing_rebuild_service.read_public_testing_health",
                side_effect=health,
            ),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            result = self.activate()
        record = self.adapter.current()
        readiness = record.application_readiness
        assert readiness is not None and readiness.route is not None
        if unavailable:
            self.assertEqual(result.status, "reconcile_required", result)
            self.assertIsNone(record.failure)
            self.assertEqual(
                observe_testing_route(
                    readiness.route, routing.raw, json.loads(routing.route_file.read_text())
                ),
                readiness.backend,
            )
            before = routing.route_file.read_bytes(), routing.route_file.stat().st_ino
            self.assertIn(self.activate().status, {"completed", "adopted"})
            self.assertEqual(
                (routing.route_file.read_bytes(), routing.route_file.stat().st_ino), before
            )
        else:
            self.assertEqual(result.status, "completed", result)
            assert record.failure is not None and readiness.public_health is not None
            self.assertEqual(record.failure.kind, "publication_refused")
            self.assertTrue(readiness.public_health.verified)
            self.assertEqual(readiness.public_health.status, "fail")
            self.assertEqual(
                observe_testing_route(
                    readiness.route, routing.raw, json.loads(routing.route_file.read_text())
                ),
                readiness.route.old_backend,
            )
            self.prepare("healthy-public-successor")
            self.activation_key = "healthy-public-successor"
            self.assertEqual(self.activate().status, "completed")

    def test_verified_public_http_failure_restores_original_and_allows_successor(self) -> None:
        self.assert_public_health_outcome()

    def test_verified_public_identity_mismatch_restores_original(self) -> None:
        self.assert_public_health_outcome(mismatch=True)

    def test_public_transport_unknown_keeps_original_forward_for_retry(self) -> None:
        self.assert_public_health_outcome(unavailable=True)

    def assert_private_health_outcome(self, kind: str) -> None:
        self.prepare()
        docker = self.docker

        def observed(arguments: list[str], **kwargs: Any) -> Any:
            if arguments[1:5] == ["exec", "-i", "proxy", "nc"]:
                if kind == "http":
                    return self.http_response(arguments, status=503)
                if kind == "identity":
                    readiness = self.adapter.current().application_readiness
                    assert readiness is not None
                    identity = readiness.runtime_identity
                    return self.http_response(
                        arguments,
                        body=json.dumps(
                            {
                                "runtime_identity": identity.model_copy(
                                    update={"artifact_id": "different-image"}
                                ).model_dump(mode="json")
                            }
                        ).encode(),
                    )
                if kind == "transport":
                    raise subprocess.CalledProcessError(1, arguments, stderr="connection timed out")
                if kind == "parse":
                    return self.http_response(arguments, body=b"{")
                if kind == "shape":
                    return self.http_response(arguments, body=b"[]")
                if kind == "truncated":
                    return self.http_response(arguments, body=b"{}", declared_length=10)
            if (
                kind == "empty"
                and arguments[1:3] == ["service", "ps"]
                and arguments[-1] != "old-app"
            ):
                return subprocess.CompletedProcess(arguments, 0, "", "")
            result = docker(arguments, **kwargs)
            if kind in {"task", "exited_task"} and arguments[1:4] == ["inspect", "--type", "task"]:
                rows = json.loads(result.stdout)
                rows[0].update(
                    DesiredState="shutdown", Status={**rows[0]["Status"], "State": "failed"}
                )
                result.stdout = json.dumps(rows)
            if (
                kind == "exited_task"
                and arguments[1:2] == ["inspect"]
                and arguments[2] in self.containers
                and arguments[2] != "proxy"
            ):
                rows = json.loads(result.stdout)
                rows[0]["State"]["Status"] = "exited"
                rows[0]["NetworkSettings"]["Networks"] = {}
                result.stdout = json.dumps(rows)
            return result

        with (
            patch.object(self, "docker", side_effect=observed),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            result = self.activate()
            if kind not in {"transport", "parse", "shape", "truncated", "empty", "task"}:
                from control_plane.contracts.testing_activation import TestingActivationRefusal

                # Identical persisted evidence remains a failure; no new save
                # should be needed and it must never return a usable candidate.
                with self.assertRaises(TestingActivationRefusal):
                    self.adapter.qualify_application(self.adapter.current())
        if kind in {"transport", "parse", "shape", "truncated", "empty", "task"}:
            self.assertEqual(result.status, "reconcile_required", result)
            self.assertIsNone(self.adapter.current().failure)
            self.assertIn(self.activate().status, {"completed", "adopted"})
        else:
            self.assertEqual(result.status, "completed", result)
            record = self.adapter.current()
            assert record.failure is not None and record.application_readiness is not None
            assert record.application_readiness.private_health is not None
            self.assertEqual(record.failure.kind, "publication_refused")
            self.assertEqual(record.application_readiness.private_health.status, "fail")
            self.prepare("healthy-private-successor")
            self.activation_key = "healthy-private-successor"
            self.assertEqual(self.activate().status, "completed")

    def test_verified_private_http_failure_retains_candidate_and_allows_successor(self) -> None:
        self.assert_private_health_outcome("http")

    def test_verified_private_identity_failure_retains_candidate(self) -> None:
        self.assert_private_health_outcome("identity")

    def test_attributed_failed_task_retains_candidate_without_claiming_empty_inventory(
        self,
    ) -> None:
        self.assert_private_health_outcome("task")

    def test_attributed_exited_task_failure_needs_no_live_network_endpoint(self) -> None:
        self.assert_private_health_outcome("exited_task")

    def test_private_health_parse_error_keeps_original_unknown(self) -> None:
        self.assert_private_health_outcome("parse")

    def test_private_health_non_mapping_keeps_original_unknown(self) -> None:
        self.assert_private_health_outcome("shape")

    def test_private_health_truncated_body_keeps_original_unknown(self) -> None:
        self.assert_private_health_outcome("truncated")

    def test_empty_swarm_task_inventory_keeps_original_unknown(self) -> None:
        self.assert_private_health_outcome("empty")

    def test_private_http_transport_error_is_not_a_verified_failure(self) -> None:
        self.assert_private_health_outcome("transport")

    def test_addressed_generic_publication_error_retries_forward_without_route_rewrite(
        self,
    ) -> None:
        routing = self.addressed()
        self.prepare()
        with (
            patch.object(self.adapter, "publish_application", side_effect=OSError("Unavailable")),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required", first)
        self.assertIsNone(self.adapter.current().failure)
        inode, document = routing.route_file.stat().st_ino, routing.route_file.read_bytes()
        self.assertIn(self.activate().status, {"completed", "adopted"})
        self.assertEqual(routing.route_file.stat().st_ino, inode)
        self.assertEqual(routing.route_file.read_bytes(), document)

    def test_post_inspector_permanent_configuration_refusal_keeps_custody_and_continues(
        self,
    ) -> None:
        self.prepare()
        write = self.store.write_testing_activation
        corrected: list[Any] = []

        def permanent_change(**kwargs: Any) -> None:
            if kwargs["replacement"]["phase"] == "create_project" and not corrected:
                (runtime,) = self.store.list_runtime_environment_records(
                    context_name=self.context, instance_name="testing"
                )
                runtime.env["APPLICATION_MODE"] = "corrected-mode"
                self.store.write_runtime_environment_record(runtime)
                corrected.append(runtime)
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=permanent_change):
            refused = self.activate()
        self.assertEqual(refused.status, "completed")
        self.assertFalse(refused.response_payload["result"]["separation_verified"])
        self.assertFalse(refused.response_payload["result"]["applied"])
        first = self.adapter.current()
        self.assertIsNotNone(first.failure)
        self.assertFalse(self.projects)
        parent = self.store.read_testing_application_activation_operation(
            self.product, self.context, self.scope, "prepare"
        )
        assert parent is not None
        self.assertEqual(parent.provider_effect_phase, "create_project")
        self.assertEqual(parent.state, "completed")
        self.assertEqual(self.activate().status, "replayed")
        self.activation_key = "corrected-generation"
        self.prepare("corrected-preparation")
        completed = self.activate()
        self.assertEqual(completed.status, "completed", completed)
        self.assertTrue(completed.response_payload["result"]["separation_verified"])
        self.assertNotEqual(self.adapter.current().namespace, first.namespace)
        self.assertEqual(
            self.store.list_runtime_environment_records(
                context_name=self.context, instance_name="testing"
            ),
            tuple(corrected),
        )
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )

    def assert_failed_private_candidate_continues(self, *, lost_reply: bool = False) -> None:
        from pathlib import Path

        self.prepare()
        self.failed_image_type = "application"
        self.lost_image_reply = lost_reply
        result = self.activate()
        self.assertEqual(result.status, "completed", result)
        self.assertFalse(result.response_payload["result"]["separation_verified"])
        self.assertFalse(result.response_payload["result"]["applied"])
        first = self.adapter.current()
        assert first.failure is not None
        self.assertEqual(first.failure.target_type, self.failed_image_type)
        self.assertFalse(first.published)
        effects = tuple(self.writes)
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(tuple(self.writes), effects)
        from control_plane.testing_activation import preparation_resources

        old_resources, _ = preparation_resources(self.store, self.product, self.request, self.scope)
        retained_files = []
        for volume in old_resources.volumes.values():
            retained = (
                Path(str(self.storage.volumes[volume]["Mountpoint"])) / "retained-failed-candidate"
            )
            retained.write_text("Keep this candidate data")
            retained_files.append(retained)
        self.failed_image_type = ""
        self.activation_key = "after-failed-candidate"
        self.prepare("after-failed-preparation")
        next_resources, _ = preparation_resources(
            self.store, self.product, self.request, self.scope
        )
        self.assertTrue(
            set(old_resources.volumes.values()).isdisjoint(next_resources.volumes.values())
        )
        self.assertNotEqual(first.namespace, self.planned.namespace)
        completed = self.activate()
        self.assertEqual(completed.status, "completed", completed)
        self.assertTrue(completed.response_payload["result"]["separation_verified"])
        self.assertTrue(
            all(path.read_text() == "Keep this candidate data" for path in retained_files)
        )
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )

    def test_answered_failed_private_application_keeps_resources_and_new_generation(self) -> None:
        self.assert_failed_private_candidate_continues()

    def test_lost_failed_private_application_keeps_resources_and_new_generation(self) -> None:
        self.assert_failed_private_candidate_continues(lost_reply=True)

    def assert_unproved_candidate_stays_unknown(self, kind: str) -> None:
        self.prepare()
        self.lost_image_reply = True
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
        ):
            self.assertEqual(self.activate().status, "reconcile_required")
        record = self.adapter.current()
        rows = self.deployments[record.target_id]
        if kind == "missing":
            rows.clear()
        elif kind == "duplicate":
            rows[0]["status"] = "error"
            rows.append(deepcopy(rows[0]) | {"deploymentId": "another-job"})
        elif kind == "foreign":
            rows[0].update(status="error", title="unrelated deployment")
        else:
            rows[0]["status"] = "running"
        before = tuple(self.writes)
        result = self.activate()
        self.assertEqual(result.status, "reconcile_required", result)
        self.assertEqual(tuple(self.writes), before)
        self.assertIsNone(self.adapter.current().failure)
        self.assertEqual(self.canonical_password(), "retained-only-password")

    def test_missing_candidate_job_never_completes_as_refused(self) -> None:
        self.assert_unproved_candidate_stays_unknown("missing")

    def test_duplicate_candidate_jobs_never_complete_as_refused(self) -> None:
        self.assert_unproved_candidate_stays_unknown("duplicate")

    def test_foreign_candidate_job_never_completes_as_refused(self) -> None:
        self.assert_unproved_candidate_stays_unknown("foreign")

    def test_running_candidate_job_never_completes_as_refused(self) -> None:
        self.assert_unproved_candidate_stays_unknown("running")

    def test_private_health_transport_error_stays_unknown_without_image_replay(self) -> None:
        self.prepare()
        with (
            patch.object(
                self.adapter, "qualify_application", side_effect=OSError("Health reply unavailable")
            ),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            result = self.activate()
        self.assertEqual(result.status, "reconcile_required")
        self.assertIsNone(self.adapter.current().failure)
        before = sum(path == "/api/application.deploy" for path, _ in self.writes)
        self.assertIn(self.activate().status, {"completed", "adopted"})
        self.assertEqual(sum(path == "/api/application.deploy" for path, _ in self.writes), before)

    def assert_supported_http_activation(
        self,
        *,
        native_apply: bool,
        interrupted: bool = False,
        in_progress: bool = False,
        failed: bool = False,
    ) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        self.prepare()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=legacy.activation_hold_policy(self.product, self.context),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

        async def post(mode: str, key: str = "activation") -> Any:
            body = self.request.model_dump(mode="json") | {"mode": mode}
            return await request(
                app,
                "POST",
                ROUTE.format(product=self.product),
                headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
                payload=body,
            )

        planned = asyncio.run(post("dry-run"))
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertEqual(planned.json()["result"]["plan_sha256"], self.planned.plan_sha256)
        if native_apply:
            if failed:
                self.failed_image_type = "application"
            if interrupted:
                self.lose_create = True
                with patch.object(
                    self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
                ):
                    first = asyncio.run(post("apply"))
                self.assertEqual(first.status_code, 409, first.text)
                self.assertEqual(first.json()["error"]["code"], "mutation_reconciliation_required")
            if in_progress:
                from threading import Event
                from tests.support.http import lifespan_client

                started, release = Event(), Event()

                def pause(**kwargs: Any) -> Any:
                    if kwargs["path"] == "/api/project.create":
                        started.set()
                        if not release.wait(30):
                            raise TimeoutError("The test did not release original placement.")
                    return self.provider(**kwargs)

                async def concurrent() -> Any:
                    async with lifespan_client(app) as client:
                        body = self.request.model_dump(mode="json") | {"mode": "apply"}
                        headers = {
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": "activation",
                        }
                        first = asyncio.create_task(
                            request(
                                client,
                                "POST",
                                ROUTE.format(product=self.product),
                                headers=headers,
                                payload=body,
                            )
                        )
                        try:
                            self.assertTrue(await asyncio.to_thread(started.wait, 10))
                            second = await request(
                                client,
                                "POST",
                                ROUTE.format(product=self.product),
                                headers=headers,
                                payload=body,
                            )
                            self.assertEqual(second.status_code, 409, second.text)
                            self.assertEqual(second.json()["error"]["code"], "mutation_in_progress")
                        finally:
                            release.set()
                        return await first

                with patch("control_plane.dokploy.api.dokploy_request", side_effect=pause):
                    applied = asyncio.run(concurrent())
            else:
                applied = asyncio.run(post("apply"))
            self.assertEqual(applied.status_code, 202, applied.text)
            self.assertEqual(asyncio.run(post("apply")).status_code, 202)
            before = tuple(self.writes)
            alternate = asyncio.run(post("apply", "different-key"))
            self.assertEqual(alternate.status_code, 409, alternate.text)
            self.assertEqual(tuple(self.writes), before)
        else:
            self.assertEqual(self.activate().status, "completed")
        receipt = asyncio.run(post("receipt"))
        self.assertEqual(receipt.status_code, 202, receipt.text)
        self.assertTrue(receipt.json()["result"]["deployment_requested"])
        self.assertEqual(receipt.json()["result"]["separation_verified"], not failed)
        if failed:
            self.assertFalse(receipt.json()["result"]["applied"])
            self.assertEqual(receipt.json()["result"]["reason"], "candidate_deployment_failed")
            for mode in ("readback", "dry-run"):
                observed = asyncio.run(post(mode))
                self.assertEqual(observed.status_code, 202, observed.text)
                self.assertEqual(observed.json()["result"], receipt.json()["result"])
        self.assertNotIn("retained-only-password", receipt.text)

    def test_supported_http_dry_run_and_original_receipt(self) -> None:
        self.assert_supported_http_activation(native_apply=False)

    def test_historical_wait_receipt_preserves_original_candidate_custody_without_rewriting_state(
        self,
    ) -> None:
        from control_plane.testing_application_activation import activation_image_record
        from control_plane.testing_application_inspection import inspect_application_candidate
        from control_plane.provider_operations import build_provider_operation_key
        from control_plane.contracts.testing_activation import TestingActivationRefusal

        self.prepare()
        completed = self.activate()
        self.assertEqual(completed.status, "completed")
        assert completed.record is not None
        row = completed.record
        operation_key = build_provider_operation_key(
            scope=row.scope,
            route_path=row.route_path,
            idempotency_key=row.idempotency_key,
            request_fingerprint=row.request_fingerprint,
            reconciliation_key=row.reconciliation_key,
        )
        current = self.adapter.current()
        job = self.deployments[current.target_id][0]
        receipt = api._wait_for_deployment_status(
            fetch_latest_deployment=lambda: deepcopy(job),
            before_key="",
            timeout_seconds=1,
            failure_message_prefix="Fixture deployment",
        )
        self.assertEqual(
            current.state_recipient["application_deployment_id"], api.deployment_key(job)
        )
        record = current.model_copy(
            update={
                "state_recipient": {**current.state_recipient, "application_deployment_id": receipt}
            }
        )
        before = deepcopy((self.writes, self.schedules, self.schedule_history))
        self.adapter.require_terminal_candidate(record, operation_key)
        self.assertEqual(
            activation_image_record(record).deploy.deployment_id, api.deployment_key(job)
        )
        self.assertTrue(
            inspect_application_candidate(
                self.adapter, record, operation_key
            ).sole_deployment_id_matches
        )
        for invalid in (
            "",
            receipt + " extra",
            receipt + " deployment=other",
            receipt.replace("status=", "unknown="),
            "foreign-job",
        ):
            with self.subTest(receipt=invalid):
                changed = record.model_copy(
                    update={
                        "state_recipient": {
                            **record.state_recipient,
                            "application_deployment_id": invalid,
                        }
                    }
                )
                with self.assertRaises(TestingActivationRefusal):
                    self.adapter.require_terminal_candidate(changed, operation_key)
                self.assertFalse(
                    inspect_application_candidate(
                        self.adapter, changed, operation_key
                    ).sole_deployment_id_matches
                )
        self.assertEqual(self.adapter.current(), current)
        self.assertEqual(record.state_recipient["application_deployment_id"], receipt)
        self.assertEqual((self.writes, self.schedules, self.schedule_history), before)

    def test_original_candidate_inspection_identifies_terminal_predicates_without_values_or_writes(
        self,
    ) -> None:
        from control_plane.testing_application_inspection import inspect_application_candidate
        from control_plane.provider_operations import build_provider_operation_key

        self.prepare()
        completed = self.activate()
        self.assertEqual(completed.status, "completed")
        assert completed.record is not None
        row = completed.record
        record = self.adapter.current()
        operation_key = build_provider_operation_key(
            scope=row.scope,
            route_path=row.route_path,
            idempotency_key=row.idempotency_key,
            request_fingerprint=row.request_fingerprint,
            reconciliation_key=row.reconciliation_key,
        )
        before = deepcopy((self.writes, self.schedules, self.schedule_history))
        observed = inspect_application_candidate(self.adapter, record, operation_key)
        self.assertEqual(observed.placement, "matched")
        self.assertEqual(observed.domains, "empty")
        self.assertEqual(observed.history, "complete")
        self.assertEqual(observed.deployment_count, len(self.deployments[record.target_id]))
        self.assertTrue(observed.stored_deployment_recorded)
        self.assertTrue(observed.sole_deployment_id_matches)
        self.assertTrue(observed.sole_deployment_title_matches)
        self.assertEqual(observed.sole_deployment_status, "terminal_success")
        self.assertEqual(observed.current_runtime_status, "available")
        self.assertEqual(observed.current_http_status, 200)
        self.assertEqual(observed.current_identity_status, "match")
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=legacy.activation_hold_policy(self.product, self.context),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        with (
            patch(
                "control_plane.testing_activation_inspection.activation_record",
                return_value=record.model_copy(update={"phase": "restoring_application_route"}),
            ),
            patch(
                "control_plane.testing_activation_inspection.inspect_preparation_programs",
                return_value=[],
            ),
        ):
            response = asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "activation",
                    },
                    payload=self.request.model_dump(mode="json") | {"mode": "inspect"},
                )
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["result"]["candidate"]["sole_deployment_id_matches"])
        self.assertEqual(response.json()["result"]["candidate"]["current_http_status"], 200)
        for private in (
            record.target_id,
            record.app_name,
            "retained-only-password",
            self.deployments[record.target_id][0]["title"],
        ):
            self.assertNotIn(private, observed.model_dump_json())
        original = deepcopy(self.payloads[record.target_id])
        for changes, expected in (
            ({}, "missing"),
            ({"domains": None}, "malformed"),
            ({"domains": [{"host": "private.example"}]}, "nonempty"),
        ):
            self.payloads[record.target_id] = {
                key: value for key, value in original.items() if key != "domains"
            } | changes
            self.assertEqual(
                inspect_application_candidate(self.adapter, record, operation_key).domains, expected
            )
        self.payloads[record.target_id] = original
        with (
            patch.object(
                api,
                "list_deployments_for_target",
                side_effect=ValueError("private provider detail"),
            ),
            self.assertLogs("control_plane.http_error_evidence", level="WARNING") as diagnostic,
        ):
            unknown = inspect_application_candidate(self.adapter, record, operation_key)
        self.assertEqual(unknown.history, "unknown")
        self.assertIsNone(unknown.deployment_count)
        self.assertIsNone(unknown.sole_deployment_id_matches)
        self.assertNotIn("private provider detail", unknown.model_dump_json())
        self.assertNotIn("private provider detail", "\n".join(diagnostic.output))
        self.deployments[record.target_id].append(
            {"deploymentId": "foreign-job", "title": "private foreign title", "status": "running"}
        )
        multiple = inspect_application_candidate(self.adapter, record, operation_key)
        self.assertEqual(multiple.deployment_count, len(self.deployments[record.target_id]))
        self.assertIsNone(multiple.sole_deployment_status)
        from control_plane.testing_application_runtime import VerifiedApplicationRuntimeFailure

        with patch(
            "control_plane.testing_application_inspection.inspect_application_runtime",
            side_effect=VerifiedApplicationRuntimeFailure(
                {
                    "http_status": 503,
                    "identity_status": "missing",
                    "health_payload": {"private": "unreported"},
                }
            ),
        ):
            failed = inspect_application_candidate(self.adapter, record, operation_key)
        self.assertEqual(failed.current_http_status, 503)
        self.assertEqual(failed.current_identity_status, "missing")
        self.assertNotIn("unreported", failed.model_dump_json())
        self.payloads[record.target_id]["appName"] = "foreign-private-application"
        with patch(
            "control_plane.testing_application_inspection.inspect_application_runtime",
            side_effect=AssertionError("Changed placement must not be probed"),
        ):
            changed = inspect_application_candidate(self.adapter, record, operation_key)
        self.assertEqual(changed.placement, "changed")
        self.assertEqual(changed.current_runtime_status, "not_read")
        self.assertEqual((self.writes, self.schedules, self.schedule_history), before)
        self.assertEqual(self.adapter.current(), record)

    def test_original_route_inspection_reads_bound_receipts_without_provider_or_writes(
        self,
    ) -> None:
        from control_plane.testing_activation_inspection import (
            inspect_application_activation,
            inspect_application_route_receipts,
        )
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE
        from control_plane.testing_route_effects import ROUTE_WRITER_RECEIPT

        routing, _, _ = self.private_addressed()
        self.prepare()
        self.assertEqual(self.activate().status, "completed")
        record = self.adapter.current()
        readiness = record.application_readiness
        assert readiness is not None and readiness.route_execution is not None
        assert readiness.private_health is not None and readiness.public_health is not None
        forward = self.store.read_idempotency_record(
            scope=record.scope,
            route_path=ROUTE.format(product=self.product) + ":route",
            idempotency_key=record.plan_sha256 + ":route",
        )
        assert forward is not None
        writer = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_WRITER_RECEIPT,
            idempotency_key=readiness.route_execution.key(attempt=forward.attempt),
        )
        assert writer is not None
        before = deepcopy(
            (self.writes, self.schedules, self.schedule_history, routing.effect_count)
        )
        read = self.store.read_idempotency_record
        with (
            patch(
                "control_plane.dokploy.api.dokploy_request", side_effect=AssertionError
            ) as provider_reads,
            patch(
                "control_plane.secrets._decrypt_secret_value", side_effect=AssertionError
            ) as secret_reads,
        ):
            observed = inspect_application_route_receipts(self.store, record)
            self.assertEqual(observed[0].state, forward.state)
            self.assertEqual(observed[0].attempt, forward.attempt)
            self.assertEqual(observed[0].status, "bound")
            self.assertEqual(observed[0].writer_status, "bound")
            self.assertEqual(observed[0].writer_state, writer.state)
            self.assertEqual(observed[0].writer_outcome, writer.response_payload["writer_outcome"])
            self.assertEqual(observed[1].status, "not_found")
            with patch(
                "control_plane.testing_activation_inspection.inspect_preparation_programs",
                return_value=[],
            ):
                integrated = inspect_application_activation(
                    root=self.root,
                    store=self.store,
                    product=self.product,
                    request=self.request,
                    actor=self.actor,
                    scope=self.scope,
                    idempotency_key="activation",
                    trace_id="original-route-inspection",
                    observe_original=self.adapter.observe,
                )
            self.assertEqual(integrated.routes, observed)
            self.assertEqual(integrated.private_health_status, readiness.private_health.status)
            self.assertEqual(integrated.public_health_status, readiness.public_health.status)
            intent = self.store.read_original_mutation_intent
            for original in (forward, writer):
                with (
                    self.subTest(missing_intent=original.record_id),
                    patch.object(
                        self.store,
                        "read_original_mutation_intent",
                        side_effect=lambda row, selected=original: (
                            {} if row == selected else intent(row)
                        ),
                    ),
                ):
                    missing = inspect_application_route_receipts(self.store, record)[0]
                    if original == forward:
                        self.assertEqual(missing.status, "unbound")
                        self.assertEqual(missing.writer_status, "not_read")
                    else:
                        self.assertEqual(missing.writer_status, "unbound")
            for original, changes in (
                (forward, {"request_fingerprint": "foreign-private-fingerprint"}),
                (writer, {"request_fingerprint": "foreign-private-fingerprint"}),
                (writer, {"response_payload": {"writer_outcome": "private-provider-message"}}),
                (writer, {"state": "reconcile_required"}),
                (
                    forward,
                    {
                        "state": "reconcile_required",
                        "response_payload": {"route_write_not_started": True},
                    },
                ),
            ):
                with self.subTest(changes=changes, record=original.record_id):
                    replacement = original.model_copy(update=changes)

                    def changed_read(**kwargs: Any) -> Any:
                        found = read(**kwargs)
                        return replacement if found == original else found

                    with patch.object(
                        self.store, "read_idempotency_record", side_effect=changed_read
                    ):
                        changed = inspect_application_route_receipts(self.store, record)[0]
                    if "state" in changes:
                        self.assertEqual(changed.status, "bound")
                        if original == forward:
                            self.assertIsNone(changed.route_write_not_started)
                        else:
                            self.assertIsNone(changed.writer_outcome)
                    elif original == forward:
                        self.assertEqual(changed.status, "unbound")
                        self.assertIsNone(changed.state)
                        self.assertEqual(changed.writer_status, "not_read")
                    elif "request_fingerprint" in changes:
                        self.assertEqual(changed.writer_status, "unbound")
                        self.assertIsNone(changed.writer_state)
                        self.assertIsNone(changed.writer_outcome)
                    else:
                        self.assertEqual(changed.writer_outcome, "unrecognized")
                    serialized = changed.model_dump_json()
                    self.assertNotIn("private-provider-message", serialized)
                    self.assertNotIn("foreign-private-fingerprint", serialized)
            serialized = "".join(item.model_dump_json() for item in observed)
            self.assertNotIn(readiness.backend, serialized)
            self.assertNotIn("retained-only-password", serialized)
            provider_reads.assert_not_called()
            secret_reads.assert_not_called()
        self.assertEqual(
            (self.writes, self.schedules, self.schedule_history, routing.effect_count), before
        )
        self.assertEqual(self.adapter.current(), record)
        self.assertEqual(
            read(
                scope=forward.scope,
                route_path=forward.route_path,
                idempotency_key=forward.idempotency_key,
            ),
            forward,
        )
        self.assertEqual(
            read(
                scope=writer.scope,
                route_path=writer.route_path,
                idempotency_key=writer.idempotency_key,
            ),
            writer,
        )

    def test_original_activation_inspection_retains_negative_program_evidence_without_writes(
        self,
    ) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.testing_application_capabilities import (
            RETAINED_CAPABILITY_PROGRAM_NAME,
            retained_application_capability_spec,
            retained_application_capability_program,
        )
        from control_plane.testing_separation_resources import APPLICATION_CAPABILITY_PHASE
        from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
        from tests.support.http import request

        self.prepare()

        def refuse(**kwargs: Any) -> None:
            kwargs["before_effect"](APPLICATION_CAPABILITY_PHASE)
            raise ValueError("private-unreported-provider-error")

        with patch(
            "control_plane.testing_application_capabilities.inspect_retained_application_capabilities",
            side_effect=refuse,
        ):
            refused = self.activate()
        self.assertEqual(refused.status, "completed", refused)
        record = self.adapter.current()
        self.assertIsNotNone(record.failure)
        spec = retained_application_capability_spec(
            self.store, self.root, self.product, self.context
        )
        from control_plane.dokploy import post_deploy

        _, lookup, _, _ = post_deploy._resolve_dokploy_schedule_runtime(
            host="https://provider.test",
            token="fixture-token",
            compose_id=self.old_id,
            compose_name=self.context,
            target_payload=self.payloads[self.old_id],
        )
        schedule: dict[str, Any] = {
            "scheduleId": "original-capability",
            "name": RETAINED_CAPABILITY_PROGRAM_NAME,
            "appName": record.namespace,
            "applicationId": lookup,
            "enabled": False,
            "script": retained_application_capability_program(spec),
        }
        self.schedules[schedule["scheduleId"]] = schedule
        self.schedules["preparation-capability"] = {
            **schedule,
            "scheduleId": "preparation-capability",
            "appName": record.namespace.removesuffix("-recipient"),
        }
        execution = {
            "deploymentId": "original-capability-run",
            "scheduleId": schedule["scheduleId"],
            "status": "error",
            "startedAt": self.old.now(-1),
            "finishedAt": self.old.now(),
        }
        self.schedule_history[schedule["scheduleId"]] = [execution]
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=legacy.activation_hold_policy(self.product, self.context),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        before = deepcopy((self.writes, self.schedules, self.schedule_history))
        with (
            patch(
                "control_plane.testing_separation_inspection.post_deploy._read_odoo_post_deploy_log_markers",
                return_value={"log_available": "true", "testing_resources_ok": "false"},
            ),
            patch("control_plane.secrets._decrypt_secret_value", side_effect=AssertionError),
        ):
            response = asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "activation",
                    },
                    payload=self.request.model_dump(mode="json") | {"mode": "inspect"},
                )
            )
        self.assertEqual(response.status_code, 202, response.text)
        observed = response.json()["result"]
        assert refused.record is not None
        self.assertEqual(observed["state"], refused.record.state)
        self.assertEqual(observed["activation_phase"], record.phase)
        self.assertEqual(observed["provider_effect_phase"], refused.record.provider_effect_phase)
        self.assertEqual(observed["predecessor_recorded"], record.predecessor is not None)
        self.assertEqual(observed["external_capability_count"], len(record.external_capabilities))
        programs = {item["kind"]: item for item in observed["programs"]}
        capability = programs["retained_capability"]
        self.assertEqual(capability["schedule_id"], schedule["scheduleId"])
        self.assertEqual(capability["deployment_id"], execution["deploymentId"])
        self.assertEqual(capability["deployment_status"], execution["status"])
        self.assertIs(capability["resources_ok"], False)
        self.assertTrue(capability["script_matches"])
        self.assertIsNone(programs["retained_capability_readback"]["resources_ok"])
        self.assertNotIn("storage_preparation", programs)
        self.assertNotIn(schedule["script"], response.text)
        self.assertNotIn("private-unreported-provider-error", response.text)
        self.assertEqual((self.writes, self.schedules, self.schedule_history), before)
        self.assertEqual(self.adapter.current(), record)

        for key, changes in (
            ("", {}),
            ("different-original", {}),
            ("activation", {"source_commit": "f" * 40}),
            ("activation", {"preparation_key": "different-preparation"}),
        ):
            with (
                self.subTest(key=key, changes=changes),
                patch(
                    "control_plane.testing_activation_inspection.inspect_preparation_programs",
                    side_effect=AssertionError(
                        "Unbound inspection must not read provider programs"
                    ),
                ),
            ):
                denied = asyncio.run(
                    request(
                        app,
                        "POST",
                        ROUTE.format(product=self.product),
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": key,
                        },
                        payload=self.request.model_dump(mode="json")
                        | {"mode": "inspect"}
                        | changes,
                    )
                )
                self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual((self.writes, self.schedules, self.schedule_history), before)

        for changed in (
            None,
            record.model_copy(update={"actor": "different-agent"}),
            record.model_copy(update={"namespace": record.namespace + "-different"}),
            record.model_copy(update={"preparation_record_id": "different-preparation-record"}),
            record.model_copy(
                update={
                    "retained_target_id": record.retained_target_id.model_copy(
                        update={"target_id": "different-target"}
                    )
                }
            ),
        ):
            with (
                self.subTest(record=changed),
                patch(
                    "control_plane.testing_activation_inspection.activation_record",
                    return_value=changed,
                ),
                patch(
                    "control_plane.testing_activation_inspection.inspect_preparation_programs",
                    side_effect=AssertionError("Foreign custody must refuse before provider reads"),
                ),
            ):
                denied = asyncio.run(
                    request(
                        app,
                        "POST",
                        ROUTE.format(product=self.product),
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": "activation",
                        },
                        payload=self.request.model_dump(mode="json") | {"mode": "inspect"},
                    )
                )
                self.assertEqual(denied.status_code, 409, denied.text)

        foreign_app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=legacy.activation_hold_policy(self.product, self.context),
            bearer_identity_config=_local_operator_bearer_config(
                token_label="local-owner-write"
            ).model_copy(update={"local_operator_subject": "different-agent"}),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        with patch(
            "control_plane.testing_activation_inspection.inspect_preparation_programs",
            side_effect=AssertionError("Foreign caller must refuse before provider reads"),
        ):
            denied = asyncio.run(
                request(
                    foreign_app,
                    "POST",
                    ROUTE.format(product=self.product),
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "activation",
                    },
                    payload=self.request.model_dump(mode="json") | {"mode": "inspect"},
                )
            )
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual((self.writes, self.schedules, self.schedule_history), before)

    def addressed(self) -> Any:
        """Reuse the native transport's real isolated file/lock/process harness."""
        from tests.test_testing_route_transport import TestingRouteTransportTests
        from control_plane.dokploy.testing_route import testing_route_id
        from io import BytesIO
        from unittest.mock import Mock

        routing = TestingRouteTransportTests()
        routing.setUp(initialize_fence=False)
        self.addCleanup(routing.doCleanups)
        routing.route_file = routing.root / (testing_route_id(self.product, self.context) + ".yml")
        document: dict[str, Any] = {
            "http": {
                "routers": {
                    "old-app-router-fixture": {
                        "rule": "Host(`" + routing.plan.host + "`)",
                        "entryPoints": ["web"],
                        "service": "old-app-service-fixture",
                        "middlewares": ["old-app-redirect"],
                    },
                    "old-app-router-websecure-fixture": {
                        "rule": "Host(`" + routing.plan.host + "`)",
                        "entryPoints": ["websecure"],
                        "service": "old-app-service-fixture",
                        "tls": {"certResolver": "letsencrypt"},
                    },
                },
                "services": {
                    "old-app-service-fixture": {
                        "loadBalancer": {
                            "servers": [{"url": "http://old-app:8000"}],
                            "passHostHeader": True,
                        }
                    }
                },
                "middlewares": {
                    "old-app-redirect": {"redirectScheme": {"scheme": "https", "permanent": True}}
                },
            }
        }
        (routing.root / "old-app.yml").write_text(json.dumps(document))
        routing.raw = {"routers": {}, "services": {}, "middlewares": {}}
        for section, values in document["http"].items():
            for name, value in values.items():
                applied = deepcopy(value)
                if section == "routers":
                    applied["service"] += "@file"
                    applied["priority"] = len(applied["rule"].encode("utf-8"))
                    if "middlewares" in applied:
                        applied["middlewares"] = [item + "@file" for item in applied["middlewares"]]
                elif section == "services":
                    applied["loadBalancer"].update(
                        strategy="wrr", responseForwarding={"flushInterval": "100ms"}
                    )
                routing.raw[section][name + "@file"] = {**applied, "status": "enabled"}
        self.payloads[self.old_id]["domains"] = [
            {
                "domainId": "retained-domain",
                "applicationId": self.old_id,
                "host": routing.plan.host,
                "port": 8000,
                "path": "/",
                "https": True,
                "uniqueConfigKey": "fixture",
            }
        ]
        proxy = routing.containers["proxy"]
        proxy["NetworkSettings"]["Networks"]["dokploy-network"]["NetworkID"] = "fixture-network-id"
        self.containers["proxy"] = proxy
        routing.containers = self.containers
        native = routing.command

        def command(arguments: list[str], **kwargs: Any) -> Any:
            if arguments[1:3] == ["service", "ls"]:
                if self.before_service_list is not None:
                    self.before_service_list()
                if arguments[3:] not in (["-q"], ["--quiet"]):
                    return subprocess.CompletedProcess(
                        arguments, 125, b"", b"unsupported list option"
                    )
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout="\n".join(row["ID"][:12] for row in self.services.values()).encode(),
                    stderr=b"",
                )
            if arguments[1:4] == ["service", "inspect", "--format"]:
                selected = []
                for identifier in arguments[5:]:
                    matching = [
                        row for row in self.services.values() if row["ID"].startswith(identifier)
                    ]
                    if len(matching) != 1:
                        return subprocess.CompletedProcess(
                            arguments, 1, b"", b"ambiguous or missing service"
                        )
                    selected.append(matching[0])
                if arguments[4] == "{{json .ID}}":
                    rows = [row["ID"] for row in selected]
                    if self.service_id_resolution is not None:
                        rows = self.service_id_resolution(rows)
                    return subprocess.CompletedProcess(
                        arguments, 0, "\n".join(json.dumps(row) for row in rows).encode(), b""
                    )
                assert len(selected) == 1
                service = selected[0]
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout=json.dumps(
                        {
                            "name": service["Spec"]["Name"],
                            "networks": service["Spec"]["TaskTemplate"]["Networks"],
                        }
                    ).encode(),
                    stderr=b"",
                )
            if (
                arguments[1] == "version"
                or arguments[1:3]
                in (
                    ["service", "inspect"],
                    ["service", "ps"],
                    ["node", "ls"],
                    ["image", "inspect"],
                )
                or arguments[1:4]
                == [
                    "inspect",
                    "--type",
                    "task",
                ]
            ):
                result = self.docker(arguments, **kwargs)
                return subprocess.CompletedProcess(
                    arguments,
                    result.returncode,
                    stdout=result.stdout.encode(),
                    stderr=result.stderr.encode(),
                )
            return native(arguments, **kwargs)

        self.enterContext(patch.object(routing, "command", side_effect=command))
        self.stored_service_reads = 0
        self.stored_service_view = None
        self.after_stored_service_read = None

        def stdio() -> Any:
            from io import BytesIO
            from unittest.mock import MagicMock
            from urllib.parse import urlsplit, parse_qs

            process = MagicMock()
            process.__enter__.return_value = process
            process.poll.return_value = 0
            process.wait.return_value = 0

            def request(data: bytes) -> int:
                path = urlsplit(data.split(b" ")[1].decode())
                service = next(
                    row
                    for row in self.services.values()
                    if row["ID"] == path.path.rsplit("/", 1)[-1]
                )
                result = deepcopy(service)
                if parse_qs(path.query).get("insertDefaults") != ["false"]:
                    for key, default in self.cli_container_defaults.items():
                        result["Spec"]["TaskTemplate"]["ContainerSpec"].setdefault(
                            key, deepcopy(default)
                        )
                if self.stored_service_view is not None:
                    result = self.stored_service_view(result)
                body = json.dumps(result).encode()
                process.stdout = BytesIO(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                self.stored_service_reads += 1
                if self.after_stored_service_read is not None:
                    self.after_stored_service_read()
                return len(data)

            process.stdin.write.side_effect = request
            return process

        routing.docker_stdio = stdio
        profile = self.store.read_product_profile_record(self.product)
        profile.public_website = "required"
        lane = next(item for item in profile.lanes if item.instance == "testing")
        lane.base_url, lane.health_url = (
            "https://" + routing.plan.host,
            "https://" + routing.plan.host + "/health",
        )
        self.store.write_product_profile_record(profile)
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.public_hosts, target.domains = (routing.plan.host,), (routing.plan.host,)
        self.store.write_dokploy_target_record(target)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=target,
                target_id_record=self.store.read_dokploy_target_id_record(
                    context_name=self.context, instance_name="testing"
                ),
            )
        )
        self.retained = target

        class Health(BytesIO):
            def geturl(inner) -> str:
                return lane.health_url

        opener = Mock()

        def health(*args: Any, **kwargs: Any) -> Any:
            from urllib.parse import urlsplit

            application = "old-app"
            if routing.route_file.exists():
                document = json.loads(routing.route_file.read_text())
                backend = next(iter(document["http"]["services"].values()))["loadBalancer"][
                    "servers"
                ][0]["url"]
                application = urlsplit(backend).hostname
            environment = self.containers[str(application) + "-container"]["Config"]["Env"]
            identity = json.loads(
                dict(value.split("=", 1) for value in environment)[
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"
                ]
            )
            return Health(json.dumps({"runtime_identity": identity}).encode())

        opener.open.side_effect = health
        self.enterContext(patch("urllib.request.build_opener", return_value=opener))
        return routing

    def test_database_clock_stamps_fresh_route_observation_under_service_skew(self) -> None:
        from datetime import datetime, timedelta, timezone

        self.addressed()
        self.prepare()

        def ahead() -> str:
            return (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        with patch(
            "control_plane.testing_application_activation.utc_now_timestamp", side_effect=ahead
        ):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        self.assertTrue(self.adapter.current().published)

    def test_publication_refuses_stale_route_readiness_without_changing_custody(self) -> None:
        from datetime import datetime, timedelta

        self.addressed()
        self.prepare()
        write = self.store.write_testing_activation
        attempted: list[str] = []

        def publish(**kwargs: Any) -> None:
            if kwargs["replacement"]["published"]:
                before = self.adapter.current()
                stale = deepcopy(kwargs["replacement"])
                readiness = stale["application_readiness"]
                observed = datetime.fromisoformat(readiness["observed_at"].replace("Z", "+00:00"))
                readiness["observed_at"] = (observed - timedelta(hours=1)).isoformat()
                attempted.append(readiness["observed_at"])
                with self.assertRaises(ValueError):
                    write(**{**kwargs, "replacement": stale})
                self.assertEqual(self.adapter.current(), before)
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=publish):
            result = self.activate()
        self.assertTrue(attempted)
        self.assertEqual(result.status, "completed", result)
        self.assertTrue(self.adapter.current().published)

    def test_retained_fence_is_not_separated_when_binding_changes_between_reads(self) -> None:
        from control_plane.testing_activation import retained_fence_is_separated
        from control_plane.generic_web_testing_exclusion import bind_testing_recipient

        self.prepare()
        self.assertEqual(self.activate().status, "completed")
        self.assertTrue(retained_fence_is_separated(self.store, self.original))
        identifier = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        provider = self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        )
        rebound: list[str] = []

        def return_to_original(*args: Any, **kwargs: Any) -> Any:
            self.store.write_dokploy_target_record(self.retained)
            self.store.write_dokploy_target_id_record(
                identifier.model_copy(update={"target_id": self.old_id})
            )
            self.store.write_provider_target_record(
                provider.model_copy(update={"target_id": self.old_id})
            )
            for alias in self.store.list_dokploy_target_id_records():
                if alias.target_id == self.old_id and (alias.context, alias.instance) != (
                    self.context,
                    "testing",
                ):
                    self.store.delete_dokploy_target_id_record(expected_record=alias)
            for registration in self.store.list_provider_target_records():
                if registration.target_id == self.old_id and (
                    registration.context,
                    registration.instance,
                ) != (self.context, "testing"):
                    self.store.delete_provider_target_record(expected_record=registration)
            bound = bind_testing_recipient(*args, **kwargs)
            rebound.append(bound[2].target_id)
            return bound

        with patch(
            "control_plane.testing_activation.bind_testing_recipient",
            side_effect=return_to_original,
        ):
            self.assertFalse(retained_fence_is_separated(self.store, self.original))
        self.assertEqual(rebound, [self.old_id])

    def test_private_ingress_refuses_a_serving_application_moved_to_another_server(self) -> None:
        _, ingress, _ = self.private_addressed()
        self.prepare()
        self.assertEqual(self.activate().status, "completed")
        self.assertTrue(self.adapter.current().published)
        ingress.plan()
        self.payloads[self.adapter.current().target_id]["serverId"] = "another-server"
        with self.assertRaises(ValueError):
            ingress.plan()

    def test_addressed_publication_correction_restores_route_and_allows_fresh_generation(
        self,
    ) -> None:
        from control_plane.dokploy.testing_route import observe_testing_route

        routing = self.addressed()
        self.prepare()
        write = self.store.write_testing_activation
        corrected: list[Any] = []

        def correct(**kwargs: Any) -> None:
            if kwargs["replacement"]["published"] and not corrected:
                record = self.adapter.current()
                readiness = record.application_readiness
                assert readiness is not None and readiness.route is not None
                self.assertEqual(
                    observe_testing_route(
                        readiness.route, routing.raw, json.loads(routing.route_file.read_text())
                    ),
                    readiness.backend,
                )
                (runtime,) = self.store.list_runtime_environment_records(
                    context_name=self.context, instance_name="testing"
                )
                runtime.env["APPLICATION_MODE"] = "permanent-reviewed-correction"
                self.store.write_runtime_environment_record(runtime)
                corrected.append(runtime)
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=correct):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        record = self.adapter.current()
        assert record.failure is not None
        self.assertEqual(record.failure.kind, "publication_refused")
        self.assertFalse(record.published)
        readiness = record.application_readiness
        assert readiness is not None and readiness.route is not None
        self.assertEqual(
            observe_testing_route(
                readiness.route, routing.raw, json.loads(routing.route_file.read_text())
            ),
            readiness.route.old_backend,
        )
        self.assertEqual(
            self.store.list_runtime_environment_records(
                context_name=self.context, instance_name="testing"
            ),
            (corrected[0],),
        )
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )
        before = tuple(self.writes)
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(tuple(self.writes), before)
        old_target = record.target_id
        # A second refused generation restores the same effective authority;
        # it must not make the next template selection artificially ambiguous.
        self.prepare("second-route-refusal")
        self.activation_key = "second-route-refusal"
        with patch.object(
            self.adapter, "publish_application", side_effect=self.corrected_publication
        ):
            second = self.activate()
        self.assertEqual(second.status, "completed", second)
        failure = self.adapter.current().failure
        assert failure is not None
        self.assertEqual(failure.kind, "publication_refused")
        self.prepare("after-route-refusal")
        self.activation_key = "after-route-refusal"
        successor = self.activate()
        self.assertEqual(successor.status, "completed", successor)
        self.assertNotEqual(self.adapter.current().target_id, old_target)
        self.assertTrue(self.adapter.current().published)

    def test_lost_inverse_reply_settles_its_original_and_never_resumes_forward(self) -> None:
        self.cli_container_defaults = {"DNSConfig": {}, "StopGracePeriod": 17}
        self.assert_lost_inverse_reentry()

    def test_proxy_restart_after_lost_inverse_reply_reuses_original_child(self) -> None:
        self.assert_lost_inverse_reentry(restart=True)

    def assert_lost_inverse_reentry(self, *, restart: bool = False) -> None:
        from control_plane.dokploy.testing_route import observe_testing_route

        routing, _, _ = self.private_addressed()
        self.unhealthy_retained_runtime(omit_identity_default=True)
        self.prepare()
        native = routing.command
        lost = False

        def lose_inverse(arguments: list[str], **kwargs: Any) -> Any:
            nonlocal lost
            result = native(arguments, **kwargs)
            record = self.adapter.current()
            readiness = record.application_readiness
            if (
                readiness is not None
                and readiness.route is not None
                and not lost
                and record.phase == "restoring_application_route"
                and routing.route_file.exists()
                and arguments[1:3] == ["exec", "-i"]
                and observe_testing_route(
                    readiness.route, routing.raw, json.loads(routing.route_file.read_text())
                )
                == readiness.route.old_backend
            ):
                lost = True
                raise subprocess.TimeoutExpired(arguments, 1)
            return result

        with (
            patch.object(routing, "command", side_effect=lose_inverse),
            patch.object(
                self.adapter, "publish_application", side_effect=self.corrected_publication
            ),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            first = self.activate()
        self.assertTrue(lost)
        self.assertEqual(first.status, "reconcile_required", first)
        self.assertEqual(self.adapter.current().phase, "restoring_application_route")
        if restart:
            routing.process_start.write_text("200")
            routing.containers["proxy"]["State"]["StartedAt"] = "successor-proxy-start"
        before = tuple(self.writes)
        result = self.activate()
        self.assertIn(result.status, {"completed", "adopted"}, result)
        failure = self.adapter.current().failure
        assert failure is not None
        self.assertEqual(failure.kind, "publication_refused")
        self.assertEqual(tuple(self.writes), before)
        self.assertEqual(self.canonical_password(), "retained-only-password")

    def test_proxy_restart_before_inverse_retains_distinct_inverse_custody(self) -> None:
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE
        from control_plane.dokploy.testing_route_execution import TestingRouteExecutionIntent
        from control_plane.testing_activation_inspection import inspect_application_route_receipts
        from control_plane.testing_route_effects import ROUTE_WRITER_RECEIPT

        routing = self.addressed()
        self.prepare()

        def correction(record: Any, checkpoint: Any) -> Any:
            routing.process_start.write_text("200")
            routing.containers["proxy"]["State"]["StartedAt"] = "before-inverse-proxy-start"
            return self.corrected_publication(record, checkpoint)

        with patch.object(self.adapter, "publish_application", side_effect=correction):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        completed = self.adapter.current()
        assert completed.failure is not None
        self.assertEqual(completed.failure.kind, "publication_refused")
        inverse = self.store.read_idempotency_record(
            scope=completed.scope,
            route_path=ROUTE.format(product=self.product) + ":route-inverse",
            idempotency_key=completed.plan_sha256 + ":route-inverse",
        )
        assert inverse is not None
        execution = TestingRouteExecutionIntent.model_validate(
            self.store.read_original_mutation_intent(inverse)["route_execution"]
        )
        writer = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_WRITER_RECEIPT,
            idempotency_key=execution.key(attempt=inverse.attempt),
        )
        assert writer is not None
        inspected = inspect_application_route_receipts(self.store, completed)[1]
        self.assertEqual(inspected.status, "bound")
        self.assertEqual(inspected.writer_status, "bound")
        self.assertEqual(inspected.writer_state, writer.state)
        self.assertEqual(inspected.writer_outcome, writer.response_payload["writer_outcome"])

    def test_persisted_inverse_pauses_for_staff_use_then_restores_original(self) -> None:
        routing, _, _ = self.private_addressed()
        self.unhealthy_retained_runtime()
        self.prepare()
        restore = self.adapter.restore_application_route

        forward = []

        def pause(*args: Any) -> Any:
            forward.append((routing.route_file.read_bytes(), routing.effect_count))
            self.record_staff_usage("in_use")
            return restore(*args)

        with (
            patch.object(
                self.adapter, "publish_application", side_effect=self.corrected_publication
            ),
            patch.object(self.adapter, "restore_application_route", side_effect=pause),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required", first)
        self.assertEqual(self.adapter.current().phase, "restoring_application_route")
        self.assertEqual((routing.route_file.read_bytes(), routing.effect_count), forward[0])
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=self.scope,
                route_path=ROUTE.format(product=self.product) + ":route-inverse",
                idempotency_key=self.adapter.current().plan_sha256 + ":route-inverse",
            )
        )
        before = forward[0]
        self.assertEqual(self.activate().status, "reconcile_required")
        self.assertEqual((routing.route_file.read_bytes(), routing.effect_count), before)
        self.record_staff_usage("unused")
        original_service = deepcopy(self.services["old-app"])
        for problem in ("service-id", "template"):
            try:
                with self.subTest(problem=problem):
                    if problem == "service-id":
                        self.services["old-app"]["ID"] += "-recreated"
                    else:
                        self.services["old-app"]["Spec"]["TaskTemplate"]["ContainerSpec"][
                            "Mounts"
                        ] = []
                    self.assertEqual(self.activate().status, "reconcile_required")
                    self.assertEqual(
                        (routing.route_file.read_bytes(), routing.effect_count), before
                    )
            finally:
                self.services["old-app"] = deepcopy(original_service)
        self.assertIn(self.activate().status, {"completed", "adopted"})
        completed = self.adapter.current()
        assert completed.failure is not None
        self.assertEqual(completed.failure.kind, "publication_refused")

    def test_inverse_restores_before_unrelated_candidate_job_holds_negative_receipt(self) -> None:
        from control_plane.dokploy.testing_route import observe_testing_route

        routing = self.addressed()
        self.prepare()

        def correction(record: Any, checkpoint: Any) -> Any:
            self.deployments[record.target_id].append(
                {
                    "deploymentId": "foreign-job",
                    "title": "Unrelated manual job",
                    "status": "done",
                }
            )
            return self.corrected_publication(record, checkpoint)

        with (
            patch.object(self.adapter, "publish_application", side_effect=correction),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            result = self.activate()
        self.assertEqual(result.status, "reconcile_required", result)
        record = self.adapter.current()
        self.assertIsNone(record.failure)
        self.assertEqual(record.phase, "restoring_application_route")
        readiness = record.application_readiness
        assert readiness is not None and readiness.route is not None
        self.assertEqual(
            observe_testing_route(
                readiness.route, routing.raw, json.loads(routing.route_file.read_text())
            ),
            readiness.route.old_backend,
        )
        self.assertEqual(self.canonical_password(), "retained-only-password")

    def test_newer_serving_target_blocks_inverse_without_overwriting_correction(self) -> None:
        routing = self.addressed()
        self.prepare()
        write = self.store.write_testing_activation
        corrected: list[Any] = []

        def correct(**kwargs: Any) -> None:
            if kwargs["replacement"]["published"] and not corrected:
                target_id = self.store.read_dokploy_target_id_record(
                    context_name=self.context, instance_name="testing"
                )
                correction = target_id.model_copy(
                    update={"target_id": "independent-newer-recipient"}
                )
                self.store.write_dokploy_target_id_record(correction)
                corrected.append(correction)
            write(**kwargs)

        with patch.object(self.store, "write_testing_activation", side_effect=correct):
            result = self.activate()
        self.assertEqual(result.status, "reconcile_required", result)
        self.assertIsNone(self.adapter.current().failure)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ),
            corrected[0],
        )
        self.assertTrue(routing.route_file.exists())
        self.assertEqual(self.canonical_password(), "retained-only-password")

    def test_addressed_native_physical_writer_keeps_origin_and_final_product_check_pending(
        self,
    ) -> None:
        routing = self.addressed()
        self.prepare()
        self.assertFalse(routing.route_file.exists())
        before_publish = self.adapter.publish_application

        def publish(record: Any, checkpoint: Any) -> Any:
            from control_plane.dokploy.testing_route import observe_testing_route

            self.assertEqual(
                observe_testing_route(
                    record.application_readiness.route,
                    routing.raw,
                    json.loads(routing.route_file.read_text()),
                ),
                record.application_readiness.backend,
            )
            self.assertEqual(self.canonical_password(), "retained-only-password")
            return before_publish(record, checkpoint)

        with patch.object(self.adapter, "publish_application", side_effect=publish):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        record = self.adapter.current()
        assert (
            record.published_target is not None
            and record.application_readiness is not None
            and record.application_readiness.public_health is not None
        )
        self.assertEqual(record.published_target.public_hosts, self.retained.public_hosts)
        self.assertEqual(record.application_readiness.public_health.status, "pass")
        self.assertEqual(
            self.store.read_deployment_record(
                record.application_readiness.runtime_identity.deployment_record_id
            ).destination_health.status,
            "pending",
        )
        effects = routing.effect_count
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(routing.effect_count, effects)

    def test_addressed_lost_writer_reply_settles_original_without_a_second_write(self) -> None:
        routing, _, _ = self.private_addressed()
        self.unhealthy_retained_runtime()
        self.prepare()
        native = routing.command
        lost = False

        def lose_publication(arguments: list[str], **kwargs: Any) -> Any:
            nonlocal lost
            result = native(arguments, **kwargs)
            if not lost and routing.route_file.exists() and arguments[1:3] == ["exec", "-i"]:
                lost = True
                raise subprocess.TimeoutExpired(arguments, 1)
            return result

        with (
            patch.object(routing, "command", side_effect=lose_publication),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-600)
            ),
        ):
            first = self.activate()
        self.assertEqual(first.status, "reconcile_required", first)
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertTrue(routing.route_file.exists())
        inode, document = routing.route_file.stat().st_ino, routing.route_file.read_bytes()
        second = self.activate()
        self.assertIn(second.status, {"completed", "adopted"}, second)
        self.assertEqual(routing.route_file.read_bytes(), document)
        self.assertEqual(routing.route_file.stat().st_ino, inode)

    def test_addressed_foreign_file_router_is_not_admitted_as_retained_application(self) -> None:
        routing = self.addressed()
        routing.raw["routers"]["foreign@file"] = deepcopy(
            next(iter(routing.raw["routers"].values()))
        )
        self.prepare()
        from control_plane.contracts.testing_activation import TestingActivationRefusal

        with self.assertRaises(TestingActivationRefusal):
            self.activate()
        self.assertFalse(self.projects)
        self.assertFalse(routing.route_file.exists())
        self.assertFalse(self.adapter.current().published)
        self.assertEqual(self.canonical_password(), "retained-only-password")

    def test_second_native_generation_keeps_both_original_operations_and_old_data(self) -> None:
        self.test_normal_lost_maintenance_native_activation_and_ordinary_successor()
        first = self.adapter.current()
        first_operation = self.store.read_testing_application_activation_operation(
            product=self.product, context=self.context, scope=self.scope, preparation_key="prepare"
        )
        self.lose_command = True
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=self.old.now(-3600)
        ):
            self.assertEqual(self.normal("second-original").status, "reconcile_required")
        self.lose_command = False
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.policies.staff_testing_hold = self.retained.policies.staff_testing_hold
        self.store.write_dokploy_target_record(target)
        self.retained = target
        self.old_id = first.target_id
        self.activation_key = "second-activation"
        self.prepare("second-preparation")
        result = self.activate()
        self.assertEqual(result.status, "completed", result)
        second = self.adapter.current()
        self.assertNotEqual(first.target_id, second.target_id)
        self.assertNotEqual(first.namespace, second.namespace)
        self.assertEqual(
            self.store.read_testing_application_activation_operation(
                product=self.product,
                context=self.context,
                scope=self.scope,
                preparation_key="prepare",
            ),
            first_operation,
        )
        self.assertEqual((self.old_backing / "keep").read_text(), "Retained original data")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )

        from dataclasses import replace
        from unittest.mock import Mock
        from control_plane.storage.postgres import (
            ORIGINAL_MUTATION_INTENT_ROUTE,
            LaunchplaneIdempotencyRow,
        )
        from control_plane.workflows.public_ingress_monitor import (
            _deploy_fence_monitor_target,
            check_public_ingress_target,
        )

        snapshot = self.store.read_provider_hold_snapshot(
            product=self.product, context=self.context, instance="testing"
        )
        self.assertFalse(snapshot.holds)
        profile = self.store.read_product_profile_record(self.product)
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        observation = check_public_ingress_target(
            target=replace(
                _deploy_fence_monitor_target(profile=profile, lane=lane, fence=None),
                provider_hold_snapshot=snapshot,
            ),
            checked_at=self.old.now(),
            timeout_seconds=1,
            http_get=Mock(),
            tls_get=Mock(),
        )
        self.assertEqual(observation.status, "pass")
        revisions = self.store.health_monitor_authority_revisions()
        assert first_operation is not None
        with self.store._session_factory() as session:
            original_row = session.scalar(
                select(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == first_operation.record_id,
                )
            )
            assert original_row is not None
            original_id = original_row.record_id
            original_payload = deepcopy(original_row.payload)
            foreign_payload = deepcopy(original_payload)
            foreign_payload["response_payload"]["original_intent"]["testing_activation"][
                "product"
            ] = "unrelated-product"
            foreign_payload["response_payload"].pop("reservation_id")
            foreign_payload["record_id"] = "unrelated-application-original"
            foreign_payload["idempotency_key"] = "unrelated-application"
            foreign = LaunchplaneIdempotencyRow(
                record_id=foreign_payload["record_id"],
                scope=original_row.scope,
                route_path=original_row.route_path,
                idempotency_key=foreign_payload["idempotency_key"],
            )
            self.store._sync_idempotency_row(
                foreign, type(first_operation).model_validate(foreign_payload)
            )
            session.add(foreign)
            session.commit()
        self.assertEqual(
            self.store.read_provider_hold_snapshot(
                product=self.product, context=self.context, instance="testing"
            ),
            snapshot,
        )
        for corruption in (
            "original-parent-link",
            "parent-fingerprint",
            "request",
            "preparation-key",
            "request-shape",
            "parent-id-type",
        ):
            with self.subTest(corruption=corruption):
                with self.store._session_factory.begin() as session:
                    row = session.get(
                        LaunchplaneIdempotencyRow,
                        original_id
                        if corruption != "parent-fingerprint"
                        else first_operation.record_id,
                    )
                    assert row is not None
                    saved_payload = deepcopy(row.payload)
                    corrupt_payload = deepcopy(saved_payload)
                    if corruption == "original-parent-link":
                        corrupt_payload["response_payload"].pop("reservation_id")
                    elif corruption == "parent-fingerprint":
                        corrupt_payload.pop("request_fingerprint")
                    elif corruption == "parent-id-type":
                        corrupt_payload["response_payload"]["reservation_id"] = 123
                    else:
                        captured = corrupt_payload["response_payload"]["original_intent"][
                            "testing_activation"
                        ]
                        if corruption == "request":
                            captured.pop("request")
                        elif corruption == "request-shape":
                            captured["request"] = []
                        else:
                            captured["request"].pop("preparation_key")
                    row.payload = corrupt_payload
                try:
                    unavailable = self.store.read_provider_hold_snapshot(
                        product=self.product, context=self.context, instance="testing"
                    )
                    self.assertIn(
                        self.original.record_id,
                        {hold.reservation.record_id for hold in unavailable.holds},
                    )
                    self.assertNotEqual(unavailable.source_sha256, snapshot.source_sha256)
                finally:
                    with self.store._session_factory.begin() as session:
                        row = session.get(LaunchplaneIdempotencyRow, saved_payload["record_id"])
                        assert row is not None
                        row.payload = saved_payload
                self.assertEqual(
                    self.store.read_provider_hold_snapshot(
                        product=self.product, context=self.context, instance="testing"
                    ),
                    snapshot,
                )
        from tests.test_public_ingress_monitor import _minutes_after

        with self.store._session_factory.begin() as session:
            for record_id in (original_id, first_operation.record_id):
                row = session.get(LaunchplaneIdempotencyRow, record_id)
                assert row is not None
                value = type(first_operation).model_validate(row.payload)
                self.store._sync_idempotency_row(
                    row,
                    value.model_copy(update={"updated_at": _minutes_after(value.updated_at, 1)}),
                )
        self.assertEqual(
            self.store.read_provider_hold_snapshot(
                product=self.product, context=self.context, instance="testing"
            ),
            snapshot,
        )
        self.assertEqual(self.store.health_monitor_authority_revisions(), revisions)
        with patch.object(
            self.store, "_database_mutation_timestamp", return_value=observation.observed_at
        ):
            self.assertEqual(
                self.store.write_public_ingress_transition_with_outbox(
                    observation=observation, incidents=(), expected_provider_hold_snapshot=snapshot
                ).status,
                "written",
            )
        previous_observations = self.store.list_public_ingress_observation_records()
        prod_snapshot = self.store.read_provider_hold_snapshot(
            product=self.product, context=self.context, instance="prod"
        )
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneIdempotencyRow, first_operation.record_id)
            assert row is not None
            result = first_operation.response_payload["result"]
            self.store._sync_idempotency_row(
                row,
                first_operation.model_copy(
                    update={
                        "response_payload": {
                            **first_operation.response_payload,
                            "result": {**result, "separation_verified": False},
                        }
                    }
                ),
            )
            session.commit()
        current = self.store.read_provider_hold_snapshot(
            product=self.product, context=self.context, instance="testing"
        )
        self.assertIn(
            self.original.record_id, {hold.reservation.record_id for hold in current.holds}
        )
        self.assertNotEqual(snapshot.source_sha256, current.source_sha256)
        self.assertEqual(
            self.store.read_provider_hold_snapshot(
                product=self.product, context=self.context, instance="prod"
            ),
            prod_snapshot,
        )
        lane_key = (self.product, self.context, "testing")
        self.assertNotEqual(
            revisions[lane_key], self.store.health_monitor_authority_revisions()[lane_key]
        )
        publication = self.store.write_public_ingress_transition_with_outbox(
            observation=observation, incidents=(), expected_provider_hold_snapshot=snapshot
        )
        self.assertEqual(publication.status, "provider_source_changed")
        self.assertEqual(
            self.store.list_public_ingress_observation_records(), previous_observations
        )
        from control_plane.provider_target_holds import ProviderOperationReference

        capsule = self.store.read_provider_target_origin(self.original)
        assert capsule is not None
        reference = ProviderOperationReference(
            self.original.record_id, capsule.operation_nonce
        ).encode()
        for reservation in self.store.list_held_provider_target_reservations():
            self.store.write_idempotency_record(
                reservation.model_copy(
                    update={
                        "state": "completed",
                        "response_status_code": 200,
                        "response_trace_id": "fixture-completed",
                        "recorded_at": reservation.updated_at,
                    }
                )
            )
        foreign_lane = (self.product + "-other", self.context, "testing")
        for attribution in ("retained", "corrupt"):
            with self.subTest(attribution=attribution):
                if attribution == "corrupt":
                    with self.store._session_factory.begin() as session:
                        row = session.scalar(
                            select(LaunchplaneIdempotencyRow).where(
                                LaunchplaneIdempotencyRow.route_path
                                == ORIGINAL_MUTATION_INTENT_ROUTE,
                                LaunchplaneIdempotencyRow.idempotency_key
                                == self.original.record_id,
                            )
                        )
                        assert row is not None
                        row.payload = {
                            **row.payload,
                            "response_payload": {
                                **row.payload["response_payload"],
                                "provider_target_origin": {},
                            },
                        }
                bulk = self.store.read_provider_hold_snapshots(
                    lane_keys=(lane_key, foreign_lane),
                    references_by_lane={foreign_lane: (reference,)},
                )
                self.assertEqual(
                    bulk[lane_key],
                    self.store.read_provider_hold_snapshot(
                        product=self.product, context=self.context, instance="testing"
                    ),
                )
                self.assertIn(reference, bulk[foreign_lane].unavailable_references)


class FreshApplicationFlowTests(unittest.TestCase):
    def fresh_fixture(self) -> ApplicationFlowTests:
        fixture = ApplicationFlowTests()
        fixture.no_original = True
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    @staticmethod
    def reserve_writer(fixture: ApplicationFlowTests, *, target_key: str) -> Any:
        from control_plane.contracts.provider_target_origin import (
            ProviderTargetOrigin,
            requires_provider_target_origin,
        )

        return fixture.store.reserve_mutation(
            scope="late-writer",
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key="late-writer",
            request_fingerprint="late-writer",
            lease_owner="late-writer",
            reconciliation_key="unattributed-original",
            provider_target_key=target_key,
            provider_target_origin=ProviderTargetOrigin(
                product=fixture.product,
                context=fixture.context,
                instance="testing",
                provider_id="dokploy",
                target_type="application",
                target_id=fixture.old_id,
            )
            if requires_provider_target_origin(target_key)
            else None,
        )

    def test_fresh_application_publishes_without_fabricating_an_original(self) -> None:
        fixture = self.fresh_fixture()
        old_payload = json.loads(json.dumps(fixture.payloads[fixture.old_id]))
        self.assertFalse(fixture.store.list_held_provider_target_reservations())
        fixture.prepare()
        result = fixture.activate()
        self.assertEqual(result.status, "completed", result)
        record = fixture.adapter.current()
        self.assertTrue(record.published)
        self.assertFalse(record.retained_reservations)
        self.assertNotEqual(record.target_id, fixture.old_id)
        self.assertEqual(fixture.payloads[fixture.old_id], old_payload)
        self.assertEqual((fixture.old_backing / "keep").read_text(), "Retained original data")
        self.assertNotEqual(fixture.canonical_password(), "retained-only-password")
        self.assertFalse(fixture.store.list_held_provider_target_reservations())

    def test_actual_rollback_identity_is_attributed_from_its_exact_record(self) -> None:
        fixture = self.fresh_fixture()
        physical = deepcopy((fixture.services, fixture.tasks, fixture.containers, fixture.images))
        self.assertEqual(fixture.normal("new-baseline").status, "completed")
        fixture.services, fixture.tasks, fixture.containers, fixture.images = physical
        fixture.prepare()
        result = fixture.activate()
        self.assertEqual(result.status, "completed", result)
        self.assertTrue(fixture.adapter.current().published)

    def test_unproved_observed_deployment_refuses_before_preparation_effects(self) -> None:
        fixture = self.fresh_fixture()
        observed = fixture.observed_application(application_id=fixture.old_id)
        read = fixture.store.read_deployment_record
        original = read(observed.service_deployment_record_id)
        assert original.deployed_target is not None
        physical = deepcopy((fixture.services, fixture.tasks, fixture.containers, fixture.images))
        self.assertEqual(fixture.normal("new-baseline").status, "completed")
        fixture.services, fixture.tasks, fixture.containers, fixture.images = physical
        writes = list(fixture.writes)
        variants = {
            "missing": None,
            "no-identity": original.model_copy(update={"runtime_identity": None}),
            "no-artifact": original.model_copy(update={"artifact_identity": None}),
            "foreign-lane": original.model_copy(update={"instance": "production"}),
            "foreign-target": original.model_copy(
                update={
                    "deployed_target": original.deployed_target.model_copy(
                        update={"target_id": "another-application"}
                    )
                }
            ),
        }
        for name, replacement in variants.items():
            with self.subTest(name=name):

                def deployment(record_id: str) -> Any:
                    if record_id != original.record_id:
                        return read(record_id)
                    if replacement is None:
                        raise FileNotFoundError(record_id)
                    return replacement

                with patch.object(fixture.store, "read_deployment_record", side_effect=deployment):
                    with self.assertRaises(ValueError):
                        fixture.prepare()
                self.assertEqual(fixture.writes, writes)
                self.assertFalse(fixture.store.list_held_provider_target_reservations())

    def test_unattributed_hold_cannot_supply_fresh_absence(self) -> None:
        fixture = self.fresh_fixture()
        self.reserve_writer(fixture, target_key="unknown-provider-target")
        writes = list(fixture.writes)
        with self.assertRaises(ValueError):
            fixture.prepare()
        self.assertEqual(fixture.writes, writes)

    def test_foreign_original_capsule_distinguishes_an_unrelated_hold(self) -> None:
        from control_plane.contracts.provider_target_origin import ProviderTargetOrigin

        for same_target in (False, True):
            with self.subTest(same_target=same_target):
                fixture = self.fresh_fixture()
                try:
                    origin = ProviderTargetOrigin(
                        product="another-product",
                        context="another-testing-context",
                        instance="testing",
                        provider_id="dokploy",
                        target_type="application",
                        target_id=fixture.old_id if same_target else "another-application",
                    )
                    reservation = fixture.store.reserve_mutation(
                        scope="foreign-writer",
                        route_path=GENERIC_WEB_DEPLOY_ROUTE,
                        idempotency_key="foreign-writer",
                        request_fingerprint="foreign-writer",
                        lease_owner="foreign-writer",
                        reconciliation_key="legacy-undecodable-target",
                        provider_target_key=origin.target_key(),
                        provider_target_origin=origin,
                    )
                    writes = list(fixture.writes)
                    if same_target:
                        with self.assertRaises(ValueError):
                            fixture.prepare()
                        self.assertEqual(fixture.writes, writes)
                    else:
                        fixture.prepare()
                        self.assertEqual(fixture.activate().status, "completed")
                        self.assertTrue(fixture.adapter.current().published)
                    self.assertEqual(
                        fixture.store.read_idempotency_record(
                            scope=reservation.record.scope,
                            route_path=reservation.record.route_path,
                            idempotency_key=reservation.record.idempotency_key,
                        ),
                        reservation.record,
                    )
                finally:
                    fixture.doCleanups()

    def test_runtime_read_failure_refuses_before_preparation_effects(self) -> None:
        from control_plane.dokploy.runtime_evidence import DokployEvidenceProviderError

        fixture = self.fresh_fixture()
        writes = list(fixture.writes)
        with patch(
            "control_plane.testing_application_capabilities.fetch_application_runtime",
            side_effect=DokployEvidenceProviderError("target-inspect"),
        ):
            with self.assertRaises(ValueError):
                fixture.prepare()
        self.assertEqual(fixture.writes, writes)
        self.assertFalse(fixture.store.list_held_provider_target_reservations())

    def test_late_writer_is_rechecked_inside_publication_transaction(self) -> None:
        for state in ("running", "reconcile_required"):
            with self.subTest(state=state):
                fixture = self.fresh_fixture()
                try:
                    fixture.prepare()
                    old_key = preparation_binding(
                        fixture.store,
                        fixture.product,
                        fixture.context,
                        fixture.actor,
                        fixture.root,
                        fixture.specification,
                    )[1]
                    write = fixture.store.write_testing_activation
                    inserted: list[Any] = []

                    def publish(**kwargs: Any) -> None:
                        if kwargs["replacement"]["published"] and not inserted:
                            reservation = self.reserve_writer(fixture, target_key=old_key)
                            inserted.append(reservation.record)
                            if state == "reconcile_required":
                                updated = fixture.store.mark_mutation_reconcile_required(
                                    reservation=reservation.record,
                                    reconciliation_key=reservation.record.reconciliation_key,
                                )
                                self.assertEqual(updated.status, "updated")
                        write(**kwargs)

                    with patch.object(
                        fixture.store, "write_testing_activation", side_effect=publish
                    ):
                        fixture.activate()
                    self.assertTrue(inserted)
                    self.assertFalse(fixture.adapter.current().published)
                    self.assertEqual(
                        fixture.store.read_dokploy_target_id_record(
                            context_name=fixture.context, instance_name="testing"
                        ).target_id,
                        fixture.old_id,
                    )
                    self.assertEqual(fixture.canonical_password(), "retained-only-password")
                finally:
                    fixture.doCleanups()

    def test_late_writer_keeps_original_target_before_and_after_creation(self) -> None:
        from control_plane.contracts.testing_activation import TestingActivationRefusal
        from control_plane.testing_separation_resources import preparation_binding

        for after_creation in (False, True):
            with self.subTest(after_creation=after_creation):
                fixture = self.fresh_fixture()
                try:
                    fixture.prepare()
                    old_key = preparation_binding(
                        fixture.store,
                        fixture.product,
                        fixture.context,
                        fixture.actor,
                        fixture.root,
                        fixture.specification,
                    )[1]
                    write = fixture.store.write_testing_activation
                    inserted: list[Any] = []

                    def insert(**kwargs: Any) -> None:
                        write(**kwargs)
                        if kwargs["replacement"]["phase"] == "recipient_ready" and not inserted:
                            inserted.append(self.reserve_writer(fixture, target_key=old_key))

                    if after_creation:
                        with patch.object(
                            fixture.store, "write_testing_activation", side_effect=insert
                        ):
                            result = fixture.activate()
                        self.assertTrue(inserted)
                        if result.status == "completed":
                            self.assertFalse(result.response_payload["result"]["applied"])
                    else:
                        self.reserve_writer(fixture, target_key=old_key)
                        with self.assertRaises(TestingActivationRefusal):
                            fixture.activate()
                    record = activation_record(
                        fixture.store,
                        fixture.product,
                        fixture.context,
                        scope=fixture.scope,
                        preparation_key=fixture.request.preparation_key,
                    )
                    self.assertTrue(record is None or not record.published)
                    self.assertEqual(
                        fixture.store.read_dokploy_target_id_record(
                            context_name=fixture.context, instance_name="testing"
                        ).target_id,
                        fixture.old_id,
                    )
                    self.assertFalse(
                        any(path == "/api/application.deploy" for path, _ in fixture.writes)
                    )
                    self.assertEqual(fixture.canonical_password(), "retained-only-password")
                finally:
                    fixture.doCleanups()


class DirectApplicationContinuationTests(unittest.TestCase):
    def test_direct_original_post_and_pre_image_continue_with_own_successor_receipts(self) -> None:
        for pre_image in (False, True):
            with self.subTest(pre_image=pre_image):
                fixture = ApplicationFlowTests()
                fixture.direct_original = True
                fixture.pre_image_unknown = pre_image
                fixture.setUp()
                try:
                    fixture.assert_direct_http_original_replay()
                    fixture.test_normal_lost_maintenance_native_activation_and_ordinary_successor()
                finally:
                    fixture.doCleanups()

    def test_direct_custody_requires_exact_original_receipt_bindings_and_fenced_row(self) -> None:
        from control_plane.generic_web_testing_exclusion import (
            require_retained_application_custody,
            TestingExclusionRefused,
        )
        from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
        from control_plane.workflows.generic_web_execution import EXECUTION_EVIDENCE_KEY
        from control_plane.workflows.generic_web_application_snapshot import (
            APPLICATION_LIFECYCLE_EVIDENCE_KEY,
        )

        fixture = ApplicationFlowTests()
        fixture.direct_original = True
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = fixture.original
        retained_data = (fixture.old_backing / "keep").read_bytes()
        record = fixture.normal_execution_record(original)
        read = fixture.store.read_deployment_record
        lifecycle = json.loads(
            record.post_deploy_update.evidence[APPLICATION_LIFECYCLE_EVIDENCE_KEY]
        )
        lifecycle["product"] = "foreign-product"
        variants = {
            "foreign-operation": record.model_copy(update={"record_id": "foreign-operation"}),
            "missing-receipt": record.model_copy(
                update={
                    "post_deploy_update": record.post_deploy_update.model_copy(
                        update={
                            "evidence": {
                                key: value
                                for key, value in record.post_deploy_update.evidence.items()
                                if key != EXECUTION_EVIDENCE_KEY
                            }
                        }
                    )
                }
            ),
            "malformed-receipt": record.model_copy(
                update={
                    "post_deploy_update": record.post_deploy_update.model_copy(
                        update={
                            "evidence": {
                                **record.post_deploy_update.evidence,
                                EXECUTION_EVIDENCE_KEY: "{",
                            }
                        }
                    )
                }
            ),
            "foreign-context": record.model_copy(update={"context": "foreign-context"}),
            "missing-artifact": record.model_copy(update={"artifact_identity": None}),
            "foreign-resolved-target": record.model_copy(
                update={
                    "resolved_target": record.resolved_target.model_copy(
                        update={"target_id": "foreign-target"}
                    )
                }
            ),
            "foreign-provider": record.model_copy(
                update={
                    "deployed_target": record.deployed_target.model_copy(
                        update={"provider_id": "foreign-provider"}
                    )
                }
            ),
            "foreign-runtime": record.model_copy(
                update={
                    "runtime_identity": record.runtime_identity.model_copy(
                        update={"product": "foreign-product"}
                    )
                }
            ),
            "foreign-lifecycle": record.model_copy(
                update={
                    "post_deploy_update": record.post_deploy_update.model_copy(
                        update={
                            "evidence": {
                                **record.post_deploy_update.evidence,
                                APPLICATION_LIFECYCLE_EVIDENCE_KEY: json.dumps(lifecycle),
                            }
                        }
                    )
                }
            ),
        }
        with fixture.store.testing_replacement_execution_lock(
            testing_event_execution_lock_name(original.provider_target_key)
        ) as check:
            assert check is not None

            def custody() -> Any:
                return require_retained_application_custody(
                    fixture.store, product=fixture.product, context=fixture.context, check=check
                )

            self.assertEqual(custody(), (original,))
            with (
                patch.object(
                    fixture.store,
                    "read_deployment_record",
                    side_effect=FileNotFoundError(record.record_id),
                ),
                self.assertRaises(TestingExclusionRefused),
            ):
                custody()
            for problem, variant in variants.items():
                with self.subTest(problem=problem):

                    def changed(record_id: str) -> Any:
                        return variant if record_id == record.record_id else read(record_id)

                    with (
                        patch.object(fixture.store, "read_deployment_record", side_effect=changed),
                        self.assertRaises(TestingExclusionRefused),
                    ):
                        custody()
            retried = fixture.store.retry_reconciled_mutation(
                reservation=original, lease_owner="competing-worker"
            )
            self.assertEqual(retried.status, "acquired")
            assert retried.record is not None
            # A captured old list cannot hide the actual row's changed attempt.
            with (
                patch.object(
                    fixture.store,
                    "list_held_provider_target_reservations",
                    return_value=(original,),
                ),
                self.assertRaises(TestingExclusionRefused),
            ):
                custody()
            with self.assertRaises(TestingExclusionRefused):
                custody()
            fenced = fixture.store.mark_mutation_reconcile_required(
                reservation=retried.record, reconciliation_key=retried.record.reconciliation_key
            )
            self.assertEqual(fenced.status, "updated")
            # Reconcile-required does not replace the real unexpired-lease check.
            with self.assertRaises(TestingExclusionRefused):
                custody()
        self.assertEqual(fixture.normal_execution_record(original), record)
        self.assertEqual((fixture.old_backing / "keep").read_bytes(), retained_data)


class PreImageApplicationFlowTests(unittest.TestCase):
    def test_attributable_predecessor_task_supports_unknown_pre_image_continuation(self) -> None:
        fixture = ApplicationFlowTests()
        fixture.pre_image_unknown = True
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.test_normal_lost_maintenance_native_activation_and_ordinary_successor()
