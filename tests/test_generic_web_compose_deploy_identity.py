"""A compose deploy that finished must not look unfinished.

Dokploy compose deployments have not carried the title Launchplane sends, so the
titled wait never matched them and every RepairShopr deploy left its reservation
in ``reconcile_required`` (cbusillo/launchplane#2531). These tests cover the
untitled-deployment fallback and the runtime proof that keeps an unproven outcome
unknown.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal
import unittest
from unittest.mock import patch

import click

from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.dokploy import api as dokploy_api
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.workflows.dokploy_deploy import execute_dokploy_artifact_deploy
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployRequest,
    execute_generic_web_deploy,
)
from control_plane.workflows.generic_web_deploy_provider import (
    DokployGenericWebDeployProvider,
    GenericWebProviderDeploymentObservation,
    GenericWebResolvedDeployTarget,
    GenericWebRuntimeArtifactObservation,
)
from tests import test_generic_web_deploy as generic_web_deploy_tests
from tests.test_generic_web_deploy import (
    _FakeGenericWebDeployProvider,
    _GenericWebDeployStore,
    _profile,
)


_TITLE = "Launchplane operation 0123456789abcdef01234567"
_ARTIFACT = "ghcr.io/cbusillo/sellyouroutboard@sha256:" + "a" * 64
_OTHER_ARTIFACT = "ghcr.io/cbusillo/sellyouroutboard@sha256:" + "b" * 64


def _deployment(
    key: str, *, status: str = "done", title: str = "Manual deployment"
) -> dokploy_api.JsonObject:
    return {
        "deploymentId": key,
        "title": title,
        "status": status,
        "createdAt": f"2026-09-28T02:54:{24 + len(key) % 10:02d}.000Z",
        "startedAt": "2026-09-28T02:54:24.742Z",
        "finishedAt": "2026-09-28T02:54:38.667Z",
    }


def _wait(
    listings: list[list[dokploy_api.JsonObject]],
    *,
    known: frozenset[str] = frozenset({"old-1", "old-2"}),
) -> dokploy_api.JsonObject:
    clock = iter(range(0, 10_000, 5))
    with (
        patch.object(dokploy_api, "list_deployments_for_target", side_effect=listings),
        patch("control_plane.dokploy.api.time.sleep"),
        patch("control_plane.dokploy.api.time.monotonic", side_effect=lambda: next(clock)),
    ):
        return dokploy_api.wait_for_titled_or_sole_new_target_deployment(
            host="https://dokploy.example",
            token="token",
            target_type="compose",
            target_id="compose-1",
            known_deployment_keys=known,
            deployment_title=_TITLE,
            timeout_seconds=len(listings) * 5,
        )


class TitledOrSoleNewDeploymentWaitTests(unittest.TestCase):
    def test_untitled_sole_new_deployment_is_accepted_once_it_succeeds(self) -> None:
        old = [_deployment("old-1"), _deployment("old-2")]

        matched = _wait(
            [old, [*old, _deployment("new", status="running")], [*old, _deployment("new")]]
        )

        self.assertEqual(matched["deploymentId"], "new")

    def test_titled_deployment_is_preferred_over_an_untitled_new_one(self) -> None:
        old = [_deployment("old-1")]

        matched = _wait(
            [[*old, _deployment("other"), _deployment("ours", title=_TITLE)]],
            known=frozenset({"old-1"}),
        )

        self.assertEqual(matched["deploymentId"], "ours")

    def test_two_new_untitled_deployments_are_ambiguous_and_time_out(self) -> None:
        listing = [_deployment("old-1"), _deployment("new-a"), _deployment("new-b")]

        with self.assertRaisesRegex(click.ClickException, "Timed out"):
            _wait([listing, listing, listing], known=frozenset({"old-1"}))

    def test_no_new_deployment_times_out(self) -> None:
        listing = [_deployment("old-1"), _deployment("old-2")]

        with self.assertRaisesRegex(click.ClickException, "Timed out"):
            _wait([listing, listing])

    def test_failed_sole_new_deployment_raises_failure(self) -> None:
        with self.assertRaises(dokploy_api.DokployDeploymentFailed):
            _wait(
                [[_deployment("old-1"), _deployment("old-2"), _deployment("new", status="error")]]
            )


def _compose_ship_request() -> tuple[ShipRequest, ResolvedTargetEvidence]:
    return (
        ShipRequest(
            artifact_id=_ARTIFACT,
            context="example",
            instance="prod",
            source_git_ref="abc123",
            target_name="example-compose",
            target_type="compose",
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            deploy_mode="dokploy-compose-api",
            verify_health=False,
            destination_health=HealthcheckEvidence(status="skipped"),
        ),
        ResolvedTargetEvidence(
            target_type="compose", target_id="compose-1", target_name="example-compose"
        ),
    )


class ExecuteDokployArtifactDeployIdentityTests(unittest.TestCase):
    def _execute(
        self, *, accept: bool, matched: dokploy_api.JsonObject
    ) -> tuple[object, list[str]]:
        ship_request, resolved_target = _compose_ship_request()
        called: list[str] = []

        def record_identity_wait(**_kwargs: object) -> dokploy_api.JsonObject:
            called.append("identity")
            return matched

        def record_titled_wait(**_kwargs: object) -> str:
            called.append("titled")
            return ""

        with (
            patch.object(dokploy_api, "latest_deployment_for_target", return_value=None),
            patch.object(
                dokploy_api,
                "list_deployments_for_target",
                return_value=[_deployment("old-1")],
            ),
            patch("control_plane.workflows.dokploy_deploy.update_dokploy_target_artifact"),
            patch.object(dokploy_api, "trigger_deployment"),
            patch.object(
                dokploy_api,
                "wait_for_titled_or_sole_new_target_deployment",
                side_effect=record_identity_wait,
            ) as identity_wait,
            patch.object(
                dokploy_api,
                "wait_for_target_deployment",
                side_effect=record_titled_wait,
            ),
        ):
            result = execute_dokploy_artifact_deploy(
                host="https://dokploy.example",
                token="token",
                ship_request=ship_request,
                resolved_target=resolved_target,
                deploy_timeout_seconds=60,
                deployment_title=_TITLE,
                accept_sole_new_deployment=accept,
            )
        if accept:
            self.assertEqual(
                identity_wait.call_args.kwargs["known_deployment_keys"], frozenset({"old-1"})
            )
        return result, called

    def test_untitled_match_is_returned_for_proof(self) -> None:
        result, called = self._execute(accept=True, matched=_deployment("new"))

        self.assertEqual(called, ["identity"])
        self.assertEqual(result, _deployment("new"))

    def test_titled_match_returns_none(self) -> None:
        result, called = self._execute(accept=True, matched=_deployment("new", title=_TITLE))

        self.assertEqual(called, ["identity"])
        self.assertIsNone(result)

    def test_without_the_flag_the_titled_wait_is_unchanged(self) -> None:
        result, called = self._execute(accept=False, matched=_deployment("new"))

        self.assertEqual(called, ["titled"])
        self.assertIsNone(result)


class _UntitledComposeProvider(_FakeGenericWebDeployProvider):
    """Finishes an untitled deployment and reports the queued runtime state."""

    def __init__(self, runtime: GenericWebRuntimeArtifactObservation | Exception) -> None:
        super().__init__(observation=GenericWebProviderDeploymentObservation(outcome="absent"))
        self.runtime = runtime
        self.runtime_calls = 0
        self.title_observations = 0

    def execute_artifact_deploy(  # type: ignore[override]
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        runtime_identity: RuntimeIdentity,
        deployment_title: str,
        before_provider_mutation: Callable[[str], None],
        effect_started: Callable[[], None],
    ) -> GenericWebProviderDeploymentObservation | None:
        super().execute_artifact_deploy(
            control_plane_root=control_plane_root,
            resolved_deploy_target=resolved_deploy_target,
            runtime_identity=runtime_identity,
            deployment_title=deployment_title,
            before_provider_mutation=before_provider_mutation,
            effect_started=effect_started,
        )
        return GenericWebProviderDeploymentObservation(
            outcome="present",
            deployment_status="done",
            deployment_id="dokploy-untitled-1",
            started_at="2026-09-28T02:54:24.742Z",
            finished_at="2026-09-28T02:54:38.667Z",
        )

    def observe_artifact_deploy(self, **kwargs: object) -> GenericWebProviderDeploymentObservation:
        self.title_observations += 1
        return super().observe_artifact_deploy(**kwargs)  # type: ignore[arg-type]

    def observe_runtime_artifact(self, **_kwargs: object) -> GenericWebRuntimeArtifactObservation:
        self.runtime_calls += 1
        if isinstance(self.runtime, Exception):
            raise self.runtime
        return self.runtime


def _runtime(
    *, target: str = _ARTIFACT, running: tuple[str, ...] = (_ARTIFACT, "mariadb:13.0")
) -> GenericWebRuntimeArtifactObservation:
    return GenericWebRuntimeArtifactObservation(
        target_artifact_reference=target, running_container_images=running
    )


def _request() -> GenericWebDeployRequest:
    return GenericWebDeployRequest(
        product="sellyouroutboard",
        instance="testing",
        artifact_id=_ARTIFACT,
        source_git_ref="abc123",
    )


class UntitledDeploymentProofTests(unittest.TestCase):
    def test_proven_untitled_deployment_records_pass_with_its_evidence(self) -> None:
        store = _GenericWebDeployStore(_profile())
        provider = _UntitledComposeProvider(_runtime())

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            deploy_provider=provider,
            provider_operation_title=_TITLE,
        )

        self.assertEqual(result.deploy_status, "pass", result.error_message)
        self.assertEqual(provider.runtime_calls, 1)
        self.assertEqual(provider.title_observations, 0)
        self.assertEqual(store.deployments[0].deploy.deployment_id, "dokploy-untitled-1")
        self.assertEqual(store.deployments[0].deploy.finished_at, "2026-09-28T02:54:38.667Z")
        self.assertEqual(len(store.inventories), 1)

    def test_unproven_untitled_deployment_stays_unfinished(self) -> None:
        cases: dict[str, GenericWebRuntimeArtifactObservation | Exception] = {
            "target configured for another image": _runtime(target=_OTHER_ARTIFACT),
            "another image still running": _runtime(running=(_ARTIFACT, _OTHER_ARTIFACT)),
            "original image not running": _runtime(running=("mariadb:13.0",)),
            "runtime read fails": click.ClickException("read failed"),
        }
        for name, runtime in cases.items():
            with self.subTest(name):
                store = _GenericWebDeployStore(_profile())

                result = execute_generic_web_deploy(
                    control_plane_root=Path("."),
                    record_store=store,
                    request=_request(),
                    deploy_provider=_UntitledComposeProvider(runtime),
                    provider_operation_title=_TITLE,
                )

                self.assertEqual(result.deploy_status, "fail")
                self.assertTrue(result.provider_effect_attempted)
                self.assertEqual(store.inventories, [])

    def test_provider_without_runtime_capability_cannot_prove_untitled_deploy(self) -> None:
        class _NoRuntime(_UntitledComposeProvider):
            observe_runtime_artifact = None  # type: ignore[assignment]

        store = _GenericWebDeployStore(_profile())

        result = execute_generic_web_deploy(
            control_plane_root=Path("."),
            record_store=store,
            request=_request(),
            deploy_provider=_NoRuntime(_runtime()),
            provider_operation_title=_TITLE,
        )

        self.assertEqual(result.deploy_status, "fail")


class DurableOperationOutcomeTests(unittest.TestCase):
    """The durable adapter completes a proven deploy and holds an unproven one."""

    def _apply(self, provider: _UntitledComposeProvider) -> object:
        profile = _profile()
        store = _GenericWebDeployStore(profile)
        adapter = generic_web_deploy_tests.GenericWebDeployTests._provider_mutation_adapter(
            profile=profile,
            store=store,
            provider=provider,
            deploy_request=_request(),
        )

        class _Lease:
            def assert_current(self) -> None:
                return None

            def checkpoint_effect(self, _phase: str) -> None:
                return None

        return adapter.apply("provider-operation:compose-identity", _Lease())

    def test_proven_untitled_deploy_completes_durably(self) -> None:
        outcome = self._apply(_UntitledComposeProvider(_runtime()))

        self.assertTrue(getattr(outcome, "durable"))
        self.assertEqual(getattr(outcome, "response_payload")["result"]["deploy_status"], "pass")

    def test_unproven_untitled_deploy_stays_unknown(self) -> None:
        with self.assertRaises(ProviderMutationUnknownError):
            self._apply(_UntitledComposeProvider(_runtime(target=_OTHER_ARTIFACT)))


class DokployProviderIdentityTests(unittest.TestCase):
    def _execute(
        self,
        target_type: Literal["compose", "application"],
        returned: dokploy_api.JsonObject | None,
    ) -> tuple[object, bool]:
        ship_request, _ = _compose_ship_request()
        resolved = GenericWebResolvedDeployTarget(
            ship_request=ship_request,
            resolved_target=ResolvedTargetEvidence(
                target_type=target_type, target_id="target-1", target_name="example"
            ),
            deploy_timeout_seconds=60,
        )
        provider = DokployGenericWebDeployProvider()
        with (
            patch.object(provider, "_read_provider_config", return_value=("host", "token")),
            patch(
                "control_plane.workflows.generic_web_deploy_provider."
                "execute_dokploy_artifact_deploy",
                return_value=returned,
            ) as execute,
        ):
            observation = provider.execute_artifact_deploy(
                control_plane_root=Path("."),
                resolved_deploy_target=resolved,
                runtime_identity=RuntimeIdentity.model_construct(),
                deployment_title=_TITLE,
                before_provider_mutation=lambda _phase: None,
                effect_started=lambda: None,
            )
        return observation, execute.call_args.kwargs["accept_sole_new_deployment"]

    def test_only_compose_targets_accept_an_untitled_deployment(self) -> None:
        _, compose_accepts = self._execute("compose", None)
        _, application_accepts = self._execute("application", None)

        self.assertTrue(compose_accepts)
        self.assertFalse(application_accepts)

    def test_untitled_deployment_becomes_a_present_observation(self) -> None:
        observation, _ = self._execute("compose", _deployment("new"))

        assert isinstance(observation, GenericWebProviderDeploymentObservation)
        self.assertEqual(observation.outcome, "present")
        self.assertEqual(observation.deployment_id, "new")

    def test_untitled_deployment_without_terminal_evidence_is_rejected(self) -> None:
        incomplete: dokploy_api.JsonObject = {"deploymentId": "new", "status": "done"}

        with self.assertRaises(click.ClickException):
            self._execute("compose", incomplete)


if __name__ == "__main__":
    unittest.main()
