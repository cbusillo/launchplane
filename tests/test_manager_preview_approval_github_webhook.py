import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import ANY, patch

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
    build_manager_preview_approval_system_event,
)
from control_plane.manager_preview_approval_github_webhook import (
    ManagerPreviewApprovalGitHubDependencies,
    handle_manager_preview_approval_github_webhook_request,
    invalidate_manager_preview_approval_for_pr,
    parse_manager_preview_approval_command,
    reconcile_manager_preview_approval_for_pr,
    reconcile_manager_preview_approval_for_pr_best_effort,
)
from control_plane.manager_preview_approval_projection import (
    build_manager_preview_approval_projection,
    write_manager_preview_approval_projection,
)
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy
from control_plane.trusted_maintenance_github_webhook import (
    TrustedMaintenanceGitHubWebhookResult,
)


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
        self.comment_id = 501
        self.comment_created_at = NOW
        self.comment_actor_id = 101
        self.comment_actor_login = "manager"
        self.head_sha = HEAD_SHA
        self.pr_state = "open"
        self.projection_actor_id = 9001
        self.comments: list[dict[str, object]] = []
        self.statuses: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, object]] = []
        self.fail_projection_writes = False

    def __call__(self, **kwargs: object) -> object:
        path = str(kwargs.get("path") or "")
        method = str(kwargs.get("method") or "GET")
        body = kwargs.get("body")
        self.calls.append((method, path, body))
        if (
            self.fail_projection_writes
            and method in {"PATCH", "POST"}
            and (
                "/issues/comments/" in path
                or path.endswith(f"/issues/{PR_NUMBER}/comments")
                or "/statuses/" in path
            )
        ):
            raise click.ClickException("GitHub projection is temporarily unavailable.")
        if path == "/user":
            return {"id": self.projection_actor_id, "login": "launchplane"}
        if path == f"/repos/{REPOSITORY}/issues/comments/{self.comment_id}" and method == "GET":
            return {
                "id": self.comment_id,
                "body": self.comment_body,
                "created_at": self.comment_created_at,
                "user": {
                    "id": self.comment_actor_id,
                    "login": self.comment_actor_login,
                    "type": "User",
                },
            }
        if path == f"/repos/{REPOSITORY}/pulls/{PR_NUMBER}":
            return {
                "number": PR_NUMBER,
                "state": self.pr_state,
                "html_url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
                "head": {"sha": self.head_sha},
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
        if path == f"/repos/{REPOSITORY}/statuses/{self.head_sha}" and method == "POST":
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

    def test_signed_pull_request_delegates_to_trusted_maintenance_after_signature(
        self,
    ) -> None:
        store = _Store()
        github = _GitHubApi()
        payload = _pull_request_payload(action="synchronize")
        body = json.dumps(payload).encode()
        with patch(
            "control_plane.manager_preview_approval_github_webhook."
            "handle_trusted_maintenance_github_webhook",
            return_value=TrustedMaintenanceGitHubWebhookResult(
                status="skipped",
                reason="test",
            ),
        ) as trusted_handler:
            status_code, response = handle_manager_preview_approval_github_webhook_request(
                body,
                "pull_request",
                "delivery-trusted-preview",
                "sha256=test",
                store,
                Path("/tmp/launchplane"),
                "trace-trusted-preview",
                dependencies=_dependencies(github),
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(response["status"], "accepted")
        trusted_handler.assert_called_once_with(
            event_name="pull_request",
            delivery_id="delivery-trusted-preview",
            signed_payload_sha256=hashlib.sha256(body).hexdigest(),
            payload=payload,
            record_store=store,
            control_plane_root=Path("/tmp/launchplane"),
            dependencies=ANY,
        )

    def test_invalid_signature_never_delegates_to_trusted_maintenance(self) -> None:
        def reject_signature(**_kwargs: object) -> None:
            raise click.ClickException("invalid signature")

        dependencies = ManagerPreviewApprovalGitHubDependencies(
            webhook_secret=lambda: "secret",
            verify_signature=reject_signature,
            github_api=_GitHubApi(),
            github_token=lambda **_kwargs: "managed-token",
        )
        with patch(
            "control_plane.manager_preview_approval_github_webhook."
            "handle_trusted_maintenance_github_webhook"
        ) as trusted_handler:
            status_code, response = handle_manager_preview_approval_github_webhook_request(
                json.dumps(_pull_request_payload(action="synchronize")).encode(),
                "pull_request",
                "delivery-invalid-signature",
                "sha256=invalid",
                _Store(),
                Path("/tmp/launchplane"),
                "trace-invalid-signature",
                dependencies=dependencies,
            )

        self.assertEqual(status_code, 401)
        error = response["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "invalid_signature")
        trusted_handler.assert_not_called()

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
            dependencies=_dependencies(github, now="2026-07-31T12:05:00Z"),
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

    def test_exact_current_command_replaces_stale_prior_binding(self) -> None:
        store = _Store()
        github = _GitHubApi()
        dependencies = _dependencies(github)
        prior_fingerprint = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        ).binding_sha256
        github.comment_body = f"/preview approve {prior_fingerprint}"
        payload = json.dumps(_issue_comment_payload()).encode()
        handle_manager_preview_approval_github_webhook_request(
            payload,
            "issue_comment",
            "delivery-prior",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-prior",
            dependencies=dependencies,
        )
        current_head_sha = "2" * 40
        _replace_serving_generation(store, head_sha=current_head_sha)
        github.head_sha = current_head_sha
        github.comment_id = 502
        github.comment_created_at = NOW
        current_fingerprint = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        ).binding_sha256
        github.comment_body = f"/preview approve {current_fingerprint}"
        current_payload = json.dumps(_issue_comment_payload(comment_id=502)).encode()

        status, response = handle_manager_preview_approval_github_webhook_request(
            current_payload,
            "issue_comment",
            "delivery-current",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-current",
            dependencies=dependencies,
        )

        self.assertEqual(status, 202)
        result = response["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["event_status"], "written")
        self.assertEqual(result["approval_status"], "approved")
        self.assertEqual(len(store.events), 2)
        self.assertEqual(github.statuses[-1]["state"], "success")

        replay_status, replay_response = handle_manager_preview_approval_github_webhook_request(
            current_payload,
            "issue_comment",
            "delivery-current",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-current-replay",
            dependencies=dependencies,
        )

        self.assertEqual(replay_status, 202)
        replay_result = replay_response["result"]
        assert isinstance(replay_result, dict)
        self.assertEqual(replay_result["event_status"], "replayed")
        self.assertEqual(replay_result["approval_status"], "approved")
        self.assertEqual(len(store.events), 2)

    def test_exact_current_command_replaces_policy_stale_approval(self) -> None:
        store = _Store()
        github = _GitHubApi()
        fingerprint = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        ).binding_sha256
        github.comment_body = f"/preview approve {fingerprint}"
        github.comment_created_at = "2026-07-31T11:50:00Z"
        handle_manager_preview_approval_github_webhook_request(
            json.dumps(_issue_comment_payload()).encode(),
            "issue_comment",
            "delivery-policy-1",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-policy-1",
            dependencies=_dependencies(github, now="2026-07-31T11:50:00Z"),
        )
        store.policy = _policy_record(
            revision=2,
            extra_actions=("product_profile.read",),
        )
        github.comment_id = 502
        github.comment_created_at = "2026-07-31T12:00:00Z"

        status, response = handle_manager_preview_approval_github_webhook_request(
            json.dumps(_issue_comment_payload(comment_id=502)).encode(),
            "issue_comment",
            "delivery-policy-2",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-policy-2",
            dependencies=_dependencies(github, now="2026-07-31T12:00:00Z"),
        )

        self.assertEqual(status, 202)
        result = response["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["event_status"], "written")
        self.assertEqual(result["approval_status"], "approved")
        self.assertEqual(len(store.events), 2)
        self.assertEqual(github.statuses[-1]["state"], "success")
        latest_event = max(
            store.events.values(),
            key=lambda event: (event.occurred_at, event.event_id),
        )
        assert latest_event.authorization is not None
        self.assertEqual(latest_event.authorization.policy_revision, 2)

    def test_binding_change_during_command_evaluation_rejects_stale_fingerprint(self) -> None:
        class _SameHeadBindingRaceStore(_Store):
            def __init__(self) -> None:
                super().__init__()
                self.replace_after_read = True

            def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord:
                generation = super().read_preview_generation_record(generation_id)
                if self.replace_after_read:
                    self.replace_after_read = False
                    _replace_serving_generation(self, head_sha=HEAD_SHA)
                return generation

        store = _SameHeadBindingRaceStore()
        github = _GitHubApi()
        prior_fingerprint = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        ).binding_sha256
        github.comment_body = f"/preview approve {prior_fingerprint}"

        status, response = handle_manager_preview_approval_github_webhook_request(
            json.dumps(_issue_comment_payload()).encode(),
            "issue_comment",
            "delivery-binding-race",
            "sha256=test",
            store,
            Path("/tmp/launchplane"),
            "trace-binding-race",
            dependencies=_dependencies(github),
        )

        self.assertEqual(status, 202)
        self.assertEqual(response["reason"], "stale_fingerprint")
        self.assertEqual(store.events, {})
        self.assertEqual(github.statuses[-1]["state"], "pending")

    def test_exact_terminal_binding_remains_stale(self) -> None:
        for action in ("invalidated", "superseded"):
            with self.subTest(action=action):
                store = _Store()
                binding = build_current_manager_preview_approval_binding(
                    product=PRODUCT,
                    preview=store.preview,
                    generation=store.generation,
                )
                store.write_manager_preview_approval_event_record(
                    build_manager_preview_approval_system_event(
                        binding=binding,
                        action=action,
                        occurred_at="2026-07-31T11:45:00Z",
                        source_event_kind="preview_lifecycle",
                        source_event_id=f"generation-17:{action}",
                        reason="The exact serving preview binding is terminal.",
                    )
                )
                github = _GitHubApi()
                github.comment_body = f"/preview approve {binding.binding_sha256}"

                status, response = handle_manager_preview_approval_github_webhook_request(
                    json.dumps(_issue_comment_payload()).encode(),
                    "issue_comment",
                    f"delivery-{action}",
                    "sha256=test",
                    store,
                    Path("/tmp/launchplane"),
                    f"trace-{action}",
                    dependencies=_dependencies(github),
                )

                self.assertEqual(status, 202)
                self.assertEqual(response["reason"], "preview_evidence_not_current")
                self.assertEqual(len(store.events), 1)
                self.assertEqual(github.statuses[-1]["state"], "failure")

    def test_closed_pr_and_mismatched_head_reject_exact_current_command(self) -> None:
        for stale_source in ("closed_pr", "mismatched_head"):
            with self.subTest(stale_source=stale_source):
                store = _Store()
                github = _GitHubApi()
                binding = build_current_manager_preview_approval_binding(
                    product=PRODUCT,
                    preview=store.preview,
                    generation=store.generation,
                )
                github.comment_body = f"/preview approve {binding.binding_sha256}"
                if stale_source == "closed_pr":
                    github.pr_state = "closed"
                else:
                    github.head_sha = "2" * 40

                status, response = handle_manager_preview_approval_github_webhook_request(
                    json.dumps(_issue_comment_payload()).encode(),
                    "issue_comment",
                    f"delivery-{stale_source}",
                    "sha256=test",
                    store,
                    Path("/tmp/launchplane"),
                    f"trace-{stale_source}",
                    dependencies=_dependencies(github),
                )

                self.assertEqual(status, 202)
                self.assertIn(
                    response["reason"],
                    {"preview_evidence_not_current", "stale_head"},
                )
                self.assertEqual(store.events, {})
                self.assertEqual(github.statuses[-1]["state"], "failure")

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
        self.assertEqual(error["message"], "GitHub webhook signature is invalid.")
        self.assertNotIn("signature mismatch", json.dumps(response))

    def test_unavailable_evidence_does_not_expose_internal_exception(self) -> None:
        dependencies = ManagerPreviewApprovalGitHubDependencies(
            webhook_secret=lambda: "secret",
            verify_signature=lambda **_kwargs: None,
            github_token=lambda **_kwargs: (_ for _ in ()).throw(
                click.ClickException("private credential path")
            ),
        )

        status, response = handle_manager_preview_approval_github_webhook_request(
            json.dumps(_issue_comment_payload()).encode(),
            "issue_comment",
            "delivery-unavailable",
            "sha256=test",
            _Store(),
            Path("/tmp/launchplane"),
            "trace-unavailable",
            dependencies=dependencies,
        )

        self.assertEqual(status, 503)
        error = response["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "manager_preview_approval_unavailable")
        self.assertEqual(
            error["message"],
            "Manager preview approval is temporarily unavailable.",
        )
        self.assertNotIn("private credential path", json.dumps(response))

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

    def test_destroyed_preview_records_invalidation_from_captured_binding(self) -> None:
        store = _Store()
        binding = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        )
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
            binding=binding,
            dependencies=_dependencies(github),
        )

        self.assertEqual(result["event_status"], "written")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(store.events), 1)
        event = next(iter(store.events.values()))
        self.assertEqual(event.action, "invalidated")
        self.assertEqual(event.binding.binding_sha256, binding.binding_sha256)
        self.assertEqual(github.statuses[-1]["state"], "error")

    def test_reconcile_recovers_pending_projection_after_destroy_and_replacement(self) -> None:
        store = _Store()
        prior_binding = build_current_manager_preview_approval_binding(
            product=PRODUCT,
            preview=store.preview,
            generation=store.generation,
        )
        store.write_manager_preview_approval_event_record(
            build_manager_preview_approval_system_event(
                binding=prior_binding,
                action="invalidated",
                occurred_at="2026-07-31T11:45:00Z",
                source_event_kind="preview_destroy",
                source_event_id="destroy-generation-17",
                reason="The prior serving preview was destroyed.",
            )
        )
        assert store.generation.runtime_identity is not None
        store.preview = store.preview.model_copy(
            update={
                "updated_at": "2026-07-31T11:50:00Z",
                "active_generation_id": "generation-18",
                "serving_generation_id": "generation-18",
                "latest_generation_id": "generation-18",
                "latest_manifest_fingerprint": "manifest-18",
            }
        )
        store.generation = store.generation.model_copy(
            update={
                "generation_id": "generation-18",
                "sequence": 2,
                "requested_at": "2026-07-31T11:46:00Z",
                "started_at": "2026-07-31T11:47:00Z",
                "ready_at": "2026-07-31T11:50:00Z",
                "finished_at": "2026-07-31T11:50:00Z",
                "resolved_manifest_fingerprint": "manifest-18",
                "artifact_id": "artifact-18",
                "runtime_identity": store.generation.runtime_identity.model_copy(
                    update={
                        "deployment_record_id": "deployment-18",
                        "artifact_id": "artifact-18",
                        "image_reference": f"ghcr.io/example/site@sha256:{'b' * 64}",
                        "preview_generation_id": "generation-18",
                    }
                ),
            }
        )
        github = _GitHubApi()
        dependencies = _dependencies(github)
        github.fail_projection_writes = True

        self.assertFalse(
            reconcile_manager_preview_approval_for_pr_best_effort(
                repository=REPOSITORY,
                pr_number=PR_NUMBER,
                record_store=store,
                control_plane_root=Path("/tmp/launchplane"),
                dependencies=dependencies,
            )
        )
        self.assertEqual(github.statuses, [])
        github.fail_projection_writes = False

        result = reconcile_manager_preview_approval_for_pr(
            repository=REPOSITORY,
            pr_number=PR_NUMBER,
            record_store=store,
            control_plane_root=Path("/tmp/launchplane"),
            dependencies=dependencies,
        )
        replayed_result = reconcile_manager_preview_approval_for_pr(
            repository=REPOSITORY,
            pr_number=PR_NUMBER,
            record_store=store,
            control_plane_root=Path("/tmp/launchplane"),
            dependencies=dependencies,
        )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["check_state"], "pending")
        self.assertNotEqual(result["fingerprint"], prior_binding.binding_sha256)
        self.assertEqual(replayed_result["fingerprint"], result["fingerprint"])
        self.assertEqual(replayed_result["status"], "pending")
        self.assertEqual(len(store.events), 1)
        self.assertEqual(len(github.comments), 1)
        self.assertEqual(github.statuses[-1]["state"], "pending")
        self.assertIn(str(result["fingerprint"]), str(github.comments[-1]["body"]))


def _dependencies(
    github: _GitHubApi,
    *,
    now: str = NOW,
) -> ManagerPreviewApprovalGitHubDependencies:
    return ManagerPreviewApprovalGitHubDependencies(
        webhook_secret=lambda: "secret",
        verify_signature=lambda **_kwargs: None,
        now_timestamp=lambda: now,
        github_api=github,
        github_token=lambda **_kwargs: "managed-token",
    )


def _issue_comment_payload(*, comment_id: int = 501) -> dict[str, object]:
    return {
        "action": "created",
        "repository": {"full_name": REPOSITORY},
        "issue": {
            "number": PR_NUMBER,
            "pull_request": {"url": f"https://api.github.com/repos/{REPOSITORY}/pulls/{PR_NUMBER}"},
        },
        "comment": {"id": comment_id},
    }


def _pull_request_payload(*, action: str = "synchronize") -> dict[str, object]:
    return {
        "action": action,
        "number": PR_NUMBER,
        "repository": {"full_name": REPOSITORY},
        "pull_request": {
            "number": PR_NUMBER,
            "html_url": f"https://github.com/{REPOSITORY}/pull/{PR_NUMBER}",
            "head": {"sha": HEAD_SHA},
            "user": {"id": 301, "type": "Bot", "login": "automation"},
        },
        "sender": {"id": 301, "type": "Bot", "login": "automation"},
    }


def _replace_serving_generation(store: _Store, *, head_sha: str) -> None:
    assert store.generation.runtime_identity is not None
    store.preview = store.preview.model_copy(
        update={
            "updated_at": "2026-07-31T11:50:00Z",
            "active_generation_id": "generation-18",
            "serving_generation_id": "generation-18",
            "latest_generation_id": "generation-18",
            "latest_manifest_fingerprint": "manifest-18",
        }
    )
    store.generation = store.generation.model_copy(
        update={
            "generation_id": "generation-18",
            "sequence": 2,
            "requested_at": "2026-07-31T11:46:00Z",
            "started_at": "2026-07-31T11:47:00Z",
            "ready_at": "2026-07-31T11:50:00Z",
            "finished_at": "2026-07-31T11:50:00Z",
            "resolved_manifest_fingerprint": "manifest-18",
            "artifact_id": "artifact-18",
            "anchor_summary": store.generation.anchor_summary.model_copy(
                update={"head_sha": head_sha}
            ),
            "runtime_identity": store.generation.runtime_identity.model_copy(
                update={
                    "deployment_record_id": "deployment-18",
                    "artifact_id": "artifact-18",
                    "source_git_ref": head_sha,
                    "image_reference": f"ghcr.io/example/site@sha256:{'b' * 64}",
                    "preview_generation_id": "generation-18",
                }
            ),
        }
    )


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


def _policy_record(
    *,
    revision: int = 1,
    extra_actions: tuple[str, ...] = (),
) -> LaunchplaneAuthzPolicyRecord:
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
                    *extra_actions,
                ),
            ),
        ),
    )
    policy_sha256 = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=revision,
            policy_sha256=policy_sha256,
        ),
        revision=revision,
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
