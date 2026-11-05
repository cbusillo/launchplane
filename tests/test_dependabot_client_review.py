from tests.merge_train_policy_fixtures import dependency_repository_policy
from types import SimpleNamespace
from typing import Any, cast
import unittest
import base64

from control_plane.merge_train_github import GitHubMergeTrainSnapshotReader
from control_plane.contracts.artifact_identity import ArtifactAddonSource
from tests.support.artifact_manifests import artifact_manifest_v2
from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
from tests.test_merge_train_github import _github_pull_request


REFERENCE_UPDATES = (
    {"depName": "docker/dockerfile", "currentValue": "1.6", "newValue": "1.28"},
    {"depName": "example/runtime", "currentValue": "3.13", "newValue": "3.14"},
    {"depName": "example/db", "currentValue": "17.1", "newValue": "17.2"},
    {"depName": "python", "currentValue": "3.13", "newValue": "3.14"},
    {
        "depName": "example/action",
        "currentValue": "v2",
        "newValue": "v2",
        "currentDigest": "a" * 40,
        "newDigest": "b" * 40,
    },
)


class DependabotClientReviewTests(unittest.TestCase):
    def test_client_repository_dependency_update_needs_no_notes_or_review(self) -> None:
        pull = _github_pull_request(16)
        pull["body"] = "Bumps a dependency."
        pull["labels"] = []
        pull["user"] = {"id": 49699333, "login": "dependabot[bot]", "type": "Bot"}
        profile = SimpleNamespace(
            release_on_acceptance="held",
            is_active=True,
            repository="cbusillo/sellyouroutboard",
            preview=SimpleNamespace(enabled=False, context=""),
            owner=SimpleNamespace(is_set=True, review_label="owner-review"),
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"files": [{"filename": "package.json", "status": "modified"}]},)
        )
        reader = GitHubMergeTrainSnapshotReader(
            transport=transport,
            repository_policy=dependency_repository_policy(),
            branch_refresh_store=cast(
                Any, SimpleNamespace(list_product_profile_records=lambda: (profile,))
            ),
        )
        snapshot = reader._pull_request_snapshot(
            repository_path=profile.repository, pull_request=pull, enrich=False
        )
        self.assertFalse(snapshot.client_test_notes_error)
        self.assertFalse(snapshot.owner_review_required)

    def test_only_complete_dependency_changes_qualify(self) -> None:
        login = "configured-update-bot"
        from control_plane.dependency_update_review import pure_dependency_update

        pull = _github_pull_request(16)
        pull["labels"] = []
        pull["user"] = {"id": 49699333, "login": login, "type": "Bot"}
        good = [
            {"filename": "frontend/package.json", "status": "modified"},
            {"filename": "frontend/pnpm-lock.yaml", "status": "modified"},
        ]
        for changes, expected in (
            (good, True),
            ([{"filename": "uv.lock", "status": "modified"}], True),
            ([{"filename": "requirements.txt", "status": "modified"}], True),
            (good + [{"filename": "app.py", "status": "modified"}], False),
            ([{"filename": ".github/workflows/ci.yml", "status": "modified"}], False),
            ([{"filename": "Dockerfile", "status": "modified"}], False),
            (
                [{"filename": "package.json", "status": "renamed", "previous_filename": "app.py"}],
                False,
            ),
            ([], False),
            (good * 150, False),
            ([{"filename": "package.json"}], False),
        ):
            with self.subTest(changes=changes[:2], count=len(changes)):
                self.assertEqual(
                    pure_dependency_update(
                        pull,
                        read=lambda _: {"files": changes},
                        repository_policy=dependency_repository_policy(),
                    ),
                    expected,
                )
        for update in (
            {"user": {"login": "agent[bot]", "type": "Bot"}},
            {"user": {"login": "person", "type": "User"}},
            {"user": {"login": login, "type": "User"}},
            {"head": {"sha": "new", "repo": {"full_name": "someone/fork"}}},
            {"changed_files": 3},
        ):
            with self.subTest(update=update):
                self.assertFalse(
                    pure_dependency_update(
                        pull | update,
                        read=lambda _: {"files": good},
                        repository_policy=dependency_repository_policy(),
                    )
                )

        def refused(_: str) -> object:
            raise ValueError("read refused")

        self.assertFalse(
            pure_dependency_update(
                pull, read=refused, repository_policy=dependency_repository_policy()
            )
        )
        self.assertFalse(
            pure_dependency_update(
                pull,
                read=lambda _: {"message": "unavailable"},
                repository_policy=dependency_repository_policy(),
            )
        )

    def test_workspace_only_exempts_devkit_runtime_pins_with_complete_immutable_files(self) -> None:
        from control_plane.dependency_update_review import pure_dependency_update

        for slot, extra, main_changed, expected in (
            ("devkit", "", False, True),
            ("runtime", "", False, True),
            ("shared_addons", "", False, False),
            ("devkit", '\ncommand = "new"', False, False),
            ("devkit", "", True, True),
            ("runtime", "", True, True),
        ):
            with self.subTest(slot=slot, extra=extra):
                old = f'[repos.{slot}]\nrepository = "example/tool"\nref = "' + "a" * 40 + '"'
                new = old.replace("a" * 40, "b" * 40) + extra
                pull = _github_pull_request(16)
                pull["user"] = {"id": 49699333, "login": "any-bot", "type": "Bot"}
                base_sha = cast(dict[str, str], pull["base"])["sha"]

                def read(path: str) -> object:
                    if "/compare/" in path:
                        return {
                            "files": [{"filename": "workspace.toml", "status": "modified"}],
                            "base_commit": {"sha": base_sha},
                            "merge_base_commit": {"sha": "original-parent"},
                        }
                    content = new if path.endswith("ref=head-16") else old
                    if main_changed and path.endswith("ref=" + base_sha):
                        content += '\n[workspace]\nname = "current main"'
                    return {
                        "encoding": "base64",
                        "content": base64.b64encode(content.encode()).decode(),
                        "size": len(content.encode()),
                    }

                self.assertEqual(
                    pure_dependency_update(
                        pull, read=read, repository_policy=dependency_repository_policy()
                    ),
                    expected,
                )

    def test_reference_admission_preserves_client_notes_holds_and_visible_review(self) -> None:
        from control_plane.client_test_notes import NOTHING_TO_TEST

        policy = dependency_repository_policy()
        profile = SimpleNamespace(
            release_on_acceptance="held",
            is_active=True,
            repository=policy.repository,
            preview=SimpleNamespace(enabled=False, context=""),
            owner=SimpleNamespace(is_set=True, review_label="owner-review"),
        )
        for body, expected_review, expected_error in (
            (f"## Client test notes\n\n{NOTHING_TO_TEST}: dependency update.", False, False),
            ("", True, True),
            (
                "## Client test notes\n\nClient change: New catalog behavior.\n- Check catalog.",
                True,
                False,
            ),
        ):
            with self.subTest(body=body):
                pull = _github_pull_request(16)
                pull["user"] = {"id": 49699333, "login": "configured-bot", "type": "Bot"}
                transport = RecordingMergeTrainGitHubTransport(
                    responses=(
                        {"files": [{"filename": ".github/workflows/ci.yml", "status": "modified"}]},
                    )
                )
                reader = GitHubMergeTrainSnapshotReader(
                    transport=transport,
                    repository_policy=policy,
                    branch_refresh_store=cast(
                        Any, SimpleNamespace(list_product_profile_records=lambda: (profile,))
                    ),
                )
                intent = reader.client_review_intent(
                    labels=(), repository=policy.repository, body=body, pull_request=pull
                )
                self.assertEqual(intent.review_required, expected_review)
                self.assertEqual(bool(intent.error), expected_error)

    def test_configured_review_override_still_requires_notes(self) -> None:
        from control_plane.client_test_notes import change_client_review

        intent = change_client_review(
            body="",
            labels=["custom-review"],
            review_label="custom-review",
            pure_dependency_update=True,
        )
        self.assertTrue(intent.error)
        self.assertTrue(intent.review_required)

    @staticmethod
    def _reference_change(
        filename: str,
        old: str,
        new: str,
        *,
        upstream_status: str = "ahead",
        declared_updates: tuple[dict[str, str], ...] | None = REFERENCE_UPDATES,
        current_base: str | None = None,
    ) -> bool:
        from control_plane.dependency_update_review import pure_dependency_update

        pull = _github_pull_request(16)
        pull["user"] = {"id": 49699333, "login": "configured-update-bot", "type": "Bot"}
        head_sha = cast(dict[str, str], pull["head"])["sha"]
        base_sha = cast(dict[str, str], pull["base"])["sha"]

        def read(path: str) -> object:
            if path.startswith("/repos/example/action/compare/"):
                return {
                    "status": upstream_status,
                    "base_commit": {"sha": "a" * 40},
                    "merge_base_commit": {"sha": "a" * 40},
                    "commits": [{"sha": "b" * 40}],
                }
            if "/compare/" in path:
                return {
                    "files": [{"filename": filename, "status": "modified"}],
                    "base_commit": {"sha": base_sha},
                    "merge_base_commit": {"sha": "original-parent"},
                }
            content = (
                new
                if path.endswith("ref=" + head_sha)
                else current_base
                if path.endswith("ref=" + base_sha) and current_base is not None
                else old
            )
            encoded = content.encode()
            return {
                "encoding": "base64",
                "content": base64.b64encode(encoded).decode(),
                "size": len(encoded),
            }

        return pure_dependency_update(
            pull,
            read=read,
            repository_policy=dependency_repository_policy(),
            declared_updates=declared_updates,
        )

    def test_container_references_require_forward_versions_and_unchanged_instructions(self) -> None:
        old = "# syntax=docker/dockerfile:1.6\nFROM example/runtime:3.13 AS build\nRUN app\n"
        new = old.replace(":1.6", ":1.28").replace(":3.13", ":3.14")
        self.assertTrue(self._reference_change("docker/artifact.Dockerfile", old, new))
        for changed in (new.replace("RUN app", "RUN other"), old.replace(":1.6", ":1.5")):
            with self.subTest(changed=changed):
                self.assertFalse(self._reference_change("Dockerfile", old, changed))
        compose = "services:\n  db:\n    image: example/db:17.1\n    command: start\n"
        self.assertTrue(
            self._reference_change("docker/compose.yml", compose, compose.replace(":17.1", ":17.2"))
        )
        self.assertFalse(
            self._reference_change("docker/compose.yml", compose, compose.replace(":17.1", ":18.0"))
        )

    def test_workflow_action_digests_require_forward_upstream_ancestry(self) -> None:
        old = (
            "on: [push]\njobs:\n  test:\n    steps:\n      - uses: example/action@"
            + "a" * 40
            + " # v2.1\n"
        )
        new = old.replace("a" * 40, "b" * 40).replace("# v2.1", "# v2.2")
        self.assertTrue(self._reference_change(".github/workflows/ci.yml", old, new))
        for updates in (None, ()):
            with self.subTest(updates=updates):
                self.assertFalse(
                    self._reference_change(
                        ".github/workflows/ci.yml", old, new, declared_updates=updates
                    )
                )
        self.assertFalse(
            self._reference_change(
                ".github/workflows/ci.yml",
                old,
                new,
                declared_updates=({"updateType": "digest", "currentValue": "main"},),
            )
        )

        for status in ("behind", "diverged", "identical"):
            with self.subTest(status=status):
                self.assertFalse(
                    self._reference_change(
                        ".github/workflows/ci.yml", old, new, upstream_status=status
                    )
                )
        self.assertFalse(
            self._reference_change(
                ".github/workflows/ci.yml", old, new + "permissions: write-all\n"
            )
        )
        self.assertFalse(
            self._reference_change(
                ".github/workflows/ci.yml",
                old,
                new.replace("jobs:\n", "jobs: {}\njobs:\n"),
            )
        )

    def test_reference_proof_uses_pr_merge_base_after_unrelated_main_changes(self) -> None:
        old = "# syntax=docker/dockerfile:1.6\nRUN original\n"
        self.assertTrue(
            self._reference_change(
                "Dockerfile",
                old,
                old.replace(":1.6", ":1.28"),
                current_base=old.replace("RUN original", "RUN current"),
            )
        )

    def test_workflow_inputs_and_service_images_cannot_hide_other_changes(self) -> None:
        old = (
            "jobs:\n  test:\n    services:\n      db:\n        image: example/db:17.1\n"
            "    steps:\n      - uses: example/action@v2\n"
            "        with:\n          python-version: '3.13'\n      - run: app\n"
        )
        new = old.replace(":17.1", ":17.2").replace("'3.13'", "'3.14'")
        self.assertTrue(self._reference_change(".github/workflows/ci.yml", old, new))
        for changed in (
            new.replace("run: app", "run: other"),
            new.replace("example/db", "other/db"),
        ):
            with self.subTest(changed=changed):
                self.assertFalse(self._reference_change(".github/workflows/ci.yml", old, changed))

    def test_unknown_digests_branch_pins_and_unrelated_yaml_stay_for_review(self) -> None:
        image = "services:\n  db:\n    image: example/db:17@sha256:" + "a" * 64 + "\n"
        self.assertFalse(
            self._reference_change("compose.yml", image, image.replace("a" * 64, "b" * 64))
        )
        action = "jobs:\n  test:\n    steps:\n      - uses: example/action@main\n"
        self.assertFalse(
            self._reference_change(
                ".github/workflows/ci.yml", action, action.replace("@main", "@next")
            )
        )
        self.assertFalse(
            self._reference_change(
                "settings.yml", "image: example/db:17.1", "image: example/db:17.2"
            )
        )

    def test_shared_dependency_repository_uses_consuming_client_profile(self) -> None:
        pull = _github_pull_request(16)
        pull["labels"] = []
        pull["body"] = "Bumps a dependency."
        pull["user"] = {"login": "dependabot[bot]", "type": "Bot", "id": 49699333}
        profile = SimpleNamespace(
            release_on_acceptance="held",
            is_active=True,
            repository="example/site",
            driver_id="odoo",
            preview=SimpleNamespace(enabled=False, context=""),
            owner=SimpleNamespace(is_set=True, review_label="owner-review"),
            lanes=(SimpleNamespace(instance="prod", context="site"),),
        )
        artifact = artifact_manifest_v2()
        artifact.addon_sources = (
            ArtifactAddonSource(repository="cbusillo/sellyouroutboard", ref=artifact.source_commit),
        )
        store = SimpleNamespace(
            list_product_profile_records=lambda: (profile,),
            read_release_tuple_record=lambda **_: SimpleNamespace(artifact_id="live"),
            read_artifact_manifest=lambda _: artifact,
        )
        reader = GitHubMergeTrainSnapshotReader(
            transport=RecordingMergeTrainGitHubTransport(
                responses=({"files": [{"filename": "requirements.txt", "status": "modified"}]},)
            ),
            branch_refresh_store=cast(Any, store),
            repository_policy=dependency_repository_policy(),
        )
        snapshot = reader._pull_request_snapshot(
            repository_path="cbusillo/sellyouroutboard", pull_request=pull, enrich=False
        )
        self.assertFalse(snapshot.client_test_notes_error)
        self.assertFalse(snapshot.owner_review_required)

    def test_pure_bot_notes_need_explicit_label_to_request_client_review(self) -> None:
        from control_plane.client_test_notes import change_client_review

        body = "## Client test notes\nCheck checkout after the SDK update."
        intent = change_client_review(
            body=body, labels=(), review_label="owner-review", pure_dependency_update=True
        )
        self.assertFalse(intent.review_required)
        requested = change_client_review(
            body=body,
            labels=("owner-review",),
            review_label="owner-review",
            pure_dependency_update=True,
        )
        self.assertTrue(requested.review_required)
        self.assertFalse(requested.error)
