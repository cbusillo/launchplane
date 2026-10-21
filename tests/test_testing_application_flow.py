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
from typing import Any
from typing import cast
import unittest
from unittest.mock import patch

from control_plane import secrets
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
from control_plane.provider_operations import run_durable_provider_operation
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
    adapter: ApplicationActivationAdapter
    planned: TestingActivationRecord

    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "application-flow-fixture-root"},
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

        self.actor = launchplane_identity_actor(
            LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
        )
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
        self.services: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.images: dict[str, str] = {}
        self.writes: list[tuple[str, Any]] = []
        self.deployments: dict[str, list[dict[str, Any]]] = {}
        self.state_containers: dict[str, dict[str, Any]] = {}
        self.state_roles: set[str] = set()
        self.state_databases: dict[str, str] = {}
        self.state_sql: list[str] = []
        self.state_alias_changed = False
        self.state_duplicate_alias = False
        from tests.test_odoo_testing_resources import TestingResourceProgramTests

        self.storage = TestingResourceProgramTests()
        self.storage.setUp()
        self.addCleanup(self.storage.doCleanups)
        self.old_backing = self.root / "retained-data"
        self.old_backing.mkdir()
        (self.old_backing / "keep").write_text("Retained original data")
        self.payloads[self.old_id]["mounts"] = [
            {
                "type": "volume",
                "volumeName": "retained-state",
                "mountPath": "/var/lib/example",
                "serviceType": "application",
                "serviceId": self.old_id,
            }
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
        self.enterContext(
            patch(
                "control_plane.dokploy.api.wait_for_target_deployment",
                side_effect=lambda **kw: self.deployments[kw["target_id"]][-1]["deploymentId"],
            )
        )
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
        self.lose_command = True
        with (
            self.assertRaises(OSError),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-3600)
            ),
        ):
            self.normal("original")
        self.lose_command = False
        self.original = next(
            row
            for row in self.store.list_held_provider_target_reservations()
            if row.route_path == GENERIC_WEB_DEPLOY_ROUTE
        )
        self.assertEqual(
            self.original.provider_effect_phase,
            "application_schedule_quiesce"
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
            "sourceType": None,
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
            image_reference="ghcr.io/example/site@sha256:" + "a" * 64,
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
            recorded_artifact=True,
            retain_event_request=True,
        )
        return run_durable_provider_operation(
            store=self.store,
            scope=reconcile_reservation_scope(self.product),
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key=f"{RECONCILE_SOURCE}:{self.product}:{self.context}:testing:{suffix}",
            request_fingerprint=hashlib.sha256(suffix.encode()).hexdigest(),
            lease_owner="normal-worker",
            response_trace_id="normal-" + suffix,
            adapter=adapter,
        )

    def command(self, **kwargs: Any) -> None:
        kwargs["before_trigger"]()
        if self.lose_command:
            raise OSError("Lost the original local-command execution reply")

    def install_runtime(self, payload: dict[str, Any], identity: dict[str, Any]) -> None:
        name = payload["appName"]
        service, task, container = name + "-service", name + "-task", name + "-container"
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
            "Spec": {
                "Name": name,
                "Mode": {"Replicated": {"Replicas": 1}},
                "TaskTemplate": {"Networks": [{"Target": "fixture-network-id", "Aliases": [name]}]},
            },
            "Endpoint": {},
        }
        self.tasks[task] = {
            "ID": task,
            "ServiceID": service,
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
                "Env": values,
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
                return []
            raise AssertionError(kwargs)
        self.writes.append((path, deepcopy(body)))
        if path == "/api/schedule.create":
            identifier = "schedule-" + str(len(self.schedules))
            self.schedules[identifier] = {**body, "scheduleId": identifier}
            return self.schedules[identifier]
        if path == "/api/schedule.update":
            self.schedules[body["scheduleId"]].update(body)
            if self.pre_image_unknown and self.lose_command and body.get("enabled") is False:
                raise OSError("Lost original schedule quiesce reply before image dispatch")
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
        if path == "/api/compose.create":
            identifier = "new-state-" + str(len(self.projects))
            project = self.projects[-1]
            environment = project["environments"][-1]
            self.payloads[identifier] = {
                **body,
                "composeId": identifier,
                "mounts": [],
                "domains": [],
                "deployments": [],
                "composeStatus": "idle",
            }
            environment["compose"].append(
                {"composeId": identifier, "name": body["name"], "appName": body["appName"]}
            )
            return deepcopy(self.payloads[identifier])
        if path == "/api/compose.update":
            self.payloads[body["composeId"]].update(body)
            return {}
        if path == "/api/compose.fetchSourceType":
            from control_plane.testing_activation_compose import interpolate
            from control_plane.dokploy.api import parse_dokploy_env_text

            payload = self.payloads[body["composeId"]]
            payload["converted"] = json.dumps(
                interpolate(
                    json.loads(payload["composeFile"]), parse_dokploy_env_text(payload["env"])
                )
            )
            return {}
        if path == "/api/mounts.create":
            self.payloads[body["serviceId"]]["mounts"].append({**body, "mountId": "new-mount"})
            return {}
        if path in {
            "/api/application.update",
            "/api/application.saveDockerProvider",
            "/api/application.saveEnvironment",
        }:
            self.payloads[body["applicationId"]].update(body)
            return {}
        if path in {"/api/application.deploy", "/api/compose.deploy"}:
            identifier = body.get("applicationId", body.get("composeId"))
            payload = self.payloads[identifier]
            from control_plane.dokploy.api import parse_dokploy_env_text

            if "applicationId" in body:
                identity = json.loads(
                    parse_dokploy_env_text(payload["env"])["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"]
                )
                self.install_runtime(payload, identity)
            else:
                self.install_state(payload)
            row = {
                "deploymentId": "activation-image-" + str(len(self.projects)),
                "title": body["title"],
                "status": "done",
                "startedAt": self.old.now(-1),
                "finishedAt": self.old.now(),
                "applicationId" if "applicationId" in body else "composeId": identifier,
            }
            self.deployments.setdefault(identifier, []).append(row)
            payload["deployments"] = deepcopy(self.deployments[identifier])
            payload["applicationStatus" if "applicationId" in body else "composeStatus"] = "done"
            if self.lost_image_reply:
                self.lost_image_reply = False
                raise OSError("Lost the original fresh image deployment reply")
            return {}
        raise AssertionError(kwargs)

    def install_state(self, payload: dict[str, Any]) -> None:
        document = json.loads(payload["converted"])
        service = document["services"]["postgres"]
        volume = document["volumes"]["database"]["name"]
        network = document["networks"]["provider"]["name"]
        container = payload["appName"] + "-postgres"
        self.state_containers[container] = {
            "Id": container,
            "Config": {
                "Image": service["image"],
                "Env": [key + "=" + value for key, value in service["environment"].items()],
            },
            "State": {"Running": True},
            "HostConfig": {"PortBindings": {}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": volume,
                    "Destination": service["environment"]["PGDATA"],
                    "Source": str(self.storage.volumes[volume]["Mountpoint"]),
                }
            ],
            "NetworkSettings": {
                "Networks": {
                    network: {
                        "IPAddress": "10.0.0.80",
                        "NetworkID": "fixture-network-id",
                        "Aliases": service["networks"]["provider"]["aliases"],
                    }
                }
            },
        }

    def state_docker(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = arguments[1:]
        state = next(iter(self.state_containers.values()))
        environment = dict(row.split("=", 1) for row in state["Config"]["Env"])
        output: Any = ""
        if args[:2] == ["ps", "-aq"]:
            output = "\n".join(
                self.state_containers
                if "--filter" in args
                else {**self.state_containers, **self.containers}
            )
        elif args[:2] == ["volume", "inspect"]:
            output = [self.storage.volumes[args[2]]]
        elif args[0] == "inspect" and args[1] in self.state_containers:
            output = [deepcopy(state)]
            if self.state_alias_changed:
                output[0]["NetworkSettings"]["Networks"]["dokploy-network"]["Aliases"] = []
        elif args[:2] == ["inspect", "--format"]:
            container = self.containers[args[-1]]
            networks = deepcopy(container["NetworkSettings"]["Networks"])
            if self.state_duplicate_alias:
                networks["dokploy-network"]["Aliases"] = state["NetworkSettings"]["Networks"][
                    "dokploy-network"
                ]["Aliases"]
            output = {"mounts": container["Mounts"], "networks": networks}
        elif args[:3] == ["service", "ls", "-q"]:
            output = "\n".join(self.services)
        elif args[:3] == ["service", "inspect", "--format"]:
            service = self.services[args[-1]]["Spec"]
            output = {"name": service["Name"], "networks": service["TaskTemplate"]["Networks"]}
        elif args[0] == "exec":
            password = next(
                item.removeprefix("PGPASSWORD=") for item in args if item.startswith("PGPASSWORD=")
            )
            user = args[args.index("-U") + 1]
            application = user == environment["LP_APPLICATION_ROLE"]
            expected = environment[
                "LP_APPLICATION_PASSWORD" if application else "POSTGRES_PASSWORD"
            ]
            if password != expected:
                return subprocess.CompletedProcess(
                    arguments, 2, "", "password authentication failed"
                )
            statement = kwargs["input"]
            self.state_sql.append(statement)
            role = environment["LP_APPLICATION_ROLE"]
            database = environment["LP_APPLICATION_DATABASE"]
            if statement.startswith("CREATE ROLE "):
                self.state_roles.add(role)
            elif statement.startswith("CREATE DATABASE "):
                self.state_databases[database] = role
            elif "rolcanlogin,rolsuper" in statement:
                output = "t|f|f|f|f|f" if role in self.state_roles else ""
            elif "pg_get_userbyid" in statement:
                output = self.state_databases.get(database, "")
            elif "current_user ||" in statement:
                self.assertEqual(
                    args[args.index("-h") + 1],
                    state["NetworkSettings"]["Networks"]["dokploy-network"]["Aliases"][0],
                )
                output = role + "|" + database + "|10.0.0.80"
            elif "rolname FROM pg_roles" in statement:
                output = "\n".join(sorted([environment["POSTGRES_USER"], *self.state_roles]))
            elif "datname FROM pg_database" not in statement:
                raise AssertionError(statement)
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, output if isinstance(output, str) else json.dumps(output), ""
        )

    def docker(self, arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = arguments[1:]
        if args[:2] == ["service", "inspect"]:
            value: Any = [self.services[args[2]]]
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
            value = [{"Id": args[2], "RepoDigests": [self.images[args[2]]]}]
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
        elif args[:2] == ["exec", "proxy"]:
            from urllib.parse import urlsplit

            address = urlsplit(args[-1]).hostname
            container = next(
                row
                for row in self.containers.values()
                if any(
                    network.get("IPAddress") == address
                    for network in row["NetworkSettings"]["Networks"].values()
                )
            )
            env = dict(value.split("=", 1) for value in container["Config"]["Env"])
            value = {"runtime_identity": json.loads(env["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])}
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, value if isinstance(value, str) else json.dumps(value), ""
        )

    def host_schedule(self, **kwargs: Any) -> dict[str, str]:
        kwargs["before_provider_mutation"]("host-capability-proof")
        script = kwargs["schedule_payload"]["script"]
        encoded = script.split("python3 - ", 1)[1].split(" ", 1)[0]
        spec = json.loads(base64.b64decode(encoded))
        from control_plane.testing_application_capabilities import (
            retained_application_capability_program,
        )

        state = "LP_FRESH_POSTGRES" in script
        if not state:
            self.assertEqual(script, retained_application_capability_program(spec))
        output = StringIO()
        delimiter = "LP_FRESH_POSTGRES" if state else "LP_RETAINED_CAPABILITY"
        header = script.split("<<'" + delimiter + "'\n", 1)[1].rsplit("\n" + delimiter, 1)[0]
        with (
            patch.object(sys, "argv", ["program", encoded]),
            patch.object(
                subprocess, "run", side_effect=self.state_docker if state else self.docker
            ),
            patch("sys.stdout", output),
        ):
            try:
                exec(header, {})
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
            exec(kwargs["program"], {})
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

    def test_normal_lost_maintenance_native_activation_and_ordinary_successor(self) -> None:
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
        receipt = recorded_application_lifecycle(
            self.store.read_deployment_record(inventory.deployment_record_id)
        )
        assert receipt is not None
        self.assertEqual(receipt.target_id, record.target_id)
        self.assertFalse(receipt.resume_from_hold)
        self.assertFalse(receipt.previous_artifact_id)
        self.assertFalse(receipt.previous_source_git_ref)
        self.assertTrue(all(value is None for value in receipt.originals.values()))
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

    def use_postgres(self) -> None:
        self.specification = self.specification.model_copy(
            update={
                "state": TestingApplicationState(
                    kind="postgresql",
                    container_path="/var/lib/postgresql/data",
                    image_reference="example/postgres@sha256:" + "c" * 64,
                    connection_binding="DATABASE_URL",
                    port=5432,
                )
            }
        )

    def test_postgres_state_native_flow_publishes_only_application_credentials(self) -> None:
        from control_plane.runtime_environments import resolve_site_runtime_environment_from_store
        from control_plane.testing_application_credentials import staged_runtime_values
        from urllib.parse import urlsplit, unquote

        self.use_postgres()
        self.prepare()
        publish = self.adapter.publish_application

        def publication(record: Any, checkpoint: Any) -> Any:
            before = resolve_site_runtime_environment_from_store(
                record_store=self.store,
                context_name=self.context,
                instance_name="testing",
                target_environment={},
            ).values
            self.assertNotIn("DATABASE_URL", before)
            self.assertEqual(before["DB_PASSWORD"], "retained-only-password")
            staged = staged_runtime_values(self.store, record)
            connection = urlsplit(staged["DATABASE_URL"])
            state = next(iter(self.state_containers.values()))
            values = dict(value.split("=", 1) for value in state["Config"]["Env"])
            self.assertEqual(connection.username, values["LP_APPLICATION_ROLE"])
            self.assertEqual(unquote(connection.password or ""), values["LP_APPLICATION_PASSWORD"])
            self.assertNotEqual(connection.username, values["POSTGRES_USER"])
            self.assertNotEqual(unquote(connection.password or ""), values["POSTGRES_PASSWORD"])
            fresh = self.payloads[record.target_id]
            from control_plane.dokploy.api import parse_dokploy_env_text

            delivered = parse_dokploy_env_text(fresh["env"])
            self.assertEqual(delivered["DATABASE_URL"], staged["DATABASE_URL"])
            self.assertTrue(
                {
                    "POSTGRES_PASSWORD",
                    "LP_STATE_BOOTSTRAP_PASSWORD",
                    "LP_APPLICATION_PASSWORD",
                }.isdisjoint(delivered)
            )
            self.assertEqual(fresh["mounts"], [])
            result = publish(record, checkpoint)
            after = resolve_site_runtime_environment_from_store(
                record_store=self.store,
                context_name=self.context,
                instance_name="testing",
                target_environment={},
            ).values
            self.assertEqual(after["DATABASE_URL"], staged["DATABASE_URL"])
            return result

        with patch.object(self.adapter, "publish_application", side_effect=publication):
            result = self.activate()
        self.assertEqual(result.status, "completed", result)
        self.assertTrue(any(statement.startswith("CREATE ROLE ") for statement in self.state_sql))
        self.assertTrue(
            any(statement.startswith("CREATE DATABASE ") for statement in self.state_sql)
        )
        before = len(self.state_sql)
        self.assertEqual(self.activate().status, "replayed")
        self.assertEqual(len(self.state_sql), before)
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=self.original.scope,
                route_path=self.original.route_path,
                idempotency_key=self.original.idempotency_key,
            ),
            self.original,
        )

    def test_postgres_missing_consumer_alias_refuses_before_publication(self) -> None:
        self.use_postgres()
        self.prepare()
        self.state_alias_changed = True
        result = self.activate()
        self.assertEqual(result.status, "reconcile_required", result)
        self.assertFalse(self.state_sql)
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertFalse(self.adapter.current().published)

    def test_postgres_foreign_consumer_alias_refuses_before_publication(self) -> None:
        self.use_postgres()
        self.prepare()
        self.state_duplicate_alias = True
        result = self.activate()
        self.assertEqual(result.status, "reconcile_required", result)
        self.assertFalse(self.state_sql)
        self.assertEqual(self.canonical_password(), "retained-only-password")
        self.assertFalse(self.adapter.current().published)

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
        self.assertEqual(result.status, "reconcile_required")
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
        self.assertEqual(result.status, "reconcile_required")
        self.assertFalse(self.adapter.current().published)
        self.assertFalse(self.projects)
        self.assertEqual(
            second.read_disposable_lane_safety(self.product, self.context, "testing"), observed[0]
        )
        with self.assertRaises(TestingActivationRefusal):
            self.adapter.require_unchanged(self.adapter.current())
        self.assertEqual(self.canonical_password(), "retained-only-password")

    def assert_supported_http_activation(self, *, native_apply: bool) -> None:
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
        self.assertTrue(receipt.json()["result"]["separation_verified"])
        self.assertNotIn("retained-only-password", receipt.text)

    def test_supported_http_dry_run_and_original_receipt(self) -> None:
        self.assert_supported_http_activation(native_apply=False)

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
            if arguments[1:4] == ["service", "ls", "-q"]:
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout="\n".join(row["ID"] for row in self.services.values()).encode(),
                    stderr=b"",
                )
            if arguments[1:4] == ["service", "inspect", "--format"]:
                service = next(row for row in self.services.values() if row["ID"] == arguments[-1])
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
            if arguments[1:3] in (["service", "inspect"], ["service", "ps"]) or arguments[1:4] == [
                "inspect",
                "--type",
                "task",
            ]:
                result = self.docker(arguments, **kwargs)
                return subprocess.CompletedProcess(
                    arguments,
                    result.returncode,
                    stdout=result.stdout.encode(),
                    stderr=result.stderr.encode(),
                )
            return native(arguments, **kwargs)

        self.enterContext(patch.object(routing, "command", side_effect=command))
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
            record = self.adapter.current()
            assert record.application_readiness is not None
            return Health(
                json.dumps(
                    {
                        "runtime_identity": record.application_readiness.runtime_identity.model_dump(
                            mode="json"
                        )
                    }
                ).encode()
            )

        opener.open.side_effect = health
        self.enterContext(patch("urllib.request.build_opener", return_value=opener))
        return routing

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
        routing = self.addressed()
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
        first = self.activate()
        self.assertEqual(first.status, "reconcile_required", first)
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
        with (
            self.assertRaises(OSError),
            patch.object(
                self.store, "_database_mutation_timestamp", return_value=self.old.now(-3600)
            ),
        ):
            self.normal("second-original")
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


class PreImageApplicationFlowTests(unittest.TestCase):
    def test_attributable_predecessor_task_supports_unknown_pre_image_continuation(self) -> None:
        fixture = ApplicationFlowTests()
        fixture.pre_image_unknown = True
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.test_normal_lost_maintenance_native_activation_and_ordinary_successor()
