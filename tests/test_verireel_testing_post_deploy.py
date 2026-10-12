from __future__ import annotations

from control_plane.workflows.generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_driver_id,
    generic_web_post_deploy_executor_for_profile,
)

import json
from http.client import RemoteDisconnected
from pathlib import Path
from typing import cast
import unittest
from unittest.mock import MagicMock, Mock, patch

import click

from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    GenericWebPostDeployContext,
    GenericWebPostDeployGuard,
)
from control_plane.workflows.verireel_testing_post_deploy import (
    assert_verireel_testing_identity,
    execute_verireel_testing_post_deploy,
    verify_verireel_testing_product,
    wait_for_verireel_testing_identity,
    verification_path,
    launch_testing_browser,
    fixture_email,
    checkpoint_testing_effect,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


def context(instance: str = "testing") -> GenericWebPostDeployContext:
    return GenericWebPostDeployContext(
        product="video",
        context="verireel",
        instance=instance,
        deployment_record_id="deployment-video-testing",
        target_name="test-app",
        target_id="test-target",
        target_type="application",
        artifact_id=f"ghcr.io/example/video@sha256:{'a' * 64}",
        source_git_ref="b" * 40,
    )


class VeriReelTestingPostDeployTests(unittest.TestCase):
    def test_public_probe_accepts_the_rendered_absolute_link_and_refuses_other_origins(
        self,
    ) -> None:
        origin = "https://testing.example.test"
        path = "/v/disposable-record"
        self.assertEqual(verification_path(origin, origin + path), path)
        self.assertEqual(verification_path(origin, path), path)
        for href in ("https://other.example.test" + path, "/dashboard", ""):
            with self.assertRaises(click.ClickException):
                verification_path(origin, href)

    def setUp(self) -> None:
        browser_availability = patch(
            "control_plane.workflows.verireel_testing_post_deploy.check_testing_browser_availability"
        )
        browser_availability.start()
        self.addCleanup(browser_availability.stop)
        authority = patch(
            "control_plane.workflows.verireel_testing_post_deploy.testing_qualification_authority",
            return_value="test-authority",
        )
        authority.start()
        self.addCleanup(authority.stop)
        self.context = context()
        self.store = Mock()
        self.store.read_product_profile_record.return_value = (
            LaunchplaneProductProfileRecord.model_validate(
                {
                    "product": "video",
                    "display_name": "Video",
                    "driver_id": "verireel",
                    "repository": "example/video",
                    "image": {"repository": "ghcr.io/example/video"},
                    "lanes": [{"context": "verireel", "instance": "testing"}],
                    "updated_at": "2026-10-12T00:00:00Z",
                    "source": "test",
                }
            )
        )

    def execute(self) -> PostDeployUpdateEvidence:
        return execute_verireel_testing_post_deploy(
            Path("."),
            cast(GenericWebDeployStore, self.store),
            self.context,
            GenericWebPostDeployGuard(),
        )

    def test_only_testing_registers_the_extension(self) -> None:
        self.assertIs(
            generic_web_post_deploy_executor_for_driver_id("verireel", instance="testing"),
            execute_verireel_testing_post_deploy,
        )
        for instance in ("prod", "preview", ""):
            self.assertIsNone(
                generic_web_post_deploy_executor_for_driver_id("verireel", instance=instance)
            )

    def test_migration_runs_before_product_verification_and_binds_evidence(self) -> None:
        self._check_migration_and_verification()

    def test_declared_generic_web_testing_uses_the_same_qualification_on_its_recorded_lane(
        self,
    ) -> None:
        from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions

        profile = self.store.read_product_profile_record.return_value
        self.context = self.context.model_copy(update={"context": "sample-testing"})
        profile = type(profile).model_validate(
            profile.model_copy(
                update={
                    "driver_id": "generic-web",
                    "lanes": (
                        profile.lanes[0].model_copy(
                            update={
                                "context": self.context.context,
                                "generic_web_extensions": GenericWebLaneExtensions(
                                    verification_extension="verireel-testing"
                                ),
                            }
                        ),
                    ),
                }
            ).model_dump(mode="json")
        )
        self.store.read_product_profile_record.return_value = profile
        self.assertIs(
            generic_web_post_deploy_executor_for_profile(profile, instance="testing"),
            execute_verireel_testing_post_deploy,
        )
        self.assertIsNone(generic_web_post_deploy_executor_for_profile(profile, instance="prod"))
        self._check_migration_and_verification()

    def test_maintenance_passes_the_reserved_lane_and_target_to_the_internal_executor(self) -> None:
        from control_plane.workflows.verireel_testing_post_deploy import _maintenance

        bound = self.context.model_copy(update={"context": "sample-testing"})
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy._resolve_stable_testing_application",
                return_value=("sample-app", bound.target_id),
            ) as resolve,
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.execute_verireel_app_maintenance",
                return_value=Mock(maintenance_status="pass"),
            ) as execute,
        ):
            _maintenance(
                Path("."),
                bound,
                GenericWebPostDeployGuard(),
                action="migrate",
                intent="stable-testing-migration",
            )
        resolve.assert_called_once_with(control_plane_root=Path("."), context_name=bound.context)
        target = execute.call_args.kwargs["testing_target"]
        self.assertEqual((target.context, target.application_id), (bound.context, bound.target_id))

    def _check_migration_and_verification(self) -> None:
        events: list[str] = []

        def migrate(*_args: object, **_kwargs: object) -> None:
            events.append("migration")

        def verify(*args: object) -> None:
            events.append("verification")
            evidence = cast(dict[str, str], args[-1])
            evidence.update(
                creator_verification_status="pass",
                admin_verification_status="pass",
                cleanup_status="pass",
            )

        with (
            patch("control_plane.workflows.verireel_testing_post_deploy._maintenance", migrate),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.verify_verireel_testing_product",
                verify,
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity"
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy._resolve_stable_testing_base_url",
                return_value="https://video.test",
            ),
        ):
            result = self.execute()
        self.assertEqual(events, ["migration", "verification"])
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.evidence["deployment_record_id"], self.context.deployment_record_id)
        self.assertEqual(result.evidence["source_git_ref"], self.context.source_git_ref)
        self.assertEqual(result.evidence["artifact_id"], self.context.artifact_id)

    def test_migration_failure_does_not_call_the_browser_or_report_healthy(self) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy._maintenance",
                side_effect=click.ClickException("failed"),
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.verify_verireel_testing_product"
            ) as verify,
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity"
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy._resolve_stable_testing_base_url",
                return_value="https://video.test",
            ),
        ):
            result = self.execute()
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.evidence["migration_status"], "fail")
        verify.assert_not_called()

    def test_unavailable_browser_makes_no_migration_or_fixture_change(self) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.check_testing_browser_availability",
                side_effect=click.ClickException("browser unavailable"),
            ),
            patch("control_plane.workflows.verireel_testing_post_deploy._maintenance") as maintain,
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity"
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy._resolve_stable_testing_base_url",
                return_value="https://video.test",
            ),
        ):
            result = self.execute()
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.evidence["failure_kind"], "transient")
        self.assertEqual(result.evidence["migration_status"], "skipped")
        maintain.assert_not_called()

    def test_gateway_outage_is_transient_and_a_missing_route_is_a_product_failure(self) -> None:
        from control_plane.workflows.verireel_testing_post_deploy import (
            TestingProductCheckFailed,
            _goto,
        )

        page = Mock(url="https://video.test/dashboard")
        page.goto.return_value.status = 503
        with self.assertRaises(click.ClickException) as transient:
            _goto(page, "https://video.test", "/dashboard")
        self.assertNotIsInstance(transient.exception, TestingProductCheckFailed)
        page.goto.return_value.status = 404
        with self.assertRaises(TestingProductCheckFailed):
            _goto(page, "https://video.test", "/dashboard")

    def test_production_and_runtime_mismatch_make_no_maintenance_call(self) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.wait_for_verireel_testing_identity",
                side_effect=click.ClickException("mismatch"),
            ),
            patch("control_plane.workflows.verireel_testing_post_deploy._maintenance") as maintain,
            patch(
                "control_plane.workflows.verireel_testing_post_deploy._resolve_stable_testing_base_url",
                return_value="https://video.test",
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity",
                side_effect=click.ClickException("mismatch"),
            ),
        ):
            self.assertEqual(self.execute().status, "fail")
        maintain.assert_not_called()

    def test_production_lane_is_refused_before_identity_reads_or_maintenance(self) -> None:
        from control_plane.contracts.product_profile_record import ProductLaneProfile

        profile = self.store.read_product_profile_record.return_value
        self.store.read_product_profile_record.return_value = profile.model_copy(
            update={
                "lanes": (*profile.lanes, ProductLaneProfile(context="verireel", instance="prod")),
            }
        )
        self.context = context("prod")
        with (
            patch("control_plane.workflows.verireel_testing_post_deploy._maintenance") as maintain,
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.wait_for_verireel_testing_identity"
            ) as identity,
        ):
            self.assertEqual(self.execute().status, "fail")
        maintain.assert_not_called()
        identity.assert_not_called()

    def test_remote_browser_connects_without_forwarding_the_local_environment(self) -> None:
        chromium = Mock()
        endpoint = "ws://browser.example.test:3000/"
        with patch.dict(
            "os.environ",
            {
                "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "private-fixture",
                "LAUNCHPLANE_BROWSER_WS_ENDPOINT": endpoint,
            },
        ):
            launch_testing_browser(chromium)
        chromium.connect.assert_called_once()
        self.assertEqual(chromium.connect.call_args.args, (endpoint,))
        self.assertGreater(chromium.connect.call_args.kwargs["timeout"], 0)
        chromium.launch.assert_not_called()

    def test_linux_refuses_to_render_inside_the_credentialed_service(self) -> None:
        chromium = Mock()
        with (
            patch.dict("os.environ", {"LAUNCHPLANE_BROWSER_WS_ENDPOINT": ""}),
            patch("control_plane.workflows.verireel_testing_post_deploy.sys.platform", "linux"),
        ):
            with self.assertRaises(click.ClickException):
                launch_testing_browser(chromium)
        chromium.launch.assert_not_called()

    def test_lost_lease_is_an_unknown_effect_instead_of_a_cached_failure(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        with self.assertRaises(ProviderMutationUnknownError):
            checkpoint_testing_effect(
                GenericWebPostDeployGuard(
                    provider_effect_checkpoint=Mock(side_effect=RuntimeError("lease lost")),
                ),
                "post_deploy_fixture_signup",
            )

    def test_fixture_identity_is_available_even_if_cleanup_cannot_continue(self) -> None:
        browser = MagicMock()
        page = browser.new_context.return_value.__enter__.return_value.new_page.return_value
        page.wait_for_load_state.side_effect = RuntimeError("signup failed")
        evidence: dict[str, str] = {}
        with (
            patch("control_plane.workflows.verireel_testing_post_deploy.sync_playwright"),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.launch_testing_browser",
                return_value=browser,
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity"
            ),
            patch("control_plane.workflows.verireel_testing_post_deploy._goto"),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy._maintenance",
                side_effect=click.ClickException("cleanup failed"),
            ),
        ):
            with self.assertRaises(click.ClickException):
                verify_verireel_testing_product(
                    Path("."),
                    self.context,
                    GenericWebPostDeployGuard(),
                    "https://video.test",
                    evidence,
                )
        self.assertEqual(
            evidence["fixture_email"], fixture_email(self.context.deployment_record_id)
        )
        self.assertEqual(evidence["cleanup_status"], "fail")

    def test_startup_connection_close_is_polled_before_any_mutation(self) -> None:
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity",
                side_effect=[RemoteDisconnected("starting"), None],
            ) as identity,
            patch("control_plane.workflows.verireel_testing_post_deploy.time.sleep") as sleep,
        ):
            wait_for_verireel_testing_identity("https://video.test", self.context)
        self.assertEqual(identity.call_count, 2)
        sleep.assert_called_once()

    def test_unknown_effect_cannot_be_hidden_by_a_cleanup_failure(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        browser = MagicMock()
        browser.close.side_effect = RuntimeError("browser disconnected")
        browser.new_context.return_value.__exit__.side_effect = RuntimeError("context disconnected")
        playwright = MagicMock()
        playwright.__exit__.side_effect = RuntimeError("driver disconnected")
        evidence: dict[str, str] = {}
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.sync_playwright",
                return_value=playwright,
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.launch_testing_browser",
                return_value=browser,
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity",
                side_effect=ProviderMutationUnknownError("effect fence lost"),
            ),
            patch("control_plane.workflows.verireel_testing_post_deploy._maintenance") as maintain,
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                verify_verireel_testing_product(
                    Path("."),
                    self.context,
                    GenericWebPostDeployGuard(),
                    "https://video.test",
                    evidence,
                )
        maintain.assert_not_called()
        self.assertEqual(evidence["cleanup_status"], "fail")

    def test_identity_requires_the_exact_deployment_source_and_digest(self) -> None:
        identity = {
            key: getattr(self.context, key)
            for key in (
                "context",
                "instance",
                "deployment_record_id",
                "artifact_id",
                "source_git_ref",
            )
        }
        payload = {
            "ok": True,
            "buildRevision": self.context.source_git_ref,
            "runtime_identity": identity,
        }
        with patch(
            "control_plane.workflows.verireel_testing_post_deploy.fetch_url_text",
            return_value=(200, json.dumps(payload)),
        ):
            assert_verireel_testing_identity("https://video.test", self.context)
        for key in identity:
            with self.subTest(key=key):
                changed = {**payload, "runtime_identity": {**identity, key: "different"}}
                with patch(
                    "control_plane.workflows.verireel_testing_post_deploy.fetch_url_text",
                    return_value=(200, json.dumps(changed)),
                ):
                    with self.assertRaises(click.ClickException):
                        assert_verireel_testing_identity("https://video.test", self.context)

    def test_browser_failure_cleans_up_its_fixture_and_closes_browser(self) -> None:
        browser = MagicMock()
        page = browser.new_context.return_value.__enter__.return_value.new_page.return_value
        page.wait_for_load_state.side_effect = RuntimeError("signup failed")
        evidence: dict[str, str] = {}
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.launch_testing_browser",
                return_value=browser,
            ),
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.assert_verireel_testing_identity"
            ),
            patch("control_plane.workflows.verireel_testing_post_deploy._goto"),
            patch("control_plane.workflows.verireel_testing_post_deploy._maintenance") as maintain,
        ):
            with self.assertRaises(RuntimeError):
                verify_verireel_testing_product(
                    Path("."),
                    self.context,
                    GenericWebPostDeployGuard(),
                    "https://video.test",
                    evidence,
                )
        self.assertEqual(maintain.call_args.kwargs["action"], "delete-user")
        self.assertEqual(evidence["cleanup_status"], "pass")
        browser.close.assert_called_once()
