from __future__ import annotations

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
from control_plane.workflows.odoo_generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_driver_id,
)
from control_plane.workflows.verireel_testing_post_deploy import (
    assert_verireel_testing_identity,
    execute_verireel_testing_post_deploy,
    verify_verireel_testing_product,
    wait_for_verireel_testing_identity,
    verification_path,
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
        events: list[str] = []

        def migrate(*args: object) -> None:
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
            self.context = context("prod")
            self.assertEqual(self.execute().status, "fail")
        maintain.assert_not_called()

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
        playwright = MagicMock()
        playwright.__enter__.return_value.chromium.launch.return_value = browser
        evidence: dict[str, str] = {}
        with (
            patch(
                "control_plane.workflows.verireel_testing_post_deploy.sync_playwright",
                return_value=playwright,
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
        self.assertEqual(maintain.call_args.args[3], "delete-user")
        self.assertEqual(evidence["cleanup_status"], "pass")
        browser.close.assert_called_once()
