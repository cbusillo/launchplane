import json
from pathlib import Path
import unittest
from unittest.mock import ANY

import click

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.manager_preview_approval import (
    MANAGER_PREVIEW_APPROVAL_READ_ACTION,
    MANAGER_PREVIEW_APPROVAL_WRITE_ACTION,
    ManagerPreviewApprovalEventRecord,
    ManagerPreviewApprovalEventWriteStatus,
)
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.manager_preview_approval import (
    ManagerPreviewApprovalEventConflictError,
    build_current_manager_preview_approval_binding,
)
from control_plane.manager_preview_approval_github_webhook import (
    ManagerPreviewApprovalGitHubDependencies,
    handle_manager_preview_approval_github_webhook_request,
    invalidate_manager_preview_approval_for_pr,
    parse_manager_preview_approval_command,
)
from control_plane.manager_preview_approval_projection import (
    build_manager_preview_approval_projection,
    write_manager_preview_approval_projection,
)
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy


PRODUCT = "example-site"
CONTEXT = "example-site-preview"
REPOSITORY = "example/example-site"
ANCHOR_REPO = "example-site"
PR_NUMBER = 17
HEAD_SHA = "1" * 40
IMAGE_DIGEST = f"sha256:{'a' * 64}"
NOW = "2026-07-31T12:00:00Z"


class _Store:
    def __init__(self) -> None:
        self.profile = _profile()
        self.preview = _preview()
        self.generation = _generation()
        self.policy = _policy_record()
        self.events: dict[str, ManagerPreviewApprovalEventRecord] = {}

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        if driver_id and driver_id != self.profile.driver_id:
            return ()
        return (self.profile,)

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        records = (
            (self.preview,)
            if (
                (not context_name or context_name == self.preview.context)
                and (not anchor_repo or anchor_repo == self.preview.anchor_repo)
                and (anchor_pr_number is None or anchor_pr_number == self.preview.anchor_pr_number)
            )
            else ()
        )
        return records[:limit] if limit is not None else records

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord:
        if generation_id != self.generation.generation_id:
            raise FileNotFoundError(generation_id)
        return self.generation

    def list_manager_preview_approval_event_records(
        self,
        *,
        product: str = "",
        context: str = "",
        repository: str = "",
        pr_number: int | None = None,
        preview_id: str = "",
        action: str = "",
        limit: int | None = None,
    ) -> tuple[ManagerPreviewApprovalEventRecord, ...]:
        records = tuple(
            event
            for event in self.events.values()
            if (not product or event.binding.product == product)
            and (not context or event.binding.context == context)
            and (not repository or event.binding.repository == repository)
            and (pr_number is None or event.binding.pr_number == pr_number)
            and (not preview_id or event.binding.preview_id == preview_id)
            and (not action or event.action == action)
        )
        records = tuple(sorted(records, key=lambda event: (event.occurred_at, event.event_id)))
        return records[:limit] if limit is not None else records

    def write_manager_preview_approval_event_record(
        self, record: ManagerPreviewApprovalEventRecord
    ) -> ManagerPreviewApprovalEventWriteStatus:
        existing = self.events.get(record.event_id)
        if existing is None:
            self.events[record.event_id] = record
            return "written"
        if existing == record:
            return "replayed"
        raise ManagerPreviewApprovalEventConflictError(
            "Manager preview approval event id conflicts with existing evidence."
        )

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = (self.policy,) if not status or status == self.policy.status else ()
        return records[:limit] if limit is not None else records


class _GitHubApi:
    def __init__(self) -> None:
        self.comment_body = ""
        self.comment_actor_id = 101
        self.comment_actor_login = "manager"
        self.projection_actor_id = 9001
        self.comments: list[dict[str, object]] = []
        self.statuses: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        path = str(kwargs.get("path") or "")
        method = str(kwargs.get("method") or "GET")
        body = kwargs.get("body")
        self.calls.append((method, path, body))
        if path == "/user":
            return {"id": self.projection_actor_id, "login": "launchplane"}
        if path == f"/repos/{REPOSITORY}/issues/comments/501" and method == "GET":
            return {
                "id": 501,
                "body": self.comment_body,
                "user": {
                    "id": self.comment_actor_id,
                    "login": self.comment_actor_login,
                    "type": "User",
                },
            }
        if path == f"/repos/{REPOSITORY}/pulls/{PR_NUMBER}":
            return {
                "number": PR_NUMBER,
                "state": "open",
                "html_url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
                "head": {"sha": HEAD_SHA},
            }
        if path == f"/users/{self.comment_actor_login}":
            return {
                "id": self.comment_actor_id,
                "login": self.comment_actor_login,
                "name": "Example Manager",
            }
        if path == (f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments?per_page=100"):
            return list(self.comments)
        if path == f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments" and method == "POST":
            assert isinstance(body, dict)
            comment = {
                "id": 700 + len(self.comments),
                "html_url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}#issuecomment-700",
                "body": body["body"],
                "user": {"id": self.projection_actor_id},
            }
            self.comments.append(comment)
            return comment
        if path.startswith(f"/repos/{REPOSITORY}/issues/comments/") and method == "PATCH":
            assert isinstance(body, dict)
            comment_id = int(path.rsplit("/", 1)[1])
            comment = next(comment for comment in self.comments if comment["id"] == comment_id)
            comment["body"] = body["body"]
            return comment
        if path == f"/repos/{REPOSITORY}/statuses/{HEAD_SHA}" and method == "POST":
            assert isinstance(body, dict)
            self.statuses.append(body)
            return {"id": len(self.statuses), **body}
        raise AssertionError(f"Unexpected GitHub API request: {method} {path}")


class ManagerPreviewApprovalGitHubWebhookTests(unittest.TestCase):
    def test_parses_only_exact_single_line_commands(self) -> None:
        fingerprint = "a" * 64

        approved = parse_manager_preview_approval_command(f"/preview approve {fingerprint}")
        changes = parse_manager_preview_approval_command(
            f"/preview changes {fingerprint} Please revise the hero"
        )

        self.assertIsNotNone(approved)
        assert approved is not None
        self.assertEqual(approved.action, "approved")
        self.assertIsNotNone(changes)
        assert changes is not None
        self.assertEqual(changes.action, "changes_requested")
        self.assertIsNone(
            parse_manager_preview_approval_command(f"Looks good\n/preview approve {fingerprint}")
        )
        self.assertIsNone(parse_manager_preview_approval_command(f"/preview revoke {fingerprint}"))

    def test_projection_updates_only_credential_owned_comment(self) -> None:
        store = _Store()
        github = _GitHubApi()
        github.comments = [
            {
                "id": 41,
                "html_url": "https://example.test/attacker",
                "body": "<!-- launchplane-manager-preview-approval -->",
                "user": {"id": 1234},
            },
            {
                "id": 42,
                "html_url": "https://example.test/owned",
                "body": "<!-- launchplane-manager-preview-approval -->",
                "user": {"id": github.projection_actor_id},
            },
        ]
        projection = build_manager_preview_approval_projection(
            record_store=store,
            repository=REPOSITORY,
            pr_number=PR_NUMBER,
            pr_url=f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
            pr_state="open",
            current_head_sha=HEAD_SHA,
            evaluated_at=NOW,
        )

        result = write_manager_preview_approval_projection(
            projection=projection,
            token="managed-token",
            api_request=github,
        )

        self.assertEqual(result["check_state"], "pending")
        self.assertIn(
            ("PATCH", f"/repos/{REPOSITORY}/issues/comments/42", ANY),
            github.calls,
        )
        self.assertEqual(github.statuses[-1]["context"], "manager-preview-approval")
        self.assertEqual(github.statuses[-1]["state"], "pending")

    def test_signed_comment_approves_exact_preview_and_replays(self) -> None:
        store = _Store()
        github = _GitHubApi()
        fingerprint = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        ).binding_sha256
        github.comment_body = f"/preview approve {fingerprint}"
        payload = _issue_comment_payload()
        dependencies = _dependencies(github)

        first_status, first = handle_manager_preview_approval_github_webhook_request(
            json.dumps(payload).encode(),
            "issue_comment",
            "delivery-1",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-1",
            dependencies=dependencies,
        )
        replay_status, replay = handle_manager_preview_approval_github_webhook_request(
            json.dumps(payload).encode(),
            "issue_comment",
            "delivery-1",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-2",
            dependencies=dependencies,
        )

        self.assertEqual(first_status, 202)
        first_result = first["result"]
        replay_result = replay["result"]
        assert isinstance(first_result, dict)
        assert isinstance(replay_result, dict)
        self.assertEqual(first_result["event_status"], "written")
        self.assertEqual(first_result["approval_status"], "approved")
        self.assertEqual(replay_status, 202)
        self.assertEqual(replay_result["event_status"], "replayed")
        self.assertEqual(len(store.events), 1)
        self.assertEqual(github.statuses[-1]["state"], "success")

    def test_stale_fingerprint_and_unauthorized_actor_do_not_write_evidence(self) -> None:
        store = _Store()
        github = _GitHubApi()
        dependencies = _dependencies(github)
        payload = json.dumps(_issue_comment_payload()).encode()
        github.comment_body = f"/preview approve {'0' * 64}"

        stale_status, stale = handle_manager_preview_approval_github_webhook_request(
            payload,
            "issue_comment",
            "delivery-stale",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-stale",
            dependencies=dependencies,
        )
        fingerprint = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        ).binding_sha256
        github.comment_body = f"/preview approve {fingerprint}"
        github.comment_actor_id = 202
        github.comment_actor_login = "not-manager"
        unauthorized_status, unauthorized = handle_manager_preview_approval_github_webhook_request(
            payload,
            "issue_comment",
            "delivery-unauthorized",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-unauthorized",
            dependencies=dependencies,
        )

        self.assertEqual(stale_status, 202)
        self.assertEqual(stale["reason"], "stale_fingerprint")
        self.assertEqual(unauthorized_status, 202)
        self.assertEqual(unauthorized["reason"], "unauthorized_actor")
        self.assertEqual(store.events, {})

    def test_invalid_signature_is_rejected_before_payload_processing(self) -> None:
        def reject_signature(**_kwargs: object) -> None:
            raise click.ClickException("signature mismatch")

        dependencies = ManagerPreviewApprovalGitHubDependencies(
            webhook_secret=lambda: "secret",
            verify_signature=reject_signature,
        )

        status, response = handle_manager_preview_approval_github_webhook_request(
            b"{}",
            "issue_comment",
            "delivery-invalid",
            "sha256=bad",
            _Store(),
            Path("/tmp/launchplane"),
            "trace-invalid",
            dependencies=dependencies,
        )

        self.assertEqual(status, 401)
        error = response["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "invalid_signature")

    def test_preview_label_removal_invalidates_current_approval(self) -> None:
        store = _Store()
        github = _GitHubApi()
        dependencies = _dependencies(github)
        fingerprint = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        ).binding_sha256
        github.comment_body = f"/preview approve {fingerprint}"
        handle_manager_preview_approval_github_webhook_request(
            json.dumps(_issue_comment_payload()).encode(),
            "issue_comment",
            "delivery-approve",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-approve",
            dependencies=dependencies,
        )
        payload = {
            "action": "unlabeled",
            "number": PR_NUMBER,
            "label": {"name": "launchplane-preview"},
            "repository": {"full_name": REPOSITORY},
        }

        status, response = handle_manager_preview_approval_github_webhook_request(
            json.dumps(payload).encode(),
            "pull_request",
            "delivery-unlabeled",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-unlabeled",
            dependencies=dependencies,
        )

        self.assertEqual(status, 202)
        result = response["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["approval_status"], "stale")
        self.assertEqual(len(store.events), 2)
        self.assertEqual(github.statuses[-1]["state"], "failure")

    def test_destroyed_preview_still_reprojects_non_success(self) -> None:
        store = _Store()
        store.preview = store.preview.model_copy(update={"state": "destroyed"})
        github = _GitHubApi()

        result = invalidate_manager_preview_approval_for_pr(
            repository=REPOSITORY,
            pr_number=PR_NUMBER,
            reason="The serving preview was destroyed.",
            source_event_kind="preview_destroy",
            source_event_id="destroy-17",
            record_store=store,
            control_plane_root=Path("/tmp/launchplane"),
            occurred_at=NOW,
            dependencies=_dependencies(github),
        )

        self.assertEqual(result["event_status"], "unavailable")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(github.statuses[-1]["state"], "error")


def _dependencies(github: _GitHubApi) -> ManagerPreviewApprovalGitHubDependencies:
    return ManagerPreviewApprovalGitHubDependencies(
        webhook_secret=lambda: "secret",
        verify_signature=lambda **_kwargs: None,
        now_timestamp=lambda: NOW,
        github_api=github,
        github_token=lambda **_kwargs: "managed-token",
    )


def _issue_comment_payload() -> dict[str, object]:
    return {
        "action": "created",
        "repository": {"full_name": REPOSITORY},
        "issue": {
            "number": PR_NUMBER,
            "pull_request": {"url": f"https://api.github.com/repos/{REPOSITORY}/pulls/{PR_NUMBER}"},
        },
        "comment": {"id": 501},
    }


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=PRODUCT,
        display_name="Example Site",
        repository=REPOSITORY,
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/example/site"),
        runtime_port=8069,
        health_path="/web/health",
        preview=ProductPreviewProfile(
            enabled=True,
            context=CONTEXT,
            enable_label="launchplane-preview",
        ),
        updated_at=NOW,
        source="test",
    )


def _policy_record() -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="manager.example-site",
                managed_rule_id="preview-approval",
                github_ids=(101,),
                roles=("read_only",),
                products=(PRODUCT,),
                contexts=(CONTEXT,),
                actions=(
                    MANAGER_PREVIEW_APPROVAL_READ_ACTION,
                    MANAGER_PREVIEW_APPROVAL_WRITE_ACTION,
                ),
            ),
        ),
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=1,
            policy_sha256=policy_sha256,
        ),
        revision=1,
        status="active",
        source="test:manager-preview-approval",
        updated_at=NOW,
        policy_sha256=policy_sha256,
        policy=policy,
    )


def _preview() -> PreviewRecord:
    return PreviewRecord(
        preview_id="preview-17",
        context=CONTEXT,
        anchor_repo=ANCHOR_REPO,
        anchor_pr_number=PR_NUMBER,
        anchor_pr_url=f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
        preview_label="launchplane-preview",
        canonical_url="https://preview-17.example.test/",
        state="active",
        created_at="2026-07-31T11:00:00Z",
        updated_at="2026-07-31T11:30:00Z",
        eligible_at="2026-07-31T11:00:00Z",
        active_generation_id="generation-17",
        serving_generation_id="generation-17",
        latest_generation_id="generation-17",
        latest_manifest_fingerprint="manifest-17",
    )


def _generation() -> PreviewGenerationRecord:
    runtime_identity = RuntimeIdentity(
        product=PRODUCT,
        context=CONTEXT,
        instance="preview-17",
        environment_kind="preview",
        deployment_record_id="deployment-17",
        artifact_id="artifact-17",
        source_git_ref=HEAD_SHA,
        image_reference=f"ghcr.io/example/site@{IMAGE_DIGEST}",
        preview_id="preview-17",
        preview_generation_id="generation-17",
        deployed_at="2026-07-31T11:20:00Z",
    )
    return PreviewGenerationRecord(
        generation_id="generation-17",
        preview_id="preview-17",
        sequence=1,
        state="ready",
        requested_reason="Preview requested.",
        requested_at="2026-07-31T11:00:00Z",
        started_at="2026-07-31T11:01:00Z",
        ready_at="2026-07-31T11:30:00Z",
        finished_at="2026-07-31T11:30:00Z",
        resolved_manifest_fingerprint="manifest-17",
        artifact_id="artifact-17",
        anchor_summary=PreviewPullRequestSummary(
            repo=ANCHOR_REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD_SHA,
            pr_url=f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
        ),
        deploy_status="pass",
        verify_status="pass",
        overall_health_status="pass",
        runtime_identity=runtime_identity,
    )


if __name__ == "__main__":
    unittest.main()
