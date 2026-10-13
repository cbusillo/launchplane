import json
import unittest
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import click

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.release_review import current_release_review, unavailable_release_review
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.test_release_review import BASE, HEAD, github_read, profile, seed

PRIVATE_URL = "https://private-host.invalid/secret-path"


def web_profile() -> LaunchplaneProductProfileRecord:
    return profile().model_copy(update={"driver_id": "generic-web"})


def inventory(instance: str, sha: str, *, identity: bool = True) -> EnvironmentInventory:
    artifact_id = f"artifact-{instance}"
    return EnvironmentInventory(
        context="example-site",
        instance=instance,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        source_git_ref=sha,
        deploy=DeploymentEvidence(
            target_name="site", target_type="application", deploy_mode="test", status="pass"
        ),
        runtime_identity=RuntimeIdentity(
            product="example-site",
            context="example-site",
            instance=instance,
            deployment_record_id=f"deployment-{instance}",
            artifact_id=artifact_id,
            source_git_ref=sha,
        )
        if identity
        else None,
        updated_at="2026-09-28T00:00:00Z",
        deployment_record_id=f"deployment-{instance}",
    )


class ReleaseEvidenceReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = FilesystemRecordStore(self.root / "state")
        seed(self.store)
        self.store.write_environment_inventory(inventory("prod", BASE))
        self.store.write_environment_inventory(inventory("testing", HEAD))
        self.token = self.enterContext(
            patch("control_plane.release_review.resolve_launchplane_github_token", return_value="t")
        )
        self.enterContext(
            patch(
                "control_plane.release_review.github_api_request",
                side_effect=lambda *, path, token, method="GET": (
                    None if method == "DELETE" else github_read(path)
                ),
            )
        )

    def reason(self, product_profile: LaunchplaneProductProfileRecord) -> str | None:
        with self.assertLogs("control_plane.release_review", "WARNING") as logs:
            review = current_release_review(
                control_plane_root=self.root,
                record_store=self.store,
                profile=product_profile,
                trace_id="trace-123",
            )
        self.assertIsNone(review.checklist)
        logged = " ".join(logs.output)
        self.assertIn(f"reason={review.unavailable_reason}", logged)
        self.assertIn("trace_id=trace-123", logged)
        self.assertNotIn(PRIVATE_URL, logged + " ".join(review.blockers))
        return review.unavailable_reason

    def test_compiles_from_recorded_runtime_identities(self) -> None:
        review = current_release_review(
            control_plane_root=self.root, record_store=self.store, profile=web_profile()
        )
        self.assertIsNone(review.unavailable_reason)
        assert review.checklist is not None
        self.assertEqual(review.checklist.production.source_commit, BASE)

    def test_testing_lane_missing(self) -> None:
        prod_only = web_profile().model_copy(
            update={"lanes": tuple(lane for lane in profile().lanes if lane.instance == "prod")}
        )
        self.assertEqual(self.reason(prod_only), "testing_lane_missing")

    def test_source_control_access_unavailable(self) -> None:
        self.token.return_value = ""
        self.assertEqual(self.reason(web_profile()), "source_control_access_unavailable")

    def test_production_identity_missing(self) -> None:
        self.store.write_environment_inventory(inventory("prod", BASE, identity=False))
        self.assertEqual(self.reason(web_profile()), "production_identity_missing")

    def test_candidate_identity_missing(self) -> None:
        self.store.write_environment_inventory(inventory("testing", HEAD, identity=False))
        self.assertEqual(self.reason(web_profile()), "candidate_identity_missing")

    def test_release_record_missing(self) -> None:
        empty = FilesystemRecordStore(self.root / "empty")
        self.store = empty
        self.assertEqual(self.reason(profile()), "release_record_missing")

    def test_github_read_failed_keeps_provider_text_private(self) -> None:
        with patch(
            "control_plane.release_review.github_api_request",
            side_effect=click.ClickException(f"GitHub API request failed for {PRIVATE_URL}"),
        ):
            self.assertEqual(self.reason(web_profile()), "github_read_failed")

    def test_single_transient_comparison_or_coverage_failure_recovers_checklist(self) -> None:
        for endpoint in ("/compare/", "/commits/"):
            for cause in (
                HTTPError(PRIVATE_URL, 502, "upstream failed", Message(), None),
                URLError(TimeoutError(PRIVATE_URL)),
                json.JSONDecodeError("invalid response", "", 0),
            ):
                with self.subTest(endpoint=endpoint, cause=type(cause).__name__):
                    paths: list[str] = []
                    failed_path = ""

                    def read(*, path: str, method: str = "GET", **_kwargs: object) -> object:
                        nonlocal failed_path
                        if method == "DELETE":
                            return None
                        paths.append(path)
                        if endpoint in path and not failed_path:
                            failed_path = path
                            raise click.ClickException(PRIVATE_URL) from cause
                        return github_read(path)

                    with (
                        patch("control_plane.release_review.github_api_request", side_effect=read),
                        self.assertLogs("control_plane.release_review", "WARNING") as logs,
                    ):
                        review = current_release_review(
                            control_plane_root=self.root,
                            record_store=self.store,
                            profile=web_profile(),
                            trace_id="trace-retry",
                        )
                    self.assertIsNone(review.unavailable_reason)
                    assert review.checklist is not None
                    self.assertEqual(review.checklist.candidate.source_commit, HEAD)
                    self.assertEqual(review.checklist.items[0].pull_request_number, 42)
                    self.assertEqual(paths.count(failed_path), 2)
                    self.assertEqual(len(paths), 3)
                    self.assertIn("trace_id=trace-retry", logs.output[0])
                    self.assertIn(failed_path.split("?", 1)[0], logs.output[0])
                    self.assertNotIn(PRIVATE_URL, " ".join(logs.output))

    def test_persistent_transient_failure_stops_after_second_get(self) -> None:
        def read(*, method: str = "GET", **_kwargs: object) -> object:
            if method == "DELETE":
                return None
            raise click.ClickException(PRIVATE_URL) from TimeoutError(PRIVATE_URL)

        with patch("control_plane.release_review.github_api_request", side_effect=read) as api:
            self.assertEqual(self.reason(web_profile()), "github_read_failed")
        reads = [call for call in api.call_args_list if call.kwargs.get("method", "GET") == "GET"]
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0], reads[1])
        self.assertEqual(api.call_args_list[-1].kwargs["method"], "DELETE")

    def test_authorization_missing_resource_and_quota_failures_are_not_retried(self) -> None:
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):

                def read(*, method: str = "GET", **_kwargs: object) -> object:
                    if method == "DELETE":
                        return None
                    raise click.ClickException(PRIVATE_URL) from HTTPError(
                        PRIVATE_URL, status, "denied", Message(), None
                    )

                with patch(
                    "control_plane.release_review.github_api_request", side_effect=read
                ) as api:
                    self.assertEqual(self.reason(web_profile()), "github_read_failed")
                reads = [
                    call for call in api.call_args_list if call.kwargs.get("method", "GET") == "GET"
                ]
                self.assertEqual(len(reads), 1)

    def test_unavailable_checklist_offers_reload_without_provider_details(self) -> None:
        review = unavailable_release_review("github_read_failed")
        self.assertIsNone(review.checklist)
        self.assertFalse(review.approved)
        self.assertIn("reload", " ".join(review.blockers).casefold())


if __name__ == "__main__":
    unittest.main()
