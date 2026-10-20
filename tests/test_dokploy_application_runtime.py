from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import unittest
from unittest.mock import patch

import click

from control_plane.dokploy.application_runtime import (
    ApplicationRuntimeObservation,
    application_runtime_drift_fingerprint,
    fetch_application_runtime,
)
from control_plane.dokploy.runtime_evidence import DokployEvidenceProviderError


_IMAGE_ID = "sha256:" + "b" * 64
_CONTAINER_ID = "c" * 64
_DIGEST = "sha256:" + "d" * 64
_REPOSITORY = "ghcr.io/example/application"
_IMAGE = f"{_REPOSITORY}@{_DIGEST}"
_TAG = f"{_REPOSITORY}:sha-{'a' * 40}"


class DokployApplicationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target: dict[str, object] = {
            "applicationId": "application-1",
            "appName": "fixture-app",
            "serverId": "server-1",
        }
        self.service: dict[str, object] = {
            "ID": "service-1",
            "Version": {"Index": 2},
            "Spec": {
                "Name": "fixture-app",
                "Mode": {"Replicated": {"Replicas": 1}},
                "TaskTemplate": {
                    "ContainerSpec": {
                        "Image": _TAG,
                        "Env": [
                            "LAUNCHPLANE_DEPLOYMENT_RECORD_ID=deployment-1",
                            "APP_TOKEN=do-not-expose",
                        ],
                    }
                },
            },
            "UpdateStatus": {
                "State": "completed",
                "StartedAt": "2026-10-20T03:08:20.988932973Z",
                "CompletedAt": "2026-10-20T03:08:28.963389032Z",
            },
        }
        self.tasks: list[object] = [{"containerId": "task-1", "state": "running"}]
        self.task: dict[str, object] = {
            "ID": "task-1",
            "ServiceID": "service-1",
            "NodeID": "node-1",
            "DesiredState": "running",
            "Status": {"State": "running", "ContainerStatus": {"ContainerID": _CONTAINER_ID}},
            "Spec": {"ContainerSpec": {"Image": _TAG}},
        }
        self.container: dict[str, object] = {
            "Id": _CONTAINER_ID,
            "Image": _IMAGE_ID,
            "Config": {
                "Image": _TAG,
                "Labels": {
                    "com.docker.swarm.service.id": "service-1",
                    "com.docker.swarm.service.name": "fixture-app",
                    "com.docker.swarm.task.id": "task-1",
                    "com.docker.swarm.node.id": "node-1",
                },
                "Env": ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=deployment-1", "APP_TOKEN=do-not-expose"],
            },
            "State": {
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "Health": {"Status": "healthy"},
            },
        }
        self.image: dict[str, object] = {"Id": _IMAGE_ID, "RepoDigests": [_IMAGE]}
        self.extra_inspections: dict[str, dict[str, object]] = {}
        self.requests: list[dict[str, object]] = []

    def read(self) -> ApplicationRuntimeObservation:
        def request(**kwargs: object) -> object:
            self.requests.append(kwargs)
            query = kwargs["query"]
            assert isinstance(query, dict)
            self.assertEqual(query.get("serverId"), "server-1")
            if kwargs["path"] == "/api/docker.getServiceContainersByAppName":
                self.assertEqual(query["appName"], "fixture-app")
                return deepcopy(self.tasks)
            self.assertEqual(kwargs["path"], "/api/docker.getConfig")
            identifier = query["containerId"]
            return deepcopy(
                {
                    "fixture-app": self.service,
                    "task-1": self.task,
                    _CONTAINER_ID: self.container,
                    _IMAGE_ID.removeprefix("sha256:"): self.image,
                    **self.extra_inspections,
                }[identifier]
            )

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload", return_value=self.target
            ),
            patch("control_plane.dokploy.api.dokploy_request", side_effect=request),
        ):
            return fetch_application_runtime(
                host="https://provider.example",
                token="fixture-token",
                application_id="application-1",
            )

    def test_exact_convergence_proves_actual_image_digest_and_exposes_no_values(self) -> None:
        observed = self.read()
        assert isinstance(observed, ApplicationRuntimeObservation)
        self.assertTrue(observed.ready)
        self.assertEqual(observed.running_container_images, (_IMAGE,))
        self.assertEqual(observed.running_container_deployment_record_ids, ("deployment-1",))
        self.assertIn("APP_TOKEN", observed.service_environment_keys)
        self.assertNotIn("do-not-expose", repr(observed))
        self.assertNotIn("do-not-expose", str(asdict(observed)))
        self.assertEqual(
            self.requests[-1]["query"], {"serverId": "server-1", "containerId": "fixture-app"}
        )

    def test_healthy_old_task_is_not_the_services_current_deployment(self) -> None:
        config = self.container["Config"]
        assert isinstance(config, dict)
        config["Env"] = ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=old-deployment"]
        observed = self.read()
        self.assertFalse(getattr(observed, "ready"))
        self.assertEqual(
            getattr(observed, "running_container_deployment_record_ids"), ("old-deployment",)
        )

    def test_shutdown_desired_task_still_running_keeps_rollout_unready(self) -> None:
        old_id = "e" * 64
        task = deepcopy(self.task)
        task.update(ID="old-task", DesiredState="shutdown")
        task["Status"] = {"State": "running", "ContainerStatus": {"ContainerID": old_id}}
        container = deepcopy(self.container)
        container["Id"] = old_id
        config = container["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels["com.docker.swarm.task.id"] = "old-task"
        self.tasks.append({"containerId": "old-task"})
        self.extra_inspections.update({"old-task": task, old_id: container})
        observed = self.read()
        self.assertFalse(observed.ready)
        self.assertEqual(len(observed.running_container_images), 2)

    def test_transient_new_task_before_completed_rollout_does_not_qualify(self) -> None:
        rollout = self.service["UpdateStatus"]
        assert isinstance(rollout, dict)
        for state in (
            "updating",
            "paused",
            "rollback_started",
            "rollback_paused",
            "rollback_completed",
        ):
            with self.subTest(state=state):
                rollout["State"] = state
                self.assertFalse(getattr(self.read(), "ready"))

    def test_pending_task_or_unhealthy_container_does_not_qualify(self) -> None:
        status = self.task["Status"]
        assert isinstance(status, dict)
        for state in ("starting", "failed", "rejected"):
            with self.subTest(task_state=state):
                status["State"] = state
                observation = self.read()
                self.assertFalse(getattr(observation, "ready"))
                self.assertEqual(getattr(observation, "pending_task_count"), 1)
        status["State"] = "running"
        container_state = self.container["State"]
        assert isinstance(container_state, dict)
        for state in ("starting", "unhealthy"):
            container_state["Health"] = {"Status": state}
            self.assertFalse(getattr(self.read(), "ready"))

    def test_malformed_or_foreign_evidence_is_unknown(self) -> None:
        cases: tuple[tuple[str, str, object], ...] = (
            ("target", "applicationId", "other-application"),
            ("target", "serverId", {}),
            ("service", "ID", "bad id"),
            ("service", "Version", {}),
            ("service", "Spec", {}),
            ("service", "UpdateStatus", {}),
            ("service", "UpdateStatus", {"State": "completed"}),
            ("task", "ID", "foreign-task"),
            ("task", "ServiceID", "foreign-service"),
            ("task", "NodeID", "foreign-node"),
            ("task", "Status", {"State": {}}),
            ("container", "Id", "e" * 64),
            ("container", "Config", {}),
            ("container", "State", {"Running": "true"}),
            ("container", "Image", "not-image-id"),
            ("image", "Id", "sha256:" + "e" * 64),
            ("image", "RepoDigests", ["ghcr.io/other/application@" + _DIGEST]),
            ("image", "RepoDigests", [_IMAGE, f"{_REPOSITORY}@sha256:{'e' * 64}"]),
        )
        for name, key, value in cases:
            with self.subTest(name=name, key=key, value=value):
                mapping = getattr(self, name)
                original = deepcopy(mapping)
                mapping[key] = value
                try:
                    with self.assertRaises(DokployEvidenceProviderError):
                        self.read()
                finally:
                    mapping.clear()
                    mapping.update(original)

    def test_pinned_task_image_must_agree_with_inspected_image(self) -> None:
        self.task["Spec"] = {"ContainerSpec": {"Image": f"{_REPOSITORY}@sha256:{'f' * 64}"}}
        with self.assertRaises(DokployEvidenceProviderError):
            self.read()

    def test_all_container_membership_labels_must_match(self) -> None:
        config = self.container["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        for key in tuple(labels):
            old = labels[key]
            labels[key] = "foreign"
            try:
                with self.subTest(label=key), self.assertRaises(DokployEvidenceProviderError):
                    self.read()
            finally:
                labels[key] = old

    def test_empty_or_duplicate_task_inventory_cannot_prove_absence(self) -> None:
        cases: tuple[list[object], ...] = ([], [None], [self.tasks[0], self.tasks[0]])
        for tasks in cases:
            with self.subTest(tasks=tasks):
                self.tasks = tasks
                with self.assertRaises(DokployEvidenceProviderError):
                    self.read()

    def test_provider_errors_are_bounded(self) -> None:
        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=click.ClickException("provider secret detail"),
            ),
            self.assertRaises(DokployEvidenceProviderError) as caught,
        ):
            fetch_application_runtime(
                host="fixture", token="fixture", application_id="application-1"
            )
        self.assertNotIn("secret", str(caught.exception))

    def test_same_stable_drift_keeps_one_repair_identity_despite_task_churn(self) -> None:
        observed = self.read()

        def fingerprint(runtime: ApplicationRuntimeObservation) -> str:
            return application_runtime_drift_fingerprint(
                runtime,
                artifact_reference=_IMAGE,
                provider_reference=_TAG,
                deployment_record_id="new-deployment",
            )

        first = fingerprint(observed)
        self.assertTrue(first)
        churned = replace(
            observed, service_version=9, running_container_images=(), pending_task_count=1
        )
        self.assertEqual(fingerprint(churned), first)
        self.assertFalse(fingerprint(replace(observed, rollout_state="updating")))

    def test_completion_waits_for_rollout_and_recovery_does_not_credit_pending(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
            GenericWebProviderDeploymentObservation,
            qualify_testing_application_observation,
        )
        from tests.test_generic_web_deploy_recovery import _generic_web_recovery_target

        observed = self.read()
        target = _generic_web_recovery_target().model_copy(
            update={"testing_runtime_digest": "fixture-digest"}
        )
        target.ship_request.artifact_id = _IMAGE
        target.ship_request.deploy_reference = _TAG
        provider = DokployGenericWebDeployProvider()
        job = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="done",
            deployment_id="job-1",
            started_at="2026-10-20T03:08:20Z",
            finished_at="2026-10-20T03:08:25Z",
        )

        def qualify(*, wait: bool = False) -> GenericWebProviderDeploymentObservation:
            return qualify_testing_application_observation(
                provider=provider,
                control_plane_root=Path("."),
                target=target,
                observation=job,
                deployment_record_id="deployment-1",
                wait=wait,
            )

        pending = replace(observed, rollout_state="updating", rollout_completed_at="")
        with patch.object(provider, "observe_testing_application_runtime", return_value=pending):
            self.assertEqual(qualify().outcome, "unknown")
        with (
            patch.object(
                provider, "observe_testing_application_runtime", side_effect=[pending, observed]
            ),
            patch("control_plane.workflows.generic_web_deploy_provider.sleep"),
        ):
            self.assertEqual(qualify(wait=True), job)
        rollback = replace(observed, rollout_state="rollback_completed")
        with patch.object(provider, "observe_testing_application_runtime", return_value=rollback):
            failed = qualify()
        self.assertEqual(failed.deployment_status, "unhealthy")
        self.assertIn("provider job status=done", failed.error_message)
        with patch.object(
            provider,
            "observe_testing_application_runtime",
            return_value=replace(
                rollback,
                rollout_started_at="2026-10-19T01:00:00Z",
            ),
        ):
            self.assertEqual(qualify().outcome, "unknown")
