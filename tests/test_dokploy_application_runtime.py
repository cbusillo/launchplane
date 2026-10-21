from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import json
import unittest
from unittest.mock import patch

import click

from control_plane.dokploy.application_runtime import (
    ApplicationRuntimeObservation,
    ApplicationTaskObservation,
    application_runtime_drift_fingerprint,
    application_runtime_matches,
    fetch_application_runtime,
)
from control_plane.dokploy.runtime_evidence import DokployEvidenceProviderError


from control_plane.contracts.ship_request import ShipRequest
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebResolvedDeployTarget,
)


_IMAGE_ID = "sha256:" + "b" * 64
_CONTAINER_ID = "c" * 64
_DIGEST = "sha256:" + "d" * 64
_REPOSITORY = "ghcr.io/example/application"
_IMAGE = f"{_REPOSITORY}@{_DIGEST}"
_TAG = f"{_REPOSITORY}:sha-{'a' * 40}"


def _application_target() -> GenericWebResolvedDeployTarget:
    return GenericWebResolvedDeployTarget(
        ship_request=ShipRequest(
            artifact_id=_IMAGE,
            deploy_reference=_TAG,
            context="fixture",
            instance="testing",
            source_git_ref="a" * 40,
            target_name="fixture-app",
            target_type="application",
            provider_id="dokploy",
            target_category="application",
            provider_target_type="application",
            deploy_mode="application",
        ),
        resolved_target=ResolvedTargetEvidence(
            target_type="application",
            target_id="application-1",
            target_name="fixture-app",
        ),
        deploy_timeout_seconds=10,
    )


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
        self.inspect_shape = "object"
        self.final_target: dict[str, object] | None = None
        self.vanished_tasks: set[str] = set()

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
            if identifier in self.vanished_tasks:
                raise click.ClickException("fixture task reaped")
            result = deepcopy(
                {
                    "fixture-app": self.service,
                    "task-1": self.task,
                    _CONTAINER_ID: self.container,
                    _IMAGE_ID.removeprefix("sha256:"): self.image,
                    **self.extra_inspections,
                }[identifier]
            )
            if self.inspect_shape == "json":
                return json.dumps(result)
            if self.inspect_shape == "list":
                return [result]
            return result

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=[self.target, self.final_target or self.target],
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
        self.assertEqual(observed.running_container_images, ((_IMAGE,),))
        self.assertEqual(observed.running_container_deployment_record_ids, ("deployment-1",))
        self.assertNotIn("do-not-expose", repr(observed))
        self.assertNotIn("do-not-expose", str(asdict(observed)))

        for repository, familiar in (
            ("docker.io/library/nginx", "nginx"),
            ("docker.io/acme/web", "acme/web"),
            ("index.docker.io/acme/web", "acme/web"),
            ("registry.example:5000/team/app", "registry.example:5000/team/app"),
        ):
            with self.subTest(repository=repository):
                canonical_image = f"{repository}@{_DIGEST}"
                provider_tag = f"{repository}:sha-build"
                spec = self.service["Spec"]
                assert isinstance(spec, dict)
                template = spec["TaskTemplate"]
                assert isinstance(template, dict)
                container_spec = template["ContainerSpec"]
                assert isinstance(container_spec, dict)
                container_spec["Image"] = provider_tag
                self.task["Spec"] = {"ContainerSpec": {"Image": provider_tag}}
                config = self.container["Config"]
                assert isinstance(config, dict)
                config["Image"] = f"{familiar}:sha-build"
                self.image["RepoDigests"] = [canonical_image, f"{familiar}@{_DIGEST}"]
                observed = self.read()
                self.assertTrue(observed.task_evidence_complete)
                for expected in (canonical_image, f"{familiar}@{_DIGEST}"):
                    for tag in (provider_tag, f"{familiar}:sha-build"):
                        self.assertTrue(
                            application_runtime_matches(
                                observed,
                                artifact_reference=expected,
                                provider_reference=tag,
                                deployment_record_id="deployment-1",
                            )
                        )
                        self.assertFalse(
                            application_runtime_drift_fingerprint(
                                observed,
                                artifact_reference=expected,
                                provider_reference=tag,
                                deployment_record_id="deployment-1",
                            )
                        )
                for expected, record_id in (
                    (f"{repository}@sha256:{'e' * 64}", "deployment-1"),
                    (f"docker.io/other/web@{_DIGEST}", "deployment-1"),
                    (canonical_image, "other-deployment"),
                ):
                    self.assertFalse(
                        application_runtime_matches(
                            observed,
                            artifact_reference=expected,
                            provider_reference=provider_tag,
                            deployment_record_id=record_id,
                        )
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

    def test_provider_inspect_response_shapes_share_the_supported_contract(self) -> None:
        for shape in ("object", "json", "list"):
            with self.subTest(shape=shape):
                self.inspect_shape = shape
                self.assertTrue(self.read().ready)

    def test_target_placement_must_still_match_after_runtime_inspection(self) -> None:
        for key in ("applicationId", "appName", "serverId"):
            with self.subTest(key=key):
                self.final_target = {**self.target, key: "other-target"}
                with self.assertRaises(DokployEvidenceProviderError):
                    self.read()

    def test_reaped_history_preserves_stable_drift_without_proving_convergence(self) -> None:
        self.tasks.append({"containerId": "old-task"})
        self.vanished_tasks.add("old-task")
        observed = self.read()
        self.assertFalse(observed.task_evidence_complete)
        self.assertFalse(observed.ready)
        self.assertTrue(
            application_runtime_drift_fingerprint(
                observed,
                artifact_reference=_IMAGE,
                provider_reference=_TAG,
                deployment_record_id="next-deployment",
            )
        )
        self.assertFalse(
            application_runtime_matches(
                observed,
                artifact_reference=_IMAGE,
                provider_reference=_TAG,
                deployment_record_id="deployment-1",
            )
        )

    def test_completed_rollback_can_match_its_healthy_original_baseline(self) -> None:
        rollout = self.service["UpdateStatus"]
        assert isinstance(rollout, dict)
        rollout["State"] = "rollback_completed"
        observed = self.read()
        self.assertTrue(
            application_runtime_matches(
                observed,
                artifact_reference=_IMAGE,
                provider_reference=_TAG,
                deployment_record_id="deployment-1",
            )
        )
        self.assertFalse(
            application_runtime_matches(
                observed,
                artifact_reference=_IMAGE,
                provider_reference=_TAG,
                deployment_record_id="failed-new-deployment",
            )
        )

    def test_multiple_repository_digests_qualify_only_expected_members(self) -> None:
        other = f"{_REPOSITORY}@sha256:{'e' * 64}"
        self.image["RepoDigests"] = [other, _IMAGE]
        observed = self.read()
        self.assertTrue(
            application_runtime_matches(
                observed,
                artifact_reference=_IMAGE,
                provider_reference=_TAG,
                deployment_record_id="deployment-1",
            )
        )
        self.image["RepoDigests"] = [other]
        self.assertFalse(
            application_runtime_matches(
                self.read(),
                artifact_reference=_IMAGE,
                provider_reference=_TAG,
                deployment_record_id="deployment-1",
            )
        )

    def test_only_reserved_environment_identity_is_validated(self) -> None:
        config = self.container["Config"]
        assert isinstance(config, dict)
        environment = [
            "LAUNCHPLANE_DEPLOYMENT_RECORD_ID=deployment-1",
            "dotted.key=fixture",
            "dotted.key=other",
            "no-value",
            None,
        ]
        config["Env"] = environment
        self.assertTrue(self.read().ready)
        environment.append("LAUNCHPLANE_DEPLOYMENT_RECORD_ID=deployment-1")
        self.assertFalse(self.read().task_evidence_complete)

    def test_shutdown_desired_task_still_running_keeps_rollout_unready(self) -> None:
        self.task["DesiredState"] = "shutdown"
        self.assertFalse(self.read().ready)
        self.task["DesiredState"] = "running"
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

    def test_ready_replacement_keeps_completed_service_unready_until_running(self) -> None:
        replacement = deepcopy(self.task)
        replacement.update(ID="replacement-task", DesiredState="ready")
        self.tasks.append({"containerId": "replacement-task"})
        self.extra_inspections["replacement-task"] = replacement
        for state in ("new", "ready"):
            with self.subTest(state=state):
                replacement["Status"] = {"State": state}
                self.assertFalse(self.read().ready)
        replacement.update(DesiredState="shutdown", Status={"State": "shutdown"})
        self.assertTrue(self.read().ready)

    def test_transient_new_task_before_completed_rollout_does_not_qualify(self) -> None:
        rollout = self.service["UpdateStatus"]
        assert isinstance(rollout, dict)
        for state in (
            "updating",
            "paused",
            "rollback_started",
            "rollback_paused",
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
        )
        for name, key, value in cases:
            with self.subTest(name=name, key=key, value=value):
                mapping = getattr(self, name)
                original = deepcopy(mapping)
                mapping[key] = value
                try:
                    if name in {"task", "container", "image"}:
                        observation = self.read()
                        self.assertFalse(observation.task_evidence_complete)
                        self.assertFalse(observation.ready)
                    else:
                        with self.assertRaises(DokployEvidenceProviderError):
                            self.read()
                finally:
                    mapping.clear()
                    mapping.update(original)

    def test_pinned_task_image_must_agree_with_inspected_image(self) -> None:
        self.task["Spec"] = {"ContainerSpec": {"Image": f"{_REPOSITORY}@sha256:{'f' * 64}"}}
        self.assertFalse(self.read().task_evidence_complete)

    def test_all_container_membership_labels_must_match(self) -> None:
        config = self.container["Config"]
        assert isinstance(config, dict)
        labels = config["Labels"]
        assert isinstance(labels, dict)
        for key in tuple(labels):
            old = labels[key]
            labels[key] = "foreign"
            try:
                with self.subTest(label=key):
                    self.assertFalse(self.read().task_evidence_complete)
            finally:
                labels[key] = old

    def test_empty_or_duplicate_task_inventory_cannot_prove_absence(self) -> None:
        cases: tuple[list[object], ...] = ([], [None], [self.tasks[0], self.tasks[0]])
        for tasks in cases:
            with self.subTest(tasks=tasks):
                self.tasks = tasks
                self.assertFalse(self.read().task_evidence_complete)

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

    def test_only_stable_service_drift_permits_repair(self) -> None:
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
        self.assertFalse(fingerprint(replace(observed, rollout_state="updating")))
        self.assertFalse(fingerprint(replace(observed, rollout_state="")))
        self.service.pop("UpdateStatus")
        self.assertTrue(fingerprint(self.read()))
        self.service["PreviousSpec"] = deepcopy(self.service["Spec"])
        self.assertFalse(fingerprint(self.read()))

    def test_terminal_failed_job_settles_old_service_without_crediting_success(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
            GenericWebProviderDeploymentObservation,
            qualify_testing_application_observation,
        )

        observed = replace(
            self.read(),
            service_image="ghcr.io/example/application:old",
            service_deployment_record_id="old-deployment",
            rollout_started_at="2026-01-01T00:00:00Z",
        )
        provider = DokployGenericWebDeployProvider()
        target = _application_target()
        for status, state, expected in (
            ("error", "", "unknown"),
            ("failed", "completed", "present"),
            ("error", "rollback_completed", "present"),
            ("error", "paused", "present"),
            ("failed", "rollback_paused", "present"),
            ("done", "completed", "unknown"),
            ("done", "rollback_completed", "unknown"),
            ("done", "paused", "unknown"),
            ("done", "rollback_paused", "unknown"),
            ("error", "updating", "unknown"),
            ("failed", "rollback_started", "unknown"),
            ("canceled", "completed", "unknown"),
            ("cancelled", "", "unknown"),
            ("killed", "rollback_completed", "unknown"),
        ):
            with self.subTest(status=status, state=state):
                job = GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status=status,
                    deployment_id="failed-job",
                    started_at="2026-10-20T03:08:20Z",
                    finished_at="2026-10-20T03:08:25Z",
                )
                with patch.object(
                    provider,
                    "observe_testing_application_runtime",
                    return_value=replace(observed, rollout_state=state),
                ):
                    result = qualify_testing_application_observation(
                        provider=provider,
                        control_plane_root=Path("."),
                        target=target,
                        observation=job,
                        deployment_record_id="deployment-1",
                    )
                self.assertEqual(result.outcome, expected)
                if expected == "present":
                    self.assertEqual(result, job)
        with patch.object(
            provider,
            "observe_testing_application_runtime",
            side_effect=click.ClickException("unreadable service"),
        ):
            result = qualify_testing_application_observation(
                provider=provider,
                control_plane_root=Path("."),
                target=target,
                observation=job.model_copy(update={"deployment_status": "error"}),
                deployment_record_id="deployment-1",
            )
        self.assertEqual(result.outcome, "unknown")

    def test_completion_waits_for_rollout_and_recovery_does_not_credit_pending(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
            GenericWebProviderDeploymentObservation,
            qualify_testing_application_observation,
        )

        observed = self.read()
        target = _application_target()
        provider = DokployGenericWebDeployProvider()
        job = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="done",
            deployment_id="job-1",
            started_at="2026-10-20T03:08:20Z",
            finished_at="2026-10-20T03:08:25Z",
        )

        def qualify(
            *, wait: bool = False, deadline: float | None = None
        ) -> GenericWebProviderDeploymentObservation:
            return qualify_testing_application_observation(
                provider=provider,
                control_plane_root=Path("."),
                target=target,
                observation=job,
                deployment_record_id="deployment-1",
                wait=wait,
                deadline=deadline,
            )

        pending = replace(observed, rollout_state="updating")
        with patch.object(provider, "observe_testing_application_runtime", return_value=pending):
            self.assertEqual(qualify().outcome, "unknown")
        with (
            patch.object(
                provider, "observe_testing_application_runtime", side_effect=[pending, observed]
            ),
            patch("control_plane.workflows.generic_web_deploy_provider.sleep"),
        ):
            self.assertEqual(qualify(wait=True), job)
        with (
            patch.object(
                provider,
                "observe_testing_application_runtime",
                side_effect=[DokployEvidenceProviderError("target-inspect"), observed],
            ) as reader,
            patch("control_plane.workflows.generic_web_deploy_provider.sleep"),
        ):
            self.assertEqual(qualify(wait=True), job)
            self.assertEqual(reader.call_count, 2)
        with patch.object(
            provider,
            "observe_testing_application_runtime",
            side_effect=DokployEvidenceProviderError("target-inspect"),
        ) as reader:
            self.assertEqual(qualify().outcome, "unknown")
            reader.assert_called_once()
        with (
            patch.object(
                provider, "observe_testing_application_runtime", return_value=pending
            ) as reader,
            patch(
                "control_plane.workflows.generic_web_deploy_provider.monotonic", return_value=10.0
            ),
            patch("control_plane.workflows.generic_web_deploy_provider.sleep") as sleeper,
        ):
            self.assertEqual(qualify(wait=True, deadline=10.0).outcome, "unknown")
            reader.assert_called_once()
            sleeper.assert_not_called()
        with patch.object(
            provider,
            "observe_testing_application_runtime",
            return_value=replace(observed, application_id="foreign"),
        ):
            self.assertEqual(qualify().outcome, "unknown")
        rollback = replace(
            observed,
            rollout_state="rollback_completed",
            service_deployment_record_id="prior-deployment",
            running_container_deployment_record_ids=("prior-deployment",),
        )
        target.testing_runtime_digest = ""
        with patch.object(provider, "observe_testing_application_runtime", return_value=rollback):
            failed = qualify()
        self.assertEqual(failed.deployment_status, "unhealthy")
        with patch.object(
            provider,
            "observe_testing_application_runtime",
            return_value=replace(
                rollback,
                rollout_started_at="2026-10-19T01:00:00Z",
            ),
        ):
            self.assertEqual(qualify().outcome, "unknown")

    def test_positive_runtime_read_is_accepted_when_it_finishes_after_deadline(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
            GenericWebProviderDeploymentObservation,
            qualify_testing_application_observation,
        )

        current = self.read()
        provider = DokployGenericWebDeployProvider()
        job = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="done",
            deployment_id="job-1",
            started_at="2026-10-20T03:08:20Z",
            finished_at="2026-10-20T03:08:25Z",
        )
        now = 0.0

        def read(**_kwargs: object) -> ApplicationRuntimeObservation:
            nonlocal now
            now = 11.0
            return current

        with (
            patch.object(
                provider, "observe_testing_application_runtime", side_effect=read
            ) as reader,
            patch(
                "control_plane.workflows.generic_web_deploy_provider.monotonic",
                side_effect=lambda: now,
            ),
            patch("control_plane.workflows.generic_web_deploy_provider.sleep") as sleeper,
        ):
            qualified = qualify_testing_application_observation(
                provider=provider,
                control_plane_root=Path("."),
                target=_application_target(),
                observation=job,
                deployment_record_id=current.service_deployment_record_id,
                wait=True,
                deadline=10.0,
            )
        self.assertEqual(qualified, job)
        reader.assert_called_once()
        sleeper.assert_not_called()

    def test_current_attempt_task_attributes_late_rollback_without_clock_slack(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
            GenericWebProviderDeploymentObservation,
            qualify_testing_application_observation,
        )

        current = self.read()
        provider = DokployGenericWebDeployProvider()
        job = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="done",
            deployment_id="job-1",
            started_at="2026-10-20T03:08:20Z",
            finished_at="2026-10-20T03:08:25Z",
        )
        task = ApplicationTaskObservation(
            image=_IMAGE,
            deployment_record_id=current.service_deployment_record_id,
            state="failed",
        )
        for state in ("paused", "rollback_paused", "rollback_completed"):
            rollback = replace(
                current,
                rollout_state=state,
                rollout_started_at="2026-10-20T03:08:26Z",
                service_deployment_record_id="previous-deployment",
                running_container_images=(),
                running_container_deployment_record_ids=(),
                running_container_health=(),
                tasks=(task,),
            )
            for observed_task, expected in (
                (task, "present"),
                (replace(task, deployment_record_id="previous-deployment"), "unknown"),
                (replace(task, image=f"{_REPOSITORY}@sha256:{'e' * 64}"), "unknown"),
            ):
                with (
                    self.subTest(state=state, task=observed_task),
                    patch.object(
                        provider,
                        "observe_testing_application_runtime",
                        return_value=replace(rollback, tasks=(observed_task,)),
                    ),
                ):
                    qualified = qualify_testing_application_observation(
                        provider=provider,
                        control_plane_root=Path("."),
                        target=_application_target(),
                        observation=job,
                        deployment_record_id=current.service_deployment_record_id,
                    )
                self.assertEqual(qualified.outcome, expected)
                if expected == "present":
                    self.assertEqual(qualified.deployment_status, "unhealthy")

    def test_absent_update_status_cannot_prove_failure_at_original_deadline(
        self,
    ) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            DokployGenericWebDeployProvider,
            GenericWebProviderDeploymentObservation,
            qualify_testing_application_observation,
        )

        self.service["PreviousSpec"] = deepcopy(self.service["Spec"])
        self.service.pop("UpdateStatus")
        self.task["Status"] = {"State": "failed"}
        self.task["DesiredState"] = "shutdown"
        self.task["Spec"] = {
            "ContainerSpec": {
                "Image": _IMAGE,
                "Env": ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=deployment-1"],
            }
        }
        target = _application_target()
        provider = DokployGenericWebDeployProvider()
        job = GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="done",
            deployment_id="job-1",
            started_at="2026-10-20T03:08:20Z",
            finished_at="2026-10-20T03:08:25Z",
        )

        def qualify(*, wait: bool) -> GenericWebProviderDeploymentObservation:
            with (
                patch.object(
                    provider, "observe_testing_application_runtime", return_value=self.read()
                ),
                patch(
                    "control_plane.workflows.generic_web_deploy_provider.monotonic",
                    return_value=10.0,
                ),
                patch("control_plane.workflows.generic_web_deploy_provider.sleep") as sleeper,
            ):
                result = qualify_testing_application_observation(
                    provider=provider,
                    control_plane_root=Path("."),
                    target=target,
                    observation=job,
                    deployment_record_id="deployment-1",
                    wait=wait,
                    deadline=10.0 if wait else None,
                )
                sleeper.assert_not_called()
                return result

        self.assertEqual(qualify(wait=True).outcome, "unknown")
        self.assertEqual(qualify(wait=False).outcome, "unknown")
        previous_specs: tuple[object, ...] = (None, {}, "unreadable")
        for previous in previous_specs:
            self.service["PreviousSpec"] = previous
            self.assertEqual(qualify(wait=True).outcome, "unknown")
        self.service.pop("PreviousSpec")
        self.assertEqual(qualify(wait=True).deployment_status, "unhealthy")
        self.assertEqual(qualify(wait=False).outcome, "unknown")
        self.service["PreviousSpec"] = deepcopy(self.service["Spec"])
        original_spec = deepcopy(self.task["Spec"])
        for state in ("rejected",):
            self.task["Status"] = {"State": state}
            self.assertEqual(qualify(wait=True).outcome, "unknown")
        for state in ("starting", "shutdown"):
            self.task["Status"] = {"State": state}
            self.assertEqual(qualify(wait=True).outcome, "unknown")
        self.task["Status"] = {"State": "failed"}
        for spec in (
            {},
            {"Image": _IMAGE},
            {"Image": _IMAGE, "Env": ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=older"]},
        ):
            with self.subTest(spec=spec):
                self.task["Spec"] = {"ContainerSpec": spec}
                self.assertEqual(qualify(wait=True).outcome, "unknown")
        self.task["Spec"] = original_spec
        self.tasks.append({"containerId": "old-task"})
        self.vanished_tasks.add("old-task")
        self.assertEqual(qualify(wait=True).outcome, "unknown")
        self.tasks.pop()
        self.task.update(
            DesiredState="running",
            Status={"State": "running", "ContainerStatus": {"ContainerID": _CONTAINER_ID}},
        )
        self.vanished_tasks.add(_CONTAINER_ID)
        self.assertEqual(qualify(wait=True).outcome, "unknown")
        self.vanished_tasks.remove(_CONTAINER_ID)
        self.assertEqual(qualify(wait=True), job)
        self.assertEqual(qualify(wait=False), job)
