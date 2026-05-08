import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError

from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.merge_train_github import UrllibMergeTrainGitHubTransport


class GitHubMergeTrainClientTests(unittest.TestCase):
    def test_add_pull_request_label_uses_issue_labels_endpoint(self) -> None:
        transport = RecordingMergeTrainGitHubTransport()
        client = GitHubMergeTrainClient(transport=transport)

        client.add_pull_request_label(
            repository="cbusillo/sellyouroutboard",
            pull_request_number=42,
            label="merge-blocked",
        )

        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.path, "/repos/cbusillo/sellyouroutboard/issues/42/labels"
        )
        self.assertEqual(request.body, {"labels": ["merge-blocked"]})

    def test_update_pull_request_branch_uses_expected_head_sha(self) -> None:
        transport = RecordingMergeTrainGitHubTransport()
        client = GitHubMergeTrainClient(transport=transport)

        client.update_pull_request_branch(
            repository="cbusillo/sellyouroutboard",
            pull_request_number=42,
            expected_head_sha="head-42",
        )

        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(
            request.path, "/repos/cbusillo/sellyouroutboard/pulls/42/update-branch"
        )
        self.assertEqual(request.body, {"expected_head_sha": "head-42"})

    def test_merge_pull_request_uses_sha_guard_and_policy_method(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"sha": "merge-sha-42"},)
        )
        client = GitHubMergeTrainClient(transport=transport)

        merge_commit_sha = client.merge_pull_request(
            repository="cbusillo/sellyouroutboard",
            pull_request_number=42,
            head_sha="head-42",
            merge_method="merge",
        )

        self.assertEqual(merge_commit_sha, "merge-sha-42")
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.path, "/repos/cbusillo/sellyouroutboard/pulls/42/merge")
        self.assertEqual(request.body, {"sha": "head-42", "merge_method": "merge"})

    def test_merge_pull_request_requires_merge_response_sha(self) -> None:
        transport = RecordingMergeTrainGitHubTransport(responses=({"merged": True},))
        client = GitHubMergeTrainClient(transport=transport)

        with self.assertRaisesRegex(MergeTrainGitHubError, "merge commit SHA"):
            client.merge_pull_request(
                repository="cbusillo/sellyouroutboard",
                pull_request_number=42,
                head_sha="head-42",
                merge_method="merge",
            )

    def test_repository_must_be_owner_name(self) -> None:
        client = GitHubMergeTrainClient(transport=RecordingMergeTrainGitHubTransport())

        with self.assertRaisesRegex(ValueError, "owner/name"):
            client.add_pull_request_label(
                repository="cbusillo",
                pull_request_number=42,
                label="merge-blocked",
            )

    def test_transport_maps_conflict_to_stale_head_error(self) -> None:
        transport = UrllibMergeTrainGitHubTransport(token="token")
        http_error = HTTPError(
            url="https://api.github.com/repos/cbusillo/repo/pulls/1/merge",
            code=409,
            msg="Conflict",
            hdrs=Message(),
            fp=None,
        )

        with patch("control_plane.merge_train_github.urlopen", side_effect=http_error):
            with self.assertRaises(MergeTrainGitHubStaleHeadError) as caught:
                transport.request(method="PUT", path="/repos/cbusillo/repo/pulls/1/merge")

        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
