import unittest
from unittest.mock import Mock, patch

from control_plane.lane_movement import LaneBuild, LaneMovementRefused, require_forward_build
from tests.test_product_reconcile import (
    DEPLOYABLE,
    OLDER,
    FakeGenericWebGitHub,
    _generic_web_profile,
    _digest,
)


class LaneMovementTests(unittest.TestCase):
    def test_real_transport_outage_is_mapped_before_preview_or_deploy_effects(self) -> None:
        from email.message import Message
        from urllib.error import HTTPError
        from control_plane.build_provenance import GitHubBuildProvenanceTransport
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from control_plane.lane_movement import require_forward_preview_build
        from tests.test_product_reconcile import _profile

        transport = GitHubBuildProvenanceTransport(token="test-token")
        candidates = (
            (
                self.current,
                LaneBuild(
                    "rebuild", DEPLOYABLE, self.profile.image.repository + "@sha256:" + "c" * 64
                ),
            ),
            (
                self.current,
                LaneBuild(
                    "tag",
                    "e" * 40,
                    self.profile.image.repository + "@sha256:" + "e" * 64,
                    deploy_reference=self.profile.image.repository + ":v-next",
                ),
            ),
            (LaneBuild("short", OLDER[:7]), self.current),
        )
        with patch(
            "control_plane.build_provenance.urlopen",
            side_effect=HTTPError("https://api.example.test", 503, "unavailable", Message(), None),
        ):
            for current, desired in candidates:
                with (
                    self.subTest(desired=desired.artifact_id),
                    self.assertRaises(LaneMovementRefused) as caught,
                ):
                    require_forward_build(
                        record_store=self.store,
                        profile=self.profile,
                        current=current,
                        desired=desired,
                        transport=transport,
                    )
                self.assertEqual(caught.exception.code, "source_order_unavailable")
            profile = LaunchplaneProductProfileRecord.model_validate(_profile())
            with (
                patch(
                    "control_plane.lane_movement.current_preview_builds",
                    return_value=(self.current,),
                ),
                self.assertRaises(LaneMovementRefused) as caught,
            ):
                require_forward_preview_build(
                    record_store=self.store,
                    profile=profile,
                    preview_slug="pr-5",
                    pull_request_number=5,
                    desired=LaneBuild(
                        "preview", "e" * 40, profile.image.repository + "@sha256:" + "e" * 64
                    ),
                    transport=transport,
                )
            self.assertEqual(caught.exception.code, "source_order_unavailable")

    def test_unique_abbreviated_recorded_source_can_move_forward(self) -> None:
        self.github.add_run(10, OLDER)
        self.github.add_run(20, DEPLOYABLE)
        original = self.github.get_json

        def read(path: str) -> object:
            if path.endswith(f"/commits/{OLDER[:7]}"):
                return {"sha": OLDER}
            return original(path)

        with patch.object(self.github, "get_json", side_effect=read):
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=LaneBuild(
                    "old", OLDER[:7], f"{self.profile.image.repository}@{_digest(OLDER)}"
                ),
                desired=self.current,
                transport=self.github,
            )

    def test_wrapped_token_outage_retries_but_invalid_key_does_not(self) -> None:
        from urllib.error import HTTPError
        from email.message import Message
        from control_plane.github_app_identity import GitHubAppIdentityError
        from control_plane.product_reconcile import ProductReconcileError

        for root, code in (
            (
                HTTPError("https://api.example.test", 503, "failed", Message(), None),
                "source_order_unavailable",
            ),
            (ValueError("invalid private key"), "source_authority_unavailable"),
        ):
            wrapped = GitHubAppIdentityError("token mint failed")
            wrapped.__cause__ = root
            error = ProductReconcileError("source token unavailable")
            error.__cause__ = wrapped
            with (
                self.subTest(code=code),
                patch(
                    "control_plane.product_reconcile.resolve_build_provenance_transport",
                    side_effect=error,
                ),
                self.assertRaises(LaneMovementRefused) as caught,
            ):
                require_forward_build(
                    record_store=self.store,
                    profile=self.profile,
                    current=self.current,
                    desired=LaneBuild("next", "e" * 40),
                )
            self.assertEqual(caught.exception.code, code)

    def test_missing_commit_is_terminal_and_source_outage_can_retry(self) -> None:
        from urllib.error import HTTPError
        from email.message import Message

        for status, expected in (
            (404, "source_order_unverified"),
            (503, "source_order_unavailable"),
        ):
            with (
                self.subTest(status=status),
                patch.object(
                    self.github,
                    "get_json",
                    side_effect=HTTPError(
                        "https://api.example.test", status, "failed", Message(), None
                    ),
                ),
                self.assertRaises(LaneMovementRefused) as caught,
            ):
                require_forward_build(
                    record_store=self.store,
                    profile=self.profile,
                    current=self.current,
                    desired=LaneBuild("next", "e" * 40),
                    transport=self.github,
                )
            self.assertEqual(caught.exception.code, expected)

    def setUp(self) -> None:
        self.profile = _generic_web_profile()
        self.store = Mock()
        self.store.read_artifact_manifest.side_effect = FileNotFoundError
        self.store.list_deployment_records.return_value = ()
        self.github = FakeGenericWebGitHub()
        self.current = LaneBuild(
            "current", DEPLOYABLE, f"{self.profile.image.repository}@{_digest(DEPLOYABLE)}"
        )

    def test_forward_commit_can_deploy(self) -> None:
        self.github.add_run(10, OLDER)
        self.github.add_run(20, DEPLOYABLE)
        require_forward_build(
            record_store=self.store,
            profile=self.profile,
            current=LaneBuild("old", OLDER, f"{self.profile.image.repository}@{_digest(OLDER)}"),
            desired=self.current,
            transport=self.github,
        )

    def test_descendant_claim_cannot_deploy_an_older_image_or_provider_tag(self) -> None:
        self.github.add_run(10, OLDER)
        self.github.add_run(20, DEPLOYABLE)
        old_image = f"{self.profile.image.repository}@{_digest(OLDER)}"
        for image, deploy_reference in (
            (old_image, ""),
            (self.current.image, f"{self.profile.image.repository}:sha-{OLDER}"),
        ):
            with (
                self.subTest(image=image, tag=deploy_reference),
                self.assertRaises(LaneMovementRefused) as caught,
            ):
                require_forward_build(
                    record_store=self.store,
                    profile=self.profile,
                    current=LaneBuild("old", OLDER, old_image),
                    desired=LaneBuild(
                        "candidate", DEPLOYABLE, image, deploy_reference=deploy_reference
                    ),
                    transport=self.github,
                )
            self.assertEqual(caught.exception.code, "build_identity_unverified")

    def test_preview_caller_provenance_cannot_pair_new_source_with_old_odoo_image(self) -> None:
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from control_plane.lane_movement import require_forward_preview_build
        from tests.test_product_reconcile import FakeGitHub, PR_HEAD, _profile

        profile = LaunchplaneProductProfileRecord.model_validate(_profile())
        github = FakeGitHub()
        github.add_run(20, PR_HEAD, event="pull_request")
        current = LaneBuild("serving", DEPLOYABLE, self.current.image, pull_request_number=5)
        desired = LaneBuild(
            "claimed", PR_HEAD, f"{profile.image.repository}@{_digest(OLDER)}", source_build=Mock()
        )
        with (
            patch("control_plane.lane_movement.current_preview_builds", return_value=(current,)),
            self.assertRaises(LaneMovementRefused) as caught,
        ):
            require_forward_preview_build(
                record_store=self.store,
                profile=profile,
                preview_slug="existing-preview",
                desired=desired,
                pull_request_number=5,
                transport=github,
            )
        self.assertEqual(caught.exception.code, "build_identity_unverified")

    def test_ancestor_is_refused_even_when_it_has_a_later_build(self) -> None:
        self.github.add_run(40, OLDER)
        with self.assertRaises(LaneMovementRefused) as caught:
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=self.current,
                desired=LaneBuild("older", OLDER),
                transport=self.github,
            )
        self.assertEqual(caught.exception.code, "ancestor_build")

    def test_incomplete_comparison_cannot_authorize_a_change(self) -> None:
        with (
            patch.object(self.github, "get_json", return_value={"status": "ahead"}),
            self.assertRaises(LaneMovementRefused),
        ):
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=LaneBuild("old", OLDER),
                desired=self.current,
                transport=self.github,
            )

    def test_same_commit_rebuild_requires_newer_provenance(self) -> None:
        for after_attempt in (1, 3):
            with self.subTest(after_attempt=after_attempt):
                before, after = Mock(), Mock()
                before.source_build.repository = after.source_build.repository = (
                    self.profile.repository
                )
                before.source_commit = after.source_commit = DEPLOYABLE
                before.source_build.run_id = after.source_build.run_id = 20
                before.source_build.run_attempt = 2
                after.source_build.run_attempt = after_attempt
                self.store.read_artifact_manifest.side_effect = lambda artifact_id: (
                    before if artifact_id == self.current.artifact_id else after
                )
                if after_attempt == 1:
                    with self.assertRaises(LaneMovementRefused) as caught:
                        require_forward_build(
                            record_store=self.store,
                            profile=self.profile,
                            current=self.current,
                            desired=LaneBuild("other", DEPLOYABLE),
                            transport=self.github,
                        )
                    self.assertEqual(caught.exception.code, "older_artifact")
                else:
                    require_forward_build(
                        record_store=self.store,
                        profile=self.profile,
                        current=self.current,
                        desired=LaneBuild("other", DEPLOYABLE),
                        transport=self.github,
                    )

    def test_missing_same_commit_build_history_is_refused(self) -> None:
        self.store.read_artifact_manifest.side_effect = FileNotFoundError
        with self.assertRaises(LaneMovementRefused):
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=self.current,
                desired=LaneBuild("other", DEPLOYABLE),
                transport=self.github,
            )

    def test_newer_source_cannot_replace_a_later_rebuilt_artifact_with_an_older_one(self) -> None:
        before, after = Mock(), Mock()
        before.repository = after.repository = self.profile.repository
        before.run_id, before.run_attempt = 50, 1
        after.run_id, after.run_attempt = 40, 1
        with self.assertRaises(LaneMovementRefused) as caught:
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=LaneBuild("before", OLDER, source_build=before),
                desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                transport=self.github,
            )
        self.assertEqual(caught.exception.code, "older_artifact")

    def test_diverged_preview_and_explicit_stable_build_require_newer_artifacts(
        self,
    ) -> None:
        before, after = Mock(), Mock()
        before.repository = after.repository = self.profile.repository
        before.run_id, before.run_attempt = 40, 1
        after.run_id, after.run_attempt = 50, 1
        comparison = {"status": "diverged", "base_commit": {"sha": OLDER}}
        with patch.object(self.github, "get_json", return_value=comparison):
            require_forward_build(
                record_store=self.store,
                profile=self.profile,
                current=LaneBuild("before", OLDER, source_build=before, pull_request_number=5),
                desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                transport=self.github,
            )
            after.run_id = 30
            with self.assertRaises(LaneMovementRefused):
                require_forward_build(
                    record_store=self.store,
                    profile=self.profile,
                    current=LaneBuild("before", OLDER, source_build=before),
                    desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                    transport=self.github,
                )

    def test_legacy_preview_same_commit_rebuild_needs_verified_later_start_time(self) -> None:
        after = Mock(repository=self.profile.repository, run_id=50, run_attempt=2)
        for started, allowed in (("2026-10-01T11:00:00Z", False), ("2026-10-01T13:00:00Z", True)):
            with (
                self.subTest(started=started),
                patch.object(
                    self.github,
                    "get_json",
                    return_value={
                        "id": 50,
                        "run_attempt": 2,
                        "head_sha": DEPLOYABLE,
                        "conclusion": "success",
                        "run_started_at": started,
                    },
                ),
            ):

                def require() -> None:
                    require_forward_build(
                        record_store=self.store,
                        profile=self.profile,
                        current=LaneBuild(
                            "before",
                            DEPLOYABLE,
                            observed_at="2026-10-01T12:00:00Z",
                            pull_request_number=5,
                        ),
                        desired=LaneBuild("after", DEPLOYABLE, source_build=after),
                        transport=self.github,
                    )

                if allowed:
                    require()
                else:
                    with self.assertRaises(LaneMovementRefused):
                        require()
