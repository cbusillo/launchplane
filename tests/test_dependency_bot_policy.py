from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from control_plane.contracts.merge_train_policy import MergeTrainEnqueuePolicy
from control_plane.dependency_update_review import dependency_update_policy, pure_dependency_update
from control_plane.merge_train_github import GitHubMergeTrainSnapshotReader
from tests.merge_train_policy_fixtures import (
    dependency_bot_policy,
    dependency_repository_policy,
    build_test_merge_train_policy_record,
)
from tests.support.merge_train_github import RecordingMergeTrainGitHubTransport
from tests.test_merge_train_dependency_updates import INDIRECT_PATCH, renovate_message
from tests.test_merge_train_github import _github_commit, _github_pull_request


class DependencyBotPolicyTests(unittest.TestCase):
    def test_add_remove_identity_and_change_allowed_types_without_code_changes(self) -> None:
        identity = 711
        for metadata_format, message in (
            ("dependabot_trailer", INDIRECT_PATCH),
            ("renovate_metadata", renovate_message()),
        ):
            for configured, allowed, expected in (
                (True, ("patch", "minor"), "patch_or_minor"),
                (False, ("patch", "minor"), "needs_review"),
                (True, ("digest",), "needs_review"),
            ):
                with self.subTest(
                    identity=identity,
                    metadata_format=metadata_format,
                    configured=configured,
                    allowed=allowed,
                ):
                    policy = dependency_repository_policy(identity, metadata_format=metadata_format)
                    bot = policy.enqueue.dependency_update_bots[0].model_copy(
                        update={"allowed_update_types": allowed}
                    )
                    policy.enqueue.dependency_update_bots = (bot,) if configured else ()
                    pull = _github_pull_request(16)
                    pull["user"] = {"id": identity, "login": "a-new-bot[bot]", "type": "Bot"}
                    transport = RecordingMergeTrainGitHubTransport(
                        responses=(
                            [_github_commit(identity, message, sha="head-16")],
                            [],
                            {"files": [{"filename": "package.json", "status": "modified"}]},
                        )
                    )
                    reader = GitHubMergeTrainSnapshotReader(
                        transport=transport, repository_policy=policy
                    )
                    self.assertEqual(
                        reader._safe_dependency_update_class(
                            repository_path=policy.repository,
                            pull_request_number=16,
                            author_id=identity,
                            pull_request=pull,
                            head_sha="head-16",
                            base_branch="main",
                            base_sha="base",
                        ),
                        expected,
                    )
                    self.assertEqual(
                        pure_dependency_update(
                            pull,
                            read=lambda _: {
                                "files": [{"filename": "uv.lock", "status": "modified"}]
                            },
                            repository_policy=policy,
                        ),
                        configured,
                    )

    def test_patch_only_policy_cannot_admit_inferred_or_mislabeled_minor_updates(self) -> None:
        from control_plane.merge_train_dependency_updates import classify_dependency_update
        from tests.test_merge_train_dependency_updates import SECURITY_GROUP_MINOR

        for metadata_format, message, expected in (
            ("dependabot_trailer", INDIRECT_PATCH, "patch_or_minor"),
            ("dependabot_trailer", SECURITY_GROUP_MINOR, "needs_review"),
            ("renovate_metadata", renovate_message(updateType="patch"), "needs_review"),
        ):
            with self.subTest(metadata_format=metadata_format, expected=expected):
                bot = dependency_bot_policy(711, metadata_format=metadata_format)
                self.assertEqual(
                    classify_dependency_update(
                        [message],
                        metadata_format=bot.metadata_format,
                        allowed_update_types=("patch",),
                    ),
                    expected,
                )

    def test_humans_and_forks_do_not_read_dependency_policy(self) -> None:
        def failed_read(**_: object) -> object:
            raise RuntimeError("policy store unavailable")

        store = SimpleNamespace(list_merge_train_policy_records=failed_read)
        pull = _github_pull_request(16)
        for changed in (
            pull | {"user": {"id": 711, "type": "User"}},
            pull
            | {
                "user": {"id": 711, "type": "Bot"},
                "head": {"sha": "head", "repo": {"full_name": "someone/fork"}},
            },
        ):
            with self.subTest(changed=changed):
                self.assertFalse(pure_dependency_update(changed, read=lambda _: None, store=store))

    def test_multiple_identities_share_one_format_and_legacy_ids_do_not_infer_it(self) -> None:
        policy = MergeTrainEnqueuePolicy(
            trusted_automation_github_user_ids=(711, 722),
            dependency_update_bots=(dependency_bot_policy(711), dependency_bot_policy(722)),
        )
        self.assertIsNotNone(policy.dependency_bot(711))
        self.assertIsNotNone(policy.dependency_bot(722))
        legacy = MergeTrainEnqueuePolicy(
            trusted_automation_github_user_ids=(711,), dependency_update_github_user_ids=(711,)
        )
        self.assertIsNone(legacy.dependency_bot(711))
        self.assertEqual(MergeTrainEnqueuePolicy.model_validate(legacy.model_dump()), legacy)

    def test_invalid_or_untrusted_bot_configuration_is_rejected(self) -> None:
        valid = dependency_bot_policy(711).model_dump()
        for entries, trusted in (
            ([valid], ()),
            ([valid, valid], (711,)),
            ([valid | {"metadata_format": "unknown"}], (711,)),
            ([valid | {"allowed_update_types": []}], (711,)),
            ([valid | {"allowed_update_types": ["major"]}], (711,)),
        ):
            with self.subTest(entries=entries, trusted=trusted), self.assertRaises(ValidationError):
                MergeTrainEnqueuePolicy.model_validate(
                    {
                        "trusted_automation_github_user_ids": trusted,
                        "dependency_update_bots": entries,
                    }
                )

    def test_client_exemption_uses_only_the_current_repository_branch_policy(self) -> None:
        target = dependency_repository_policy(711)
        record = build_test_merge_train_policy_record()
        record.policy.policies = (target,)
        store = SimpleNamespace(list_merge_train_policy_records=lambda **_: (record,))
        pull = _github_pull_request(16)
        self.assertIs(dependency_update_policy(pull, store=store), target.enqueue)
        for changed in (
            pull | {"base": {"ref": "other", "repo": {"full_name": target.repository}}},
            pull | {"base": {"ref": "main", "repo": {"full_name": "other/repository"}}},
        ):
            self.assertIsNone(dependency_update_policy(changed, store=store))
        self.assertIsNone(dependency_update_policy(pull, store=object()))
