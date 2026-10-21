import unittest
import base64
from hashlib import sha1
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError
from email.message import Message
from unittest.mock import Mock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.updater_proposal import (
    UPDATER_PROPOSAL_MAX_BODY_BYTES,
    UPDATER_PROPOSAL_MAX_CHANGE_BYTES,
    UPDATER_PROPOSAL_MAX_FILES,
    UPDATER_PROPOSAL_MAX_EDITS,
    UPDATER_PROPOSAL_MAX_FILE_BYTES,
    UPDATER_PROPOSAL_MAX_MATERIALIZED_BYTES,
    UPDATER_PROPOSAL_ROUTE,
    UpdaterProposalRequest,
    UpdaterProposalResponse,
)
from control_plane.github_app_identity import GitHubAppIdentity
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubOidcVerifier,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.updater_proposal import (
    UpdaterProposalRefused,
    UpdaterProposalConflict,
    authorize_updater_proposal,
    open_updater_proposal,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request as http_request

BASE_SHA = "a" * 40
TREE_SHA = "b" * 40
HEAD_SHA = "c" * 40
REPOSITORY = "example/updater"
WORKFLOW = REPOSITORY + "/.github/workflows/update.yml@refs/heads/main"


def updater_identity() -> GitHubActionsIdentity:
    return replace(
        _identity(repository=REPOSITORY, workflow_ref=WORKFLOW, event_name="schedule"),
        sha=BASE_SHA,
        raw_claims={"exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())},
    )


def updater_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        schema_version=2,
        github_actions=(
            GitHubActionsPolicyRule(
                managed_set_id="updater-proposals.test",
                managed_rule_id="approved-updater",
                repository=REPOSITORY,
                repository_id="1001",
                repository_owner_id="2001",
                workflow_refs=(WORKFLOW,),
                refs=("refs/heads/main",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("updater_proposal.create",),
            ),
        ),
    )


def proposal_payload() -> dict[str, Any]:
    return {
        "repository": REPOSITORY,
        "base_sha": BASE_SHA,
        "title": "Update dependencies",
        "files": [{"path": "requirements.txt", "content": "example==2\n"}],
    }


def blob_sha(content: bytes) -> str:
    return sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def sparse_change(path: str, content: bytes, edits: list[dict[str, Any]]) -> dict[str, Any]:
    return {"path": path, "base_blob_sha": blob_sha(content), "edits": edits}


class UpdaterAuthorizationTests(unittest.TestCase):
    def test_refuses_other_repository_workflow_ref_identity_and_unsafe_events(self) -> None:
        original = updater_identity()
        cases: tuple[dict[str, Any], ...] = (
            {"repository": "example/other"},
            {"workflow_ref": WORKFLOW.replace("update.yml", "other.yml")},
            {"ref": "refs/heads/feature"},
            {"repository_id": "9999"},
            {"repository_owner_id": "9999"},
            {"event_name": "pull_request_target"},
            {"event_name": "pull_request"},
            {"sha": HEAD_SHA},
            {"ref_type": "tag"},
            {"workflow_ref": "example/reusable/.github/workflows/update.yml@refs/heads/main"},
            {"raw_claims": {}},
            {"raw_claims": {"exp": 1}},
        )
        request = UpdaterProposalRequest.model_validate(proposal_payload())
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(UpdaterProposalRefused):
                authorize_updater_proposal(
                    identity=replace(original, **changes), policy=updater_policy(), request=request
                )
        authorize_updater_proposal(identity=original, policy=updater_policy(), request=request)

    def test_refuses_wildcard_grants_and_missing_immutable_ids(self) -> None:
        request = UpdaterProposalRequest.model_validate(proposal_payload())
        rule = updater_policy().github_actions[0]
        for changes in (
            {"workflow_refs": ("*",)},
            {"refs": ("*",)},
            {"actions": ("*",)},
            {"repository_id": "", "repository_owner_id": ""},
            {"products": ("*",)},
            {"contexts": ("*",)},
        ):
            with self.subTest(changes=changes), self.assertRaises(UpdaterProposalRefused):
                authorize_updater_proposal(
                    identity=updater_identity(),
                    request=request,
                    policy=LaunchplaneAuthzPolicy(
                        schema_version=2, github_actions=(rule.model_copy(update=changes),)
                    ),
                )

    def test_refuses_oversize_unsafe_paths_and_mutation_selectors(self) -> None:
        cases: list[dict[str, Any]] = [
            {
                "files": [
                    {"path": "a", "content": "é" * (UPDATER_PROPOSAL_MAX_CHANGE_BYTES // 2 + 1)}
                ]
            },
            {
                "files": [
                    {"path": str(index), "content": "x"}
                    for index in range(UPDATER_PROPOSAL_MAX_FILES + 1)
                ]
            },
            {"branch": "main"},
            {"labels": ["ready-to-merge"]},
            {"pr_number": 1},
            {"merge": True},
        ]
        for path in (
            "../a",
            "/a",
            "a/../b",
            ".git/config",
            "a\\b",
            ".github/workflows/evil.yml",
            "a//b",
            "a\x00b",
        ):
            cases.append({"files": [{"path": path, "content": "x"}]})
        cases.append({"files": [{"path": "a", "content": "x"}, {"path": "a", "content": None}]})
        for changes in cases:
            with self.subTest(changes=list(changes)), self.assertRaises(ValidationError):
                UpdaterProposalRequest.model_validate({**proposal_payload(), **changes})

    def test_allow_list_round_trips_existing_policy_records(self) -> None:
        policy = updater_policy()
        record = LaunchplaneAuthzPolicyRecord(
            record_id="updater-policy",
            source="test",
            updated_at=datetime.now(UTC).isoformat(),
            policy=policy,
        )
        store = PostgresRecordStore(database_url="sqlite+pysqlite:///:memory:")
        store.ensure_schema()
        try:
            store.seed_authz_policy_if_absent(record)
            loaded = store.list_authz_policy_records(status="active")[0]
            authorize_updater_proposal(
                identity=updater_identity(),
                policy=loaded.policy,
                request=UpdaterProposalRequest.model_validate(proposal_payload()),
            )
        finally:
            store.close()


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.default_branch = "main"
        self.base_sha = BASE_SHA
        self.tree_sha = TREE_SHA
        self.fail_path = ""
        self.overgrant = False
        self.base_file_size = 12
        self.truncated = False
        self.created_refs: set[str] = set()
        self.prs: list[dict[str, Any]] = []
        self.closed_prs: list[dict[str, Any]] = []
        self.base_mode = "100644"
        self.lose_pr_response = False
        self.lose_ref_response = False

    def __call__(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        path = kwargs["path"]
        if path == self.fail_path:
            raise ValueError("private provider response installation-token-secret")
        if path == "/app":
            return {"id": 42, "slug": "approved-app"}
        if path.endswith("/installation"):
            return {
                "id": 77,
                "app_id": 42,
                "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
            }
        if path == "/app/installations/77/access_tokens":
            return {
                "token": "installation-token-secret",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "permissions": {
                    "contents": "write",
                    "pull_requests": "write",
                    "metadata": "read",
                    **({"workflows": "write"} if self.overgrant else {}),
                },
                "repositories": [{"id": 1001, "full_name": REPOSITORY}],
            }
        if path == "/users/approved-app%5Bbot%5D":
            return {"id": 5001}
        if "/pulls?state=open" in path:
            return self.prs
        if "/pulls?state=closed" in path:
            return self.closed_prs
        if path == "/installation/token":
            return None
        if path == "/repos/" + REPOSITORY:
            return {
                "id": 1001,
                "full_name": REPOSITORY,
                "owner": {"id": 2001},
                "default_branch": self.default_branch,
            }
        if "/git/ref/heads/" in path:
            branch = path.split("/git/ref/heads/", 1)[1]
            if branch == "main":
                return {"object": {"sha": self.base_sha}}
            if "refs/heads/" + branch in self.created_refs:
                return {"object": {"sha": HEAD_SHA}}
            raise HTTPError(path, 404, "Missing ref", Message(), None)
        if path.endswith("/git/commits/" + HEAD_SHA):
            return {"tree": {"sha": self.tree_sha}, "parents": [{"sha": BASE_SHA}]}
        if path.endswith("/git/commits/" + BASE_SHA):
            return {"tree": {"sha": TREE_SHA}}
        if path.endswith("?recursive=1"):
            return {
                "truncated": self.truncated,
                "tree": [
                    {
                        "path": "requirements.txt",
                        "type": "blob",
                        "size": self.base_file_size,
                        "mode": self.base_mode,
                    },
                    {"path": "sources", "type": "tree"},
                ],
            }
        if path.endswith("/git/trees"):
            return {"sha": self.tree_sha}
        if path.endswith("/git/commits"):
            return {"sha": HEAD_SHA}
        if path.endswith("/git/refs"):
            ref = kwargs["body"]["ref"]
            if ref in self.created_refs:
                raise ValueError("Ref already exists")
            self.created_refs.add(ref)
            if self.lose_ref_response:
                raise ValueError("Ref response lost")
            return {"ref": ref}
        if path.endswith("/pulls"):
            result = {
                "number": 17,
                "html_url": "https://github.com/" + REPOSITORY + "/pull/17",
                "head": {"ref": kwargs["body"]["head"], "sha": HEAD_SHA, "repo": {"id": 1001}},
                "user": {"id": 5001},
                "base": {"ref": kwargs["body"]["base"]},
            }
            self.prs.append(result)
            if self.lose_pr_response:
                raise ValueError("PR response lost installation-token-secret")
            return result
        raise AssertionError(path)


class SparseFakeGitHub(FakeGitHub):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files
        self.blob_response_changes: dict[str, object] = {}
        self.tree_size_override: int | None = None
        self.tree_sha = "d" * 40

    def __call__(self, **kwargs: Any) -> object:
        path = kwargs["path"]
        if path == self.fail_path:
            return super().__call__(**kwargs)
        if path.endswith("?recursive=1"):
            self.calls.append(kwargs)
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": name,
                        "type": "blob",
                        "size": len(content)
                        if self.tree_size_override is None
                        else self.tree_size_override,
                        "mode": self.base_mode,
                        "sha": blob_sha(content),
                    }
                    for name, content in self.files.items()
                ],
            }
        if "/git/blobs/" in path:
            self.calls.append(kwargs)
            sha = path.rsplit("/", 1)[1]
            content = next(content for content in self.files.values() if blob_sha(content) == sha)
            return {
                "sha": sha,
                "size": len(content),
                "encoding": "base64",
                "content": base64.encodebytes(content).decode(),
                **self.blob_response_changes,
            }
        return super().__call__(**kwargs)


class UpdaterProviderTests(unittest.TestCase):
    key: str

    @classmethod
    def setUpClass(cls) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.key = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    def invoke(self, api: FakeGitHub, payload: dict[str, Any] | None = None) -> Any:
        with patch(
            "control_plane.updater_proposal.resolve_delivery_github_app_identity",
            return_value=GitHubAppIdentity(app_id=42, private_key=self.key),
        ):
            return open_updater_proposal(
                identity=updater_identity(),
                request=UpdaterProposalRequest.model_validate(payload or proposal_payload()),
                control_plane_root=Path("."),
                api_request=api,
            )

    def test_only_new_branch_and_pr_with_exact_scoped_token_and_revocation(self) -> None:
        api = FakeGitHub()
        api.tree_sha = "d" * 40
        result = self.invoke(api)
        mint = next(call for call in api.calls if call["path"].endswith("/access_tokens"))
        self.assertEqual(
            mint["body"],
            {
                "repository_ids": [1001],
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
        )
        writes = [
            call
            for call in api.calls
            if call.get("method") == "POST" and call["path"].startswith("/repos/")
        ]
        self.assertEqual(
            [call["path"] for call in writes],
            [
                "/repos/" + REPOSITORY + suffix
                for suffix in ("/git/trees", "/git/commits", "/git/refs", "/pulls")
            ],
        )
        self.assertEqual(writes[1]["body"]["parents"], [BASE_SHA])
        self.assertEqual(writes[2]["body"], {"ref": "refs/heads/" + result.branch, "sha": HEAD_SHA})
        self.assertEqual(writes[3]["body"]["base"], "main")
        self.assertEqual(result.pull_request_number, 17)
        self.assertNotIn("installation-token-secret", result.model_dump_json())
        self.assertEqual(api.calls[-1]["path"], "/installation/token")
        self.assertEqual(api.calls[-1]["method"], "DELETE")

    def test_identical_transport_retry_never_creates_another_branch_or_pr(self) -> None:
        api = FakeGitHub()
        api.tree_sha = "d" * 40
        first = self.invoke(api)
        replay = self.invoke(api)
        self.assertEqual(replay.disposition, "replayed")
        self.assertEqual(replay.pull_request_number, first.pull_request_number)
        self.assertEqual(api.created_refs, {"refs/heads/" + first.branch})
        self.assertEqual(sum(call["path"].endswith("/pulls") for call in api.calls), 1)
        self.assertEqual(api.calls[-1]["method"], "DELETE")

    def test_foreign_prefix_prs_are_ignored_and_closed_identical_proposals_are_not_reopened(
        self,
    ) -> None:
        api = FakeGitHub()
        api.tree_sha = "d" * 40
        first = self.invoke(api)
        real = api.prs[0]
        api.prs.insert(
            0, {**real, "head": {**real["head"], "repo": {"id": 9999}}, "user": {"id": 5002}}
        )
        api.prs.insert(0, {**real, "head": {**real["head"], "repo": None}, "user": {"id": 5002}})
        replay = self.invoke(api)
        self.assertEqual(replay.pull_request_number, first.pull_request_number)
        self.assertEqual(replay.disposition, "replayed")
        api.prs = []
        api.closed_prs = [{**real, "head": {**real["head"], "ref": "unrelated"}}, real]
        closed = self.invoke(api)
        self.assertEqual(closed.pull_request_number, first.pull_request_number)
        self.assertEqual(sum(call["path"].endswith("/pulls") for call in api.calls), 1)

    def test_pending_proposal_survives_a_moved_default_branch_without_duplication(self) -> None:
        api = FakeGitHub()
        api.tree_sha = "d" * 40
        first = self.invoke(api)
        api.base_sha = HEAD_SHA
        with patch(
            "control_plane.updater_proposal.resolve_delivery_github_app_identity",
            return_value=GitHubAppIdentity(app_id=42, private_key=self.key),
        ):
            pending = open_updater_proposal(
                identity=replace(updater_identity(), sha=HEAD_SHA),
                request=UpdaterProposalRequest.model_validate(
                    {**proposal_payload(), "base_sha": HEAD_SHA}
                ),
                control_plane_root=Path("."),
                api_request=api,
            )
        self.assertEqual(pending.disposition, "pending")
        self.assertEqual(pending.pull_request_number, first.pull_request_number)
        self.assertEqual(len(api.prs), 1)

    def test_recovers_a_created_branch_and_a_lost_pr_response(self) -> None:
        api = FakeGitHub()
        api.tree_sha = "d" * 40
        api.fail_path = "/repos/" + REPOSITORY + "/pulls"
        with self.assertRaises(ValueError):
            self.invoke(api)
        api.fail_path = ""
        api.base_sha = "e" * 40
        recovered = self.invoke(api)
        self.assertEqual(recovered.pull_request_number, 17)
        self.assertEqual(sum(call["path"].endswith("/git/refs") for call in api.calls), 1)
        lost = FakeGitHub()
        lost.tree_sha = "d" * 40
        lost.lose_pr_response = True
        self.assertEqual(self.invoke(lost).disposition, "replayed")
        self.assertEqual(len(lost.prs), 1)
        lost_ref = FakeGitHub()
        lost_ref.tree_sha = "d" * 40
        lost_ref.lose_ref_response = True
        self.assertEqual(self.invoke(lost_ref).pull_request_number, 17)
        self.assertEqual(len(lost_ref.created_refs), 1)

    def test_preserves_executable_mode_and_refuses_symlinks_and_wrong_app_author(self) -> None:
        api = FakeGitHub()
        api.tree_sha = "d" * 40
        api.base_mode = "100755"
        self.invoke(api)
        tree_write = next(call for call in api.calls if call["path"].endswith("/git/trees"))
        self.assertEqual(tree_write["body"]["tree"][0]["mode"], "100755")
        api.prs[0]["user"]["id"] = 5002
        with self.assertRaises(UpdaterProposalConflict):
            self.invoke(api)
        symlink = FakeGitHub()
        symlink.base_mode = "120000"
        with self.assertRaises(UpdaterProposalRefused):
            self.invoke(symlink)
        self.assertFalse(
            any(
                call.get("method") == "POST" and call["path"].startswith("/repos/")
                for call in symlink.calls
            )
        )

    def test_revocation_failure_keeps_the_confirmed_pr_and_logs_no_secret(self) -> None:
        api = FakeGitHub()
        api.tree_sha = "d" * 40
        api.fail_path = "/installation/token"
        with self.assertLogs("control_plane.updater_proposal", level="WARNING") as captured:
            result = self.invoke(api)
        self.assertEqual(result.pull_request_number, 17)
        self.assertNotIn("installation-token-secret", "".join(captured.output))

    def test_refuses_directory_replacement_and_missing_file_deletion(self) -> None:
        for change in (
            {"path": "sources", "content": "replacement"},
            {"path": "missing", "content": None},
            {"path": "requirements.txt/nested", "content": "child"},
        ):
            api = FakeGitHub()
            payload = {**proposal_payload(), "files": [change]}
            with (
                patch(
                    "control_plane.updater_proposal.resolve_delivery_github_app_identity",
                    return_value=GitHubAppIdentity(app_id=42, private_key=self.key),
                ),
                self.subTest(change=change),
                self.assertRaises(UpdaterProposalRefused),
            ):
                open_updater_proposal(
                    identity=updater_identity(),
                    request=UpdaterProposalRequest.model_validate(payload),
                    control_plane_root=Path("."),
                    api_request=api,
                )
            self.assertFalse(any(call["path"].endswith("/git/refs") for call in api.calls))

    def test_refuses_changed_default_branch_stale_base_and_empty_changes(self) -> None:
        for change in (
            {"default_branch": "trunk"},
            {"base_sha": HEAD_SHA},
            {"tree_sha": TREE_SHA},
            {"truncated": True},
            {"base_file_size": UPDATER_PROPOSAL_MAX_CHANGE_BYTES + 1},
        ):
            api = FakeGitHub()
            api.tree_sha = "d" * 40
            for key, value in change.items():
                setattr(api, key, value)
            with (
                self.subTest(change=change),
                self.assertRaises((UpdaterProposalRefused, UpdaterProposalConflict)),
            ):
                self.invoke(api)
            self.assertFalse(
                any(call["path"].endswith(("/git/refs", "/pulls")) for call in api.calls)
            )
            self.assertEqual(api.calls[-1]["method"], "DELETE")

    def test_revokes_after_provider_failure_and_overbroad_token(self) -> None:
        for overgrant in (False, True):
            api = FakeGitHub()
            api.tree_sha = "d" * 40
            api.fail_path = "" if overgrant else "/repos/" + REPOSITORY + "/pulls"
            api.overgrant = overgrant
            with self.subTest(overgrant=overgrant), self.assertRaises(ValueError):
                self.invoke(api)
            self.assertEqual(api.calls[-1]["method"], "DELETE")
            if overgrant:
                self.assertFalse(
                    any(
                        call.get("method") == "POST" and call["path"].startswith("/repos/")
                        for call in api.calls
                    )
                )

    def test_sparse_large_file_reconstruction_and_original_byte_coordinates(self) -> None:
        original = (
            b"# dependencies\r\nexample==1\r\n"
            + b"# unchanged source\r\n" * (UPDATER_PROPOSAL_MAX_CHANGE_BYTES // 32)
            + b"# preserved example==1\r\n"
            + "é-tail\r\n".encode()
        )
        self.assertGreater(len(original) * 2, UPDATER_PROPOSAL_MAX_CHANGE_BYTES)
        changes = [
            {
                "start_byte": original.index(b"example==1"),
                "old_text": "example==1",
                "new_text": "example==20",
            },
            {
                "start_byte": original.index("é-tail".encode()),
                "old_text": "é-tail",
                "new_text": "é-new-tail",
            },
        ]
        api = SparseFakeGitHub({"requirements.txt": original})
        api.base_mode = "100755"
        payload = {
            **proposal_payload(),
            "files": [sparse_change("requirements.txt", original, changes)],
        }
        result = self.invoke(api, payload)
        tree = next(
            call["body"]["tree"] for call in api.calls if call["path"].endswith("/git/trees")
        )
        self.assertEqual(
            tree,
            [
                {
                    "path": "requirements.txt",
                    "mode": api.base_mode,
                    "type": "blob",
                    "content": original.replace(b"example==1", b"example==20", 1)
                    .replace("é-tail".encode(), "é-new-tail".encode())
                    .decode(),
                }
            ],
        )
        self.assertEqual(result.disposition, "created")
        self.assertEqual(self.invoke(api, payload).disposition, "replayed")
        api.closed_prs, api.prs = api.prs, []
        self.assertEqual(self.invoke(api, payload).disposition, "replayed")
        self.assertEqual(sum(call["path"].endswith("/pulls") for call in api.calls), 1)

    def test_sparse_insert_delete_and_mixed_full_file_changes(self) -> None:
        original = "éabc\r\n".encode()
        payload = {
            **proposal_payload(),
            "files": [
                sparse_change(
                    "requirements.txt",
                    original,
                    [
                        {"start_byte": len("é".encode()), "old_text": "", "new_text": "X"},
                        {"start_byte": len("éa".encode()), "old_text": "b", "new_text": ""},
                    ],
                ),
                {"path": "new.txt", "content": "new file\n"},
            ],
        }
        api = SparseFakeGitHub({"requirements.txt": original})
        self.invoke(api, payload)
        tree = next(
            call["body"]["tree"] for call in api.calls if call["path"].endswith("/git/trees")
        )
        self.assertEqual([entry["content"] for entry in tree], ["éXac\r\n", "new file\n"])

    def test_sparse_shape_order_and_incoming_budgets_refuse_before_provider(self) -> None:
        edit = {"start_byte": 0, "old_text": "a", "new_text": "b"}
        change = sparse_change("requirements.txt", b"abc", [edit])
        cases = [
            {**change, "content": "replacement"},
            {**change, "mode": "100755"},
            {**change, "base_blob_sha": "not-a-sha"},
            {**change, "path": ".github/workflows/update.yml"},
            {**change, "edits": []},
            {**change, "edits": [edit, edit]},
            {**change, "edits": [{**edit, "old_text": "ab"}, {**edit, "start_byte": 1}]},
            {**change, "edits": [{**edit, "start_byte": 2}, edit]},
            {**change, "edits": [{**edit, "old_text": "", "new_text": ""}]},
            {**change, "edits": [{**edit, "new_text": "\ud800"}]},
            {
                **change,
                "edits": [
                    {
                        **edit,
                        "old_text": "",
                        "new_text": "é" * (UPDATER_PROPOSAL_MAX_CHANGE_BYTES // 2 + 1),
                    }
                ],
            },
            {
                **change,
                "edits": [{**edit, "start_byte": n} for n in range(UPDATER_PROPOSAL_MAX_EDITS + 1)],
            },
            {**change, "edits": [{**edit, "old_text": ""}, {**edit, "old_text": ""}]},
        ]
        cases.extend(
            {**change, "edits": [{**edit, "start_byte": value}]}
            for value in (-1, True, 1.0, "1", UPDATER_PROPOSAL_MAX_FILE_BYTES + 1)
        )
        for case in cases:
            with self.subTest(case=str(case)[:120]), self.assertRaises(ValidationError):
                UpdaterProposalRequest.model_validate({**proposal_payload(), "files": [case]})
        first = [{**edit, "start_byte": n} for n in range(UPDATER_PROPOSAL_MAX_EDITS)]
        with self.assertRaises(ValidationError):
            UpdaterProposalRequest.model_validate(
                {
                    **proposal_payload(),
                    "files": [
                        {**change, "edits": first},
                        {**change, "path": "other.txt"},
                    ],
                }
            )

    def test_sparse_mismatch_encoding_and_blob_corruption_make_no_repository_writes(self) -> None:
        original = "éabc".encode()
        good = {"start_byte": 2, "old_text": "a", "new_text": "x"}
        cases: list[tuple[dict[str, Any], dict[str, object]]] = [
            ({"start_byte": 2, "old_text": "b", "new_text": "x"}, {}),
            ({"start_byte": len(original) + 1, "old_text": "", "new_text": "x"}, {}),
            ({"start_byte": 1, "old_text": "", "new_text": "x"}, {}),
            (good, {"sha": "e" * 40}),
            (good, {"size": len(original) + 1}),
            (good, {"size": True}),
            (good, {"encoding": "utf-8"}),
            (good, {"content": "!not-base64!"}),
            (good, {"content": base64.b64encode(b"corrupt").decode()}),
            (good, {"content": "A" * (2 * UPDATER_PROPOSAL_MAX_FILE_BYTES + 1)}),
        ]
        for edit, corruption in cases:
            api = SparseFakeGitHub({"requirements.txt": original})
            api.blob_response_changes = corruption
            payload = {
                **proposal_payload(),
                "files": [sparse_change("requirements.txt", original, [edit])],
            }
            with (
                self.subTest(edit=edit, corruption=list(corruption)),
                self.assertRaises(UpdaterProposalRefused),
            ):
                self.invoke(api, payload)
            self.assertFalse(
                any(
                    call.get("method") == "POST" and call["path"].startswith("/repos/")
                    for call in api.calls
                )
            )
            self.assertEqual(api.calls[-1]["method"], "DELETE")
        invalid = b"\xffabc"
        api = SparseFakeGitHub({"requirements.txt": invalid})
        with self.assertRaises(UpdaterProposalRefused):
            self.invoke(
                api,
                {
                    **proposal_payload(),
                    "files": [
                        sparse_change(
                            "requirements.txt",
                            invalid,
                            [{"start_byte": 1, "old_text": "a", "new_text": "b"}],
                        )
                    ],
                },
            )

    def test_sparse_requires_path_blob_identity_and_regular_existing_file(self) -> None:
        original = b"abc"
        good = sparse_change(
            "requirements.txt", original, [{"start_byte": 0, "old_text": "a", "new_text": "b"}]
        )
        for changes, mode in (
            ({"base_blob_sha": "e" * 40}, "100644"),
            ({"path": "missing.txt"}, "100644"),
            ({}, "120000"),
            ({}, "160000"),
        ):
            api = SparseFakeGitHub({"requirements.txt": original})
            api.base_mode = mode
            with (
                self.subTest(changes=changes, mode=mode),
                self.assertRaises(UpdaterProposalRefused),
            ):
                self.invoke(api, {**proposal_payload(), "files": [{**good, **changes}]})
            self.assertFalse(
                any(
                    "/git/blobs/" in call["path"]
                    or call.get("method") == "POST"
                    and call["path"].startswith("/repos/")
                    for call in api.calls
                )
            )

    def test_sparse_materialization_limits_precede_blob_reads_and_writes(self) -> None:
        original = b"a" * UPDATER_PROPOSAL_MAX_FILE_BYTES
        edit = {"start_byte": 0, "old_text": "a", "new_text": "b"}
        count = UPDATER_PROPOSAL_MAX_MATERIALIZED_BYTES // (len(original) * 2) + 1
        files = {str(n): original for n in range(count)}
        cases = [
            (
                files,
                [sparse_change(path, content, [edit]) for path, content in files.items()],
                None,
            ),
            (
                {"requirements.txt": original},
                [sparse_change("requirements.txt", original, [{**edit, "new_text": "bb"}])],
                None,
            ),
            (
                {"requirements.txt": b"a"},
                [sparse_change("requirements.txt", b"a", [edit])],
                UPDATER_PROPOSAL_MAX_FILE_BYTES + 1,
            ),
        ]
        for originals, changes, size_override in cases:
            api = SparseFakeGitHub(originals)
            api.tree_size_override = size_override
            with (
                self.subTest(files=len(originals), override=size_override),
                self.assertRaises(UpdaterProposalRefused),
            ):
                self.invoke(api, {**proposal_payload(), "files": changes})
            self.assertFalse(
                any(
                    "/git/blobs/" in call["path"]
                    or call.get("method") == "POST"
                    and call["path"].startswith("/repos/")
                    for call in api.calls
                )
            )

    def test_sparse_validates_last_file_before_any_git_object_write(self) -> None:
        original = b"abc"
        api = SparseFakeGitHub({"first.txt": original, "last.txt": original})
        changes = [
            sparse_change(
                "first.txt", original, [{"start_byte": 0, "old_text": "a", "new_text": "b"}]
            ),
            sparse_change(
                "last.txt", original, [{"start_byte": 0, "old_text": "xyz", "new_text": "x"}]
            ),
        ]
        with self.assertRaises(UpdaterProposalRefused):
            self.invoke(api, {**proposal_payload(), "files": changes})
        self.assertFalse(
            any(
                call.get("method") == "POST" and call["path"].startswith("/repos/")
                for call in api.calls
            )
        )

    def test_sparse_noop_and_blob_read_failure_revoke_without_git_writes(self) -> None:
        original = b"aa"
        for edits, fail_read in (
            (
                [
                    {"start_byte": 0, "old_text": "a", "new_text": ""},
                    {"start_byte": 1, "old_text": "a", "new_text": "aa"},
                ],
                False,
            ),
            ([{"start_byte": 0, "old_text": "a", "new_text": "b"}], True),
        ):
            api = SparseFakeGitHub({"requirements.txt": original})
            if fail_read:
                api.fail_path = "/repos/" + REPOSITORY + "/git/blobs/" + blob_sha(original)
            with self.subTest(fail_read=fail_read), self.assertRaises(ValueError):
                self.invoke(
                    api,
                    {
                        **proposal_payload(),
                        "files": [sparse_change("requirements.txt", original, edits)],
                    },
                )
            self.assertFalse(
                any(
                    call.get("method") == "POST" and call["path"].startswith("/repos/")
                    for call in api.calls
                )
            )
            self.assertEqual(api.calls[-1]["method"], "DELETE")

    def test_sparse_materialization_at_limit_and_legacy_deletion(self) -> None:
        original = b"a" * UPDATER_PROPOSAL_MAX_FILE_BYTES
        count = UPDATER_PROPOSAL_MAX_MATERIALIZED_BYTES // (len(original) * 2)
        originals = {str(n): original for n in range(count)}
        api = SparseFakeGitHub(originals)
        self.invoke(
            api,
            {
                **proposal_payload(),
                "files": [
                    sparse_change(
                        path, content, [{"start_byte": 0, "old_text": "a", "new_text": "b"}]
                    )
                    for path, content in originals.items()
                ],
            },
        )
        tree = next(
            call["body"]["tree"] for call in api.calls if call["path"].endswith("/git/trees")
        )
        self.assertEqual(
            [entry["content"] for entry in tree], [(b"b" + original[1:]).decode()] * count
        )
        legacy = FakeGitHub()
        legacy.tree_sha = "d" * 40
        self.invoke(
            legacy, {**proposal_payload(), "files": [{"path": "requirements.txt", "content": None}]}
        )
        deleted = next(
            call["body"]["tree"] for call in legacy.calls if call["path"].endswith("/git/trees")
        )
        self.assertEqual(
            deleted,
            [{"path": "requirements.txt", "mode": legacy.base_mode, "type": "blob", "sha": None}],
        )

    def test_sparse_orphan_recovery_and_pending_after_main_moves(self) -> None:
        original = b"abc"
        api = SparseFakeGitHub({"requirements.txt": original})
        payload = {
            **proposal_payload(),
            "files": [
                sparse_change(
                    "requirements.txt",
                    original,
                    [{"start_byte": 0, "old_text": "a", "new_text": "b"}],
                )
            ],
        }
        api.fail_path = "/repos/" + REPOSITORY + "/pulls"
        with self.assertRaises(ValueError):
            self.invoke(api, payload)
        api.fail_path = ""
        api.base_sha = HEAD_SHA
        first = self.invoke(api, payload)
        count = len(api.calls)
        pending = self.invoke(api, {**payload, "title": "Another proposal"})
        self.assertEqual(pending.disposition, "pending")
        self.assertEqual(pending.pull_request_number, first.pull_request_number)
        self.assertFalse(any("/git/blobs/" in call["path"] for call in api.calls[count:]))
        self.assertEqual(len(api.created_refs), 1)


class UpdaterHttpTests(unittest.IsolatedAsyncioTestCase):
    def app(self, verifier: Any, policy: LaunchplaneAuthzPolicy | None = None) -> Any:
        return create_launchplane_fastapi_app(
            verifier=verifier,
            authz_policy=policy or updater_policy(),
            record_store_factory=lambda: object(),
        )

    async def test_authorized_caller_receives_the_typed_created_response(self) -> None:
        result = UpdaterProposalResponse(
            repository=REPOSITORY,
            branch="launchplane/updater/test",
            head_sha=HEAD_SHA,
            pull_request_number=17,
            pull_request_url="https://github.com/" + REPOSITORY + "/pull/17",
        )
        for payload in (
            proposal_payload(),
            {
                **proposal_payload(),
                "files": [
                    sparse_change(
                        "requirements.txt",
                        b"abc",
                        [{"start_byte": 0, "old_text": "a", "new_text": "b"}],
                    )
                ],
            },
        ):
            with (
                self.subTest(payload=payload),
                patch(
                    "control_plane.http_routes.updater_proposal.open_updater_proposal",
                    return_value=result,
                ) as provider,
            ):
                response = await http_request(
                    self.app(_StubVerifier(updater_identity())),
                    "POST",
                    UPDATER_PROPOSAL_ROUTE,
                    headers={"Authorization": "Bearer valid-token"},
                    payload=payload,
                )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json(), result.model_dump(mode="json"))
            provider.assert_called_once()

    async def test_unlisted_callers_never_invoke_provider(self) -> None:
        for identity in (
            replace(updater_identity(), repository="example/other"),
            replace(
                updater_identity(), workflow_ref=WORKFLOW.replace("update.yml", "unlisted.yml")
            ),
            replace(updater_identity(), ref="refs/heads/feature"),
        ):
            with (
                self.subTest(identity=identity),
                patch(
                    "control_plane.http_routes.updater_proposal.open_updater_proposal"
                ) as provider,
            ):
                response = await http_request(
                    self.app(_StubVerifier(identity)),
                    "POST",
                    UPDATER_PROPOSAL_ROUTE,
                    headers={"Authorization": "Bearer valid-token"},
                    payload=proposal_payload(),
                )
                self.assertEqual(response.status_code, 403)
                provider.assert_not_called()

    async def test_forged_expired_missing_expiry_wrong_issuer_and_audience_tokens(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwks = Mock()
        jwks.get_signing_key_from_jwt.return_value = SimpleNamespace(key=key.public_key())
        verifier = GitHubOidcVerifier(audience="launchplane", jwk_client=jwks)
        claims: dict[str, Any] = {
            **updater_identity().__dict__,
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "launchplane",
            "sub": "repo:" + REPOSITORY + ":ref:refs/heads/main",
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        }
        claims.pop("raw_claims")
        cases = [
            (other, claims, 401),
            (key, {**claims, "exp": 1}, 401),
            (key, {name: value for name, value in claims.items() if name != "exp"}, 403),
            (key, {**claims, "iss": "https://untrusted.example"}, 401),
            (key, {**claims, "aud": "other"}, 401),
        ]
        for signing_key, payload, expected_status in cases:
            token = jwt.encode(payload, signing_key, algorithm="RS256")
            with (
                self.subTest(claims=payload),
                patch(
                    "control_plane.http_routes.updater_proposal.open_updater_proposal"
                ) as provider,
            ):
                response = await http_request(
                    self.app(verifier),
                    "POST",
                    UPDATER_PROPOSAL_ROUTE,
                    headers={"Authorization": "Bearer " + token},
                    payload=proposal_payload(),
                )
                self.assertEqual(response.status_code, expected_status)
                provider.assert_not_called()

    async def test_stale_base_is_a_conflict_instead_of_an_authorization_denial(self) -> None:
        with patch(
            "control_plane.http_routes.updater_proposal.open_updater_proposal",
            side_effect=UpdaterProposalConflict("Base changed; rerun"),
        ):
            response = await http_request(
                self.app(_StubVerifier(updater_identity())),
                "POST",
                UPDATER_PROPOSAL_ROUTE,
                headers={"Authorization": "Bearer valid-token"},
                payload=proposal_payload(),
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "updater_proposal_conflict")

    async def test_provider_error_is_redacted_and_body_is_bounded(self) -> None:
        with patch(
            "control_plane.http_routes.updater_proposal.open_updater_proposal",
            side_effect=ValueError("installation-token-secret"),
        ):
            response = await http_request(
                self.app(_StubVerifier(updater_identity())),
                "POST",
                UPDATER_PROPOSAL_ROUTE,
                headers={"Authorization": "Bearer valid-token"},
                payload=proposal_payload(),
            )
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("installation-token-secret", response.text)
        response = await http_request(
            self.app(_StubVerifier(updater_identity())),
            "POST",
            UPDATER_PROPOSAL_ROUTE,
            headers={"Authorization": "Bearer valid-token", "Content-Type": "application/json"},
            raw_body=b"x" * (UPDATER_PROPOSAL_MAX_BODY_BYTES + 1),
        )
        self.assertEqual(response.status_code, 413)
