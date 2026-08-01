import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
from unittest.mock import ANY, patch

from click import ClickException, Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
)
from control_plane.every_code_github_webhook import (
    EveryCodeGitHubWebhookDependencies,
    build_every_code_github_webhook_handler,
    handle_every_code_github_webhook_request,
)
from control_plane.every_code_webhooks import sync_every_code_webhooks
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.trusted_maintenance_github_webhook import (
    TrustedMaintenanceGitHubWebhookResult,
)
from tests.support.auth import _StubVerifier, _identity
from tests.support.http import request as http_request


CLI_MAIN = cast(Command, main)


class _WebhookRunner:
    def __init__(self, *, hooks: dict[str, list[dict[str, object]]]) -> None:
        self.hooks = hooks
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(
        self, args: Sequence[str], input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        self.calls.append((command, input_text))
        if command[:4] == ("gh", "repo", "list", "cbusillo"):
            return subprocess.CompletedProcess(
                command,
                0,
                "cbusillo/code\ncbusillo/launchplane\n",
                "",
            )
        if command[:3] == ("gh", "api", "repos/cbusillo/code/hooks"):
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.hooks["cbusillo/code"]), ""
            )
        if command[:3] == ("gh", "api", "repos/cbusillo/launchplane/hooks"):
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.hooks["cbusillo/launchplane"]), ""
            )
        if command[:5] == ("gh", "api", "-X", "PATCH", "repos/cbusillo/code/hooks/12"):
            return subprocess.CompletedProcess(command, 0, json.dumps({"id": 12}), "")
        if command[:5] == ("gh", "api", "-X", "POST", "repos/cbusillo/launchplane/hooks"):
            return subprocess.CompletedProcess(command, 0, json.dumps({"id": 34}), "")
        if command[:5] == ("gh", "label", "list", "--repo", "cbusillo/code"):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps([{"name": "every-code"}, {"name": "preview"}]),
                "",
            )
        if command[:5] == ("gh", "label", "list", "--repo", "cbusillo/launchplane"):
            return subprocess.CompletedProcess(command, 0, json.dumps([]), "")
        if command[:3] == ("gh", "label", "edit"):
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ("gh", "label", "create"):
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


class _FailingWebhookRunner(_WebhookRunner):
    def __init__(self, *, error_detail: str) -> None:
        super().__init__(hooks={"cbusillo/code": [], "cbusillo/launchplane": []})
        self.error_detail = error_detail

    def __call__(
        self, args: Sequence[str], input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(args)
        if command[:3] == ("gh", "api", "repos/cbusillo/code/hooks"):
            self.calls.append((command, input_text))
            raise subprocess.CalledProcessError(1, command, stderr=self.error_detail)
        return super().__call__(args, input_text)


class EveryCodeWebhookSyncTests(unittest.TestCase):
    def test_sync_updates_existing_hook_and_creates_missing_hook(self) -> None:
        webhook_url = "https://launchplane.example/v1/every-code/github-webhook"
        runner = _WebhookRunner(
            hooks={
                "cbusillo/code": [
                    {
                        "id": 12,
                        "config": {"url": webhook_url},
                    }
                ],
                "cbusillo/launchplane": [],
            }
        )

        results = sync_every_code_webhooks(
            owner="cbusillo",
            webhook_secret="secret",
            webhook_url=webhook_url,
            runner=runner,
        )

        self.assertEqual([result.status for result in results], ["updated", "created"])
        self.assertEqual(results[0].hook_id, 12)
        self.assertEqual(results[1].hook_id, 34)
        self.assertEqual(results[0].labels_synced, 6)
        self.assertEqual(results[1].labels_synced, 6)
        payloads = {
            command[3]: json.loads(input_text or "{}")
            for command, input_text in runner.calls
            if command[:3] == ("gh", "api", "-X")
        }
        patch_payload = payloads["PATCH"]
        post_payload = payloads["POST"]
        expected_events = [
            "issues",
            "pull_request",
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
        ]
        self.assertEqual(patch_payload["events"], expected_events)
        self.assertEqual(post_payload["events"], expected_events)
        self.assertEqual(patch_payload["config"]["url"], webhook_url)
        self.assertEqual(post_payload["config"]["url"], webhook_url)
        self.assertEqual(patch_payload["config"]["secret"], "secret")
        label_edits = [
            command for command, _input in runner.calls if command[:3] == ("gh", "label", "edit")
        ]
        label_creates = [
            command for command, _input in runner.calls if command[:3] == ("gh", "label", "create")
        ]
        self.assertIn(
            (
                "gh",
                "label",
                "edit",
                "every-code",
                "--repo",
                "cbusillo/code",
                "--color",
                "FBCA04",
                "--description",
                "Ask Every Code to work this issue.",
            ),
            label_edits,
        )
        self.assertIn(
            (
                "gh",
                "label",
                "create",
                "preview-ready",
                "--repo",
                "cbusillo/launchplane",
                "--color",
                "1D76DB",
                "--description",
                "Every Code preview is ready for source issue validation.",
            ),
            label_creates,
        )

    def test_sync_requires_explicit_webhook_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires webhook_url"):
            sync_every_code_webhooks(
                owner="cbusillo",
                webhook_secret="secret",
                webhook_url="",
                runner=_WebhookRunner(hooks={}),
            )

    def test_sync_redacts_github_cli_failure_in_operator_payload(self) -> None:
        runner = _FailingWebhookRunner(
            error_detail=(
                "github_pat_example_token_value\n"
                "Authorization: Bearer example-bearer-value\n"
                "https://operator:password@hooks.internal/private\n"
                "LAUNCHPLANE_EVERY_CODE_GITHUB_TOKEN=example-token-value"
            )
        )

        results = sync_every_code_webhooks(
            owner="cbusillo",
            webhook_secret="secret",
            webhook_url="https://launchplane.example/v1/every-code/github-webhook",
            runner=runner,
        )

        failed = results[0]
        payload = json.dumps(failed.as_payload())
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.error_code, "github_cli_failed")
        self.assertTrue(failed.error_correlation_id.startswith("cpf-"))
        self.assertNotIn("\n", failed.error)
        for value in (
            "github_pat_example_token_value",
            "example-bearer-value",
            "operator:password",
            "hooks.internal",
            "example-token-value",
        ):
            self.assertNotIn(value, payload)

    def test_cli_sync_webhooks_requires_webhook_url_config(self) -> None:
        result = CliRunner().invoke(
            CLI_MAIN,
            ["every-code", "sync-webhooks", "--owner", "cbusillo"],
            env={"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": "secret"},
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "--webhook-url or LAUNCHPLANE_EVERY_CODE_WEBHOOK_URL is required", result.output
        )


def create_launchplane_fastapi_test_app(**kwargs: object) -> Any:
    state_dir = kwargs.pop("state_dir", None)
    local_record_store = kwargs.pop("local_record_store_for_tests", None)
    if "record_store_factory" not in kwargs:
        if local_record_store is None:
            if not isinstance(state_dir, Path):
                raise AssertionError("webhook tests must pass a pathlib state_dir")
            local_record_store = FilesystemRecordStore(state_dir=state_dir)
        kwargs["record_store_factory"] = lambda: local_record_store
    factory = cast(Any, create_launchplane_fastapi_app)
    return factory(**kwargs)


def create_every_code_github_webhook_app(**kwargs: object) -> Any:
    state_dir = kwargs.pop("state_dir", None)
    local_record_store = kwargs.pop("local_record_store_for_tests", None)
    dependencies = cast(
        EveryCodeGitHubWebhookDependencies | None,
        kwargs.pop("webhook_dependencies", None),
    )
    if local_record_store is None and isinstance(state_dir, Path):
        local_record_store = FilesystemRecordStore(state_dir=state_dir)
    if local_record_store is not None:
        kwargs["record_store_factory"] = lambda: local_record_store
    kwargs["every_code_github_webhook_handler"] = (
        handle_every_code_github_webhook_request
        if dependencies is None
        else build_every_code_github_webhook_handler(dependencies)
    )
    return create_launchplane_fastapi_test_app(**kwargs)


def _fixed_github_token(*, control_plane_root: Path, context_name: str) -> str:
    del control_plane_root, context_name
    return "github-token"


def _failing_github_token(*, control_plane_root: Path, context_name: str) -> str:
    del control_plane_root, context_name
    raise ClickException("Traceback (most recent call last): secret token leaked")


def _github_webhook_body_signature(body_bytes: bytes, secret: str) -> str:
    signature = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={signature}"


def _github_webhook_signature(payload: Mapping[str, object], secret: str) -> str:
    body_bytes = json.dumps(payload).encode("utf-8")
    return _github_webhook_body_signature(body_bytes, secret)


def _every_code_github_issue_labeled_payload(
    *,
    label: str = "every-code",
    action: str = "labeled",
    repository: str = "cbusillo/code",
    issue_number: int = 123,
    issue_url: str = "",
    closed_at: str = "",
    state_reason: str = "",
) -> dict[str, object]:
    resolved_issue_url = (
        issue_url.strip() or f"https://github.com/{repository}/issues/{issue_number}"
    )
    issue_payload = {
        "number": issue_number,
        "html_url": resolved_issue_url,
        "title": "Wire local automation",
    }
    if closed_at.strip():
        issue_payload["closed_at"] = closed_at
    if state_reason.strip():
        issue_payload["state_reason"] = state_reason
    return {
        "action": action,
        "label": {"name": label},
        "repository": {"full_name": repository},
        "issue": issue_payload,
        "sender": {"login": "cbusillo"},
    }


def _every_code_github_pull_request_closed_payload(
    *,
    repository: str = "cbusillo/code",
    pr_number: int = 26,
    merged: bool = True,
    closed_at: str = "2026-05-06T16:20:00Z",
    body: str = "",
) -> dict[str, object]:
    return {
        "action": "closed",
        "repository": {"full_name": repository},
        "pull_request": {
            "number": pr_number,
            "html_url": f"https://github.com/{repository}/pull/{pr_number}",
            "merged": merged,
            "closed_at": closed_at,
            "body": body,
        },
        "sender": {"login": "cbusillo"},
    }


def _claim_every_code_work_request_record(
    record_store: Any,
    request_id: str,
    *,
    host: str = "Chris-Studio",
) -> EveryCodeWorkRequestRecord:
    record = record_store.claim_every_code_work_request_record(
        request_id=request_id,
        host=host,
        claimed_at="2026-05-05T22:01:00Z",
    )
    if record is None:
        raise AssertionError(f"Every Code work request {request_id} was not queued")
    return cast(EveryCodeWorkRequestRecord, record)


def _every_code_claim_fixture_response(
    record: EveryCodeWorkRequestRecord,
) -> tuple[int, dict[str, Any]]:
    return (
        202,
        {
            "records": {"request_id": record.request_id, "state": record.state},
            "result": {"request": record.model_dump(mode="json")},
        },
    )


def _claim_every_code_work_request_in_filesystem(
    state_dir: Path,
    request_id: str,
    *,
    host: str = "Chris-Studio",
) -> tuple[int, dict[str, Any]]:
    record = _claim_every_code_work_request_record(
        FilesystemRecordStore(state_dir),
        request_id,
        host=host,
    )
    return _every_code_claim_fixture_response(record)


def _update_every_code_work_request_status_record(
    record_store: Any,
    request_id: str,
    *,
    state: Literal["running", "done", "blocked"] = "running",
    host: str = "Chris-Studio",
    updated_at: str = "2026-05-05T22:02:00Z",
    result_pr_url: str = "",
    result_summary: str = "",
    error_message: str = "",
) -> EveryCodeWorkRequestRecord:
    record = record_store.read_every_code_work_request_record(request_id)
    updated = apply_every_code_work_request_status(
        record,
        EveryCodeWorkRequestStatusUpdate(
            state=state,
            host=host,
            updated_at=updated_at,
            fencing_token=record.fencing_token,
            result_pr_url=result_pr_url,
            result_summary=result_summary,
            error_message=error_message,
        ),
    )
    record_store.write_every_code_work_request_record(updated)
    return updated


def _every_code_status_fixture_response(
    record: EveryCodeWorkRequestRecord,
) -> tuple[int, dict[str, Any]]:
    return (
        202,
        {
            "records": {"request_id": record.request_id, "state": record.state},
            "result": {"request": record.model_dump(mode="json"), "notifications": []},
        },
    )


def _update_every_code_work_request_status_in_filesystem(
    state_dir: Path,
    request_id: str,
    *,
    state: Literal["running", "done", "blocked"] = "running",
    host: str = "Chris-Studio",
    updated_at: str = "2026-05-05T22:02:00Z",
    result_pr_url: str = "",
    result_summary: str = "",
    error_message: str = "",
) -> tuple[int, dict[str, Any]]:
    record = _update_every_code_work_request_status_record(
        FilesystemRecordStore(state_dir),
        request_id,
        state=state,
        host=host,
        updated_at=updated_at,
        result_pr_url=result_pr_url,
        result_summary=result_summary,
        error_message=error_message,
    )
    return _every_code_status_fixture_response(record)


def _every_code_github_pr_comment_payload(
    *,
    repository: str = "cbusillo/code",
    pr_number: int = 26,
    body: str = "Please tighten this wording before merge.",
    comment_id: int = 1001,
    issue_body: str = "",
    sender: str = "cbusillo",
    sender_type: str = "User",
) -> dict[str, object]:
    return {
        "action": "created",
        "repository": {"full_name": repository},
        "issue": {
            "number": pr_number,
            "html_url": f"https://github.com/{repository}/pull/{pr_number}",
            "body": issue_body,
            "pull_request": {"url": f"https://api.github.com/repos/{repository}/pulls/{pr_number}"},
        },
        "comment": {
            "id": comment_id,
            "node_id": f"IC_kwDO_test_{comment_id}",
            "html_url": f"https://github.com/{repository}/pull/{pr_number}#issuecomment-{comment_id}",
            "body": body,
            "author_association": "OWNER",
            "user": {"login": sender, "type": sender_type},
        },
        "sender": {"login": sender, "type": sender_type},
    }


def _every_code_github_issue_comment_payload(
    *,
    repository: str = "cbusillo/code",
    issue_number: int = 123,
    issue_author: str = "Mbanks89",
    sender: str = "Mbanks89",
    body: str = "/preview ok",
    comment_id: int = 2001,
) -> dict[str, object]:
    return {
        "action": "created",
        "repository": {"full_name": repository},
        "issue": {
            "number": issue_number,
            "html_url": f"https://github.com/{repository}/issues/{issue_number}",
            "title": "Wire local automation",
            "user": {"login": issue_author},
        },
        "comment": {
            "id": comment_id,
            "node_id": f"IC_kwDO_issue_{comment_id}",
            "html_url": f"https://github.com/{repository}/issues/{issue_number}#issuecomment-{comment_id}",
            "body": body,
            "author_association": "CONTRIBUTOR",
            "user": {"login": sender, "type": "User"},
        },
        "sender": {"login": sender, "type": "User"},
    }


def _invoke_app(
    app: Any,
    *,
    method: str,
    path: str,
    query_string: str = "",
    payload: Mapping[str, object] | None = None,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    request_path = f"{path}?{query_string}" if query_string else path
    response = asyncio.run(
        http_request(
            app,
            method,
            request_path,
            headers=request_headers,
            payload=payload,
        )
    )
    response_payload = response.json()
    assert isinstance(response_payload, dict)
    return response.status_code, cast(dict[str, Any], response_payload)


def _write_github_planning_config(
    root: Path,
    *,
    repo_managers: dict[str, str] | None = None,
    default_manager: str = "@cellmechanic",
    path: str = ".code/github-planning.json",
) -> Path:
    config_path = root / Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "workflow": {
                    "default_manager": default_manager,
                    "repo_managers": repo_managers or {},
                }
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _invoke_http_app(
    app: Any,
    *,
    method: str,
    path: str,
    authorization: str = "",
    query_string: str = "",
    headers: dict[str, str] | None = None,
    body_bytes: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    request_path = f"{path}?{query_string}" if query_string else path
    response = asyncio.run(
        http_request(
            app,
            method,
            request_path,
            headers=request_headers,
            raw_body=body_bytes,
        )
    )
    return response.status_code, dict(response.headers), response.content


class EveryCodeGitHubWebhookRequestTests(unittest.TestCase):
    def test_every_code_github_webhook_creates_work_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["every_code_work_request.read"],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        webhook_payload = _every_code_github_issue_labeled_payload(label="EVERY-CODE")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            webhook_status, webhook_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            work_requests = FilesystemRecordStore(state_dir).list_every_code_work_request_records(
                state="queued"
            )

        self.assertEqual(webhook_status, 202)
        self.assertFalse(webhook_response["deduped"])
        self.assertEqual(webhook_response["records"]["state"], "queued")
        self.assertEqual(webhook_response["github_delivery_id"], "delivery-1")
        self.assertEqual(len(work_requests), 1)
        request = work_requests[0]
        self.assertEqual(request.source, "github_issue_label")
        self.assertEqual(request.repository, "cbusillo/code")
        self.assertEqual(request.issue_number, 123)
        self.assertEqual(request.trigger_label, "every-code")
        self.assertEqual(request.trigger_actor, "cbusillo")
        self.assertEqual(request.github_delivery_id, "delivery-1")

    def test_signed_pull_request_delegates_to_trusted_maintenance_after_signature(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_pull_request_closed_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
            patch(
                "control_plane.every_code_github_webhook.handle_trusted_maintenance_github_webhook",
                return_value=TrustedMaintenanceGitHubWebhookResult(
                    status="skipped",
                    reason="test",
                ),
            ) as trusted_handler,
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-trusted-every-code",
                    "X-Hub-Signature-256": _github_webhook_signature(
                        webhook_payload,
                        secret,
                    ),
                },
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        trusted_handler.assert_called_once_with(
            event_name="pull_request",
            delivery_id="delivery-trusted-every-code",
            signed_payload_sha256=hashlib.sha256(
                json.dumps(webhook_payload).encode("utf-8")
            ).hexdigest(),
            payload=webhook_payload,
            record_store=ANY,
            control_plane_root=Path(temporary_directory_name),
            dependencies=ANY,
        )

    def test_webhook_dependencies_inject_signature_time_anchor_and_token(self) -> None:
        verification_calls: list[tuple[bytes, str, str]] = []
        anchor_calls: list[str] = []
        token_calls: list[tuple[Path, str]] = []

        def webhook_secret() -> str:
            return "injected-secret"

        def verify_signature(
            *,
            payload_bytes: bytes,
            signature_header: str,
            secret: str,
        ) -> None:
            verification_calls.append((payload_bytes, signature_header, secret))

        def now_timestamp() -> str:
            return "2026-07-14T12:00:00Z"

        def anchor_repo_context(*, record_store: Any, repo: str) -> str:
            del record_store
            anchor_calls.append(repo)
            return "managed-preview"

        def github_token(*, control_plane_root: Path, context_name: str) -> str:
            token_calls.append((control_plane_root, context_name))
            raise ClickException("token unavailable")

        dependencies = EveryCodeGitHubWebhookDependencies(
            webhook_secret=webhook_secret,
            verify_signature=verify_signature,
            now_timestamp=now_timestamp,
            anchor_repo_context=anchor_repo_context,
            github_token=github_token,
        )
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            body="/preview ok",
        )

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
                webhook_dependencies=dependencies,
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-injected-create",
                    "X-Hub-Signature-256": "injected-signature",
                },
            )
            preview_status, preview_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-injected-preview",
                    "X-Hub-Signature-256": "injected-signature",
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(
            create_payload["result"]["request"]["queued_at"],
            "2026-07-14T12:00:00Z",
        )
        self.assertEqual(preview_status, 202)
        self.assertEqual(preview_payload["reason"], "preview_validation_failed")
        self.assertEqual(len(verification_calls), 2)
        self.assertTrue(
            all(
                call[1:] == ("injected-signature", "injected-secret") for call in verification_calls
            )
        )
        self.assertEqual(anchor_calls, ["sellyouroutboard"])
        self.assertEqual(token_calls, [(root, "managed-preview")])

    def test_every_code_github_webhook_dedupes_existing_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            first_status, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            request_id = str(first_payload["records"]["request_id"])
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                request_id,
            )
            second_status, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-2",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(first_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(second_status, 202)
        self.assertTrue(second_payload["deduped"])
        self.assertEqual(second_payload["records"]["state"], "claimed")
        self.assertEqual(stored_request.state, "claimed")
        self.assertEqual(stored_request.github_delivery_id, "delivery-1")

    def test_every_code_github_webhook_dedupes_finished_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            first_status, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            request_id = str(first_payload["records"]["request_id"])
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                request_id,
            )
            done_status, _done_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
            )
            second_status, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-2",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(first_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(done_status, 202)
        self.assertEqual(second_status, 202)
        self.assertTrue(second_payload["deduped"])
        self.assertEqual(second_payload["records"]["state"], "done")
        self.assertEqual(
            second_payload["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/26",
        )

    def test_every_code_issue_close_marks_linked_request_done(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload()
        close_payload = _every_code_github_issue_labeled_payload(
            action="closed",
            closed_at="2026-05-06T16:20:00Z",
            state_reason="completed",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
            )

            close_status, closed_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=close_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue-close",
                    "X-Hub-Signature-256": _github_webhook_signature(close_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(closed_response["records"]["state"], "done")
        self.assertEqual(
            closed_response["result"]["request"]["finished_at"],
            "2026-05-06T16:20:00Z",
        )
        self.assertEqual(
            closed_response["result"]["request"]["result_summary"],
            "Source issue closed (completed): https://github.com/cbusillo/code/issues/123",
        )

    def test_every_code_pull_request_close_marks_linked_request_done(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload()
        pr_payload = _every_code_github_pull_request_closed_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
            )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "done")
        self.assertEqual(close_payload["result"]["request"]["finished_at"], "2026-05-06T16:20:00Z")
        self.assertEqual(close_payload["result"]["request"]["error_message"], "")
        self.assertEqual(
            close_payload["result"]["request"]["result_summary"],
            "Linked pull request merged: https://github.com/cbusillo/code/pull/26",
        )

    def test_every_code_pull_request_close_blocks_unmerged_linked_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload()
        pr_payload = _every_code_github_pull_request_closed_payload(merged=False)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
            )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "blocked")
        self.assertIn("closed without merge", close_payload["result"]["request"]["error_message"])

    def test_every_code_pull_request_close_matches_issue_url_without_result_pr_url(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=64)
        pr_payload = _every_code_github_pull_request_closed_payload(
            pr_number=71,
            body="Closes #64",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-feedback-close-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
            )
            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-feedback-close-pr",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "done")
        self.assertEqual(
            close_payload["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/71",
        )

    def test_every_code_pull_request_close_matches_all_linked_issue_urls(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["every_code_work_request.read"],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        pr_payload = _every_code_github_pull_request_closed_payload(
            pr_number=71,
            body="Closes #64, closes #65",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            for issue_number in (64, 65):
                issue_payload = _every_code_github_issue_labeled_payload(issue_number=issue_number)
                _invoke_app(
                    app,
                    method="POST",
                    path="/v1/every-code/github-webhook",
                    payload=issue_payload,
                    authorization="",
                    headers={
                        "X-GitHub-Event": "issues",
                        "X-GitHub-Delivery": f"delivery-multi-close-{issue_number}",
                        "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                    },
                )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-multi-close-pr",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )
            persisted_requests = FilesystemRecordStore(
                state_dir
            ).list_every_code_work_request_records()

        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["result"]["closed_count"], 2)
        closed_requests = close_payload["result"]["requests"]
        self.assertEqual({request["issue_number"] for request in closed_requests}, {64, 65})
        self.assertTrue(all(request["state"] == "done" for request in closed_requests))
        self.assertTrue(
            all(
                request["claimed_by_host"] == "github-pull-request-close"
                for request in closed_requests
            )
        )
        self.assertEqual(
            {request.state for request in persisted_requests},
            {"done"},
        )

    def test_every_code_pull_request_close_does_not_match_issue_by_pr_number(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=26)
        pr_payload = _every_code_github_pull_request_closed_payload(pr_number=26)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-pr-number-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
            )
            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-number-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertTrue(close_payload["skipped"])
        self.assertEqual(close_payload["reason"], "linked_every_code_request_not_found")

    def test_every_code_pull_request_close_pages_beyond_newest_requests(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        target_pr_payload = _every_code_github_pull_request_closed_payload(pr_number=26)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            for issue_number in range(1, 103):
                issue_payload = _every_code_github_issue_labeled_payload(issue_number=issue_number)
                _create_status, create_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/every-code/github-webhook",
                    payload=issue_payload,
                    authorization="",
                    headers={
                        "X-GitHub-Event": "issues",
                        "X-GitHub-Delivery": f"delivery-page-issue-{issue_number}",
                        "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                    },
                )
                request_id = str(create_payload["records"]["request_id"])
                _claim_every_code_work_request_in_filesystem(state_dir, request_id)
                _update_every_code_work_request_status_in_filesystem(
                    state_dir,
                    request_id,
                    state="running",
                    result_pr_url=f"https://github.com/cbusillo/code/pull/{issue_number}",
                )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=target_pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-page-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(target_pr_payload, secret),
                },
            )

        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "done")
        self.assertEqual(
            close_payload["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/26",
        )

    def test_every_code_pull_request_close_ignores_unlinked_pr(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        pr_payload = _every_code_github_pull_request_closed_payload(pr_number=999)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(identity),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "linked_every_code_request_not_found")

    def test_every_code_pr_comment_webhook_records_feedback(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            status_status, _status_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(status_status, 202)
        self.assertEqual(claim_payload["result"]["request"]["state"], "claimed")
        self.assertEqual(feedback_status, 202)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        feedback = feedback_response["result"]["feedback"]
        self.assertEqual(feedback["request_id"], request_id)
        self.assertEqual(feedback["feedback_kind"], "issue_comment")
        self.assertEqual(feedback["body"], "Please tighten this wording before merge.")

    def test_every_code_preview_ok_comment_marks_pr_ready_to_merge(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                side_effect=[
                    [{"name": "preview-approved"}],
                    {},
                    {},
                    [{"name": "ready-to-merge"}],
                    {"owner": {"login": "cbusillo", "type": "User"}},
                    {"assignees": [{"login": "cbusillo"}]},
                ],
            ) as github_request,
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                return_value={"id": 987},
            ) as create_comment,
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            status_status, _status_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            ok_status, ok_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-ok",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(status_status, 202)
        self.assertEqual(ok_status, 202, ok_response)
        preview_validation = ok_response["result"]["preview_validation"]
        self.assertEqual(preview_validation["command"], "ok")
        self.assertEqual(preview_validation["merge_owner"], "cbusillo")
        self.assertEqual(
            github_request.call_args_list[3].kwargs["body"], {"labels": ["ready-to-merge"]}
        )
        self.assertEqual(
            github_request.call_args_list[5].kwargs["body"], {"assignees": ["cbusillo"]}
        )
        create_comment.assert_called_once()
        self.assertIn("@cbusillo", create_comment.call_args.kwargs["body"])

    def test_every_code_preview_validation_failure_returns_generic_webhook_response(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        dependencies = EveryCodeGitHubWebhookDependencies(
            github_token=_failing_github_token,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
                webhook_dependencies=dependencies,
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-validation-failed",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertIs(payload["skipped"], True)
        self.assertEqual(payload["reason"], "preview_validation_failed")
        self.assertNotIn("message", payload)
        self.assertNotIn("Traceback", json.dumps(payload, sort_keys=True))

    def test_every_code_preview_ok_allows_repo_owner_override(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        dependencies = EveryCodeGitHubWebhookDependencies(
            github_token=_fixed_github_token,
        )
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            issue_author="Mbanks89",
            sender="cbusillo",
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                side_effect=[
                    [{"name": "preview-approved"}],
                    {},
                    {},
                    [{"name": "ready-to-merge"}],
                    {"owner": {"login": "cbusillo", "type": "User"}},
                    {"assignees": [{"login": "cbusillo"}]},
                ],
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                return_value={"id": 987},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
                webhook_dependencies=dependencies,
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            ok_status, ok_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-owner-ok",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(ok_status, 202, ok_response)
        self.assertEqual(ok_response["result"]["preview_validation"]["command"], "ok")

    def test_every_code_preview_comment_skips_untrusted_actor(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            issue_author="Mbanks89",
            sender="random-user",
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            _issue_status, _issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            ok_status, ok_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-untrusted-ok",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(ok_status, 202, ok_response)
        self.assertTrue(ok_response["skipped"])
        self.assertEqual(ok_response["reason"], "untrusted_actor")

    def test_every_code_preview_changes_routes_feedback_to_session(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        dependencies = EveryCodeGitHubWebhookDependencies(
            github_token=_fixed_github_token,
        )
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            body="/preview changes The delete button still misses bulk uploads.",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                side_effect=[
                    [{"name": "preview-changes-requested"}],
                    {},
                    {},
                    {},
                ],
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
                webhook_dependencies=dependencies,
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            status_status, _status_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            changes_status, changes_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-changes",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(status_status, 202)
        self.assertEqual(changes_status, 202, changes_response)
        preview_validation = changes_response["result"]["preview_validation"]
        self.assertEqual(preview_validation["command"], "changes")
        feedback = preview_validation["feedback_id"]
        self.assertIn("every-code-pr-feedback-cbusillo-sellyouroutboard-88", feedback)

    def test_every_code_pr_comment_webhook_matches_linked_issue(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=67)
        comment_payload = _every_code_github_pr_comment_payload(
            pr_number=75,
            issue_body="Closes #67",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            running_status, _running_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="running",
                result_summary="Visible tmux session.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(running_status, 202)
        self.assertIn("records", feedback_response)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        feedback = feedback_response["result"]["feedback"]
        self.assertEqual(feedback["request_id"], request_id)
        self.assertEqual(feedback["pr_number"], 75)
        self.assertEqual(feedback["pr_url"], "https://github.com/cbusillo/code/pull/75")

    def test_every_code_pr_comment_webhook_allows_configured_manager(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=67,
        )
        comment_payload = _every_code_github_pr_comment_payload(
            repository="cbusillo/sellyouroutboard",
            pr_number=75,
            issue_body="Closes #67",
            sender="Mbanks89",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            tempfile.TemporaryDirectory() as home_directory_name,
            patch.dict(
                os.environ,
                {
                    "HOME": home_directory_name,
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            _write_github_planning_config(
                Path(home_directory_name),
                repo_managers={"cbusillo/sellyouroutboard": "@Mbanks89"},
            )
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="running",
                result_summary="Visible tmux session.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-manager",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        self.assertEqual(feedback_response["result"]["feedback"]["actor"], "Mbanks89")

    def test_every_code_pr_comment_webhook_uses_second_planning_config_path(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=67,
        )
        comment_payload = _every_code_github_pr_comment_payload(
            repository="cbusillo/sellyouroutboard",
            pr_number=75,
            issue_body="Closes #67",
            sender="Mbanks89",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            tempfile.TemporaryDirectory() as home_directory_name,
            patch.dict(
                os.environ,
                {
                    "HOME": home_directory_name,
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            _write_github_planning_config(Path(home_directory_name), repo_managers={})
            _write_github_planning_config(
                Path(home_directory_name),
                path=".codex/github-planning.json",
                repo_managers={"cbusillo/sellyouroutboard": "@Mbanks89"},
            )
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue-second-config",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/75",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-second-config",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        self.assertEqual(feedback_response["result"]["feedback"]["actor"], "Mbanks89")

    def test_every_code_pr_comment_webhook_skips_untrusted_actor(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=67)
        comment_payload = _every_code_github_pr_comment_payload(
            pr_number=75,
            issue_body="Closes #67",
            sender="random-user",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            tempfile.TemporaryDirectory() as home_directory_name,
            patch.dict(
                os.environ,
                {
                    "HOME": home_directory_name,
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _write_github_planning_config(Path(home_directory_name), repo_managers={})
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-untrusted",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertTrue(feedback_response["skipped"])
        self.assertEqual(feedback_response["reason"], "untrusted_actor")
        self.assertEqual(feedback_records, ())

    def test_every_code_pr_comment_webhook_ignores_bot_feedback(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=67)
        comment_payload = _every_code_github_pr_comment_payload(
            pr_number=75,
            issue_body="Closes #67",
            body="Odoo preview refresh started for PR #75.",
            sender="github-actions[bot]",
            sender_type="Bot",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            _running_status, _running_payload = (
                _update_every_code_work_request_status_in_filesystem(
                    state_dir,
                    str(request_id),
                    state="running",
                    result_summary="Visible tmux session.",
                    updated_at="2026-05-06T16:00:00Z",
                )
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-bot",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertTrue(feedback_response["skipped"])
        self.assertEqual(feedback_response["reason"], "automation_actor")
        self.assertEqual(feedback_records, ())

    def test_every_code_pr_comment_webhook_dedupes_feedback(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload(comment_id=2002)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            first_status, first_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            second_status, second_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertEqual(
            first_response["result"]["feedback"]["feedback_id"],
            second_response["result"]["feedback_id"],
        )
        self.assertTrue(second_response["deduped"])

    def test_every_code_github_webhook_rejects_invalid_signature(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": "sha256=invalid",
                },
            )

        self.assertEqual(status_code, 401)
        self.assertEqual(payload["error"]["code"], "webhook_signature_invalid")

    def test_invalid_pull_request_signature_never_delegates_to_trusted_maintenance(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_pull_request_closed_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
            patch(
                "control_plane.every_code_github_webhook.handle_trusted_maintenance_github_webhook"
            ) as trusted_handler,
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-invalid-trusted-every-code",
                    "X-Hub-Signature-256": "sha256=invalid",
                },
            )

        self.assertEqual(status_code, 401)
        self.assertEqual(payload["error"]["code"], "webhook_signature_invalid")
        trusted_handler.assert_not_called()

    def test_every_code_github_webhook_rejects_invalid_json_payload(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        body_bytes = b'{"action":'
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, _headers, response_body = _invoke_http_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                authorization="",
                body_bytes=body_bytes,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-invalid-json",
                    "X-Hub-Signature-256": _github_webhook_body_signature(
                        body_bytes,
                        secret,
                    ),
                },
            )
            payload = json.loads(response_body.decode("utf-8"))

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_issue_payload(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        webhook_payload["repository"] = {}
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-malformed-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_bool_issue_number(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        issue = cast(dict[str, object], webhook_payload["issue"])
        issue["number"] = True
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-bool-issue-number",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_repository_name(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        repository = cast(dict[str, object], webhook_payload["repository"])
        repository["full_name"] = " cbusillo/code"
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-malformed-repository",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_pull_request_repository(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_pull_request_closed_payload()
        repository = cast(dict[str, object], webhook_payload["repository"])
        repository["full_name"] = " cbusillo/code"
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-malformed-pr-repository",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_pull_request_url(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_pull_request_closed_payload()
        pull_request = cast(dict[str, object], webhook_payload["pull_request"])
        pull_request["html_url"] = ""
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-malformed-pr-url",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_feedback_payload(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        comment = cast(dict[str, object], comment_payload["comment"])
        comment.pop("node_id")
        comment.pop("id")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(issue_response["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-malformed-feedback",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 400)
        self.assertEqual(feedback_response["status"], "rejected")
        self.assertEqual(feedback_response["error"]["code"], "invalid_request")
        self.assertEqual(
            feedback_response["error"]["message"], "GitHub webhook payload is invalid."
        )
        self.assertEqual(feedback_records, ())

    def test_every_code_github_webhook_rejects_malformed_feedback_repository(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        repository = cast(dict[str, object], comment_payload["repository"])
        repository["full_name"] = " cbusillo/code"
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(issue_response["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-malformed-feedback-repository",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 400)
        self.assertEqual(feedback_response["status"], "rejected")
        self.assertEqual(feedback_response["error"]["code"], "invalid_request")
        self.assertEqual(
            feedback_response["error"]["message"], "GitHub webhook payload is invalid."
        )
        self.assertEqual(feedback_records, ())

    def test_every_code_github_webhook_rejects_slug_unsafe_feedback_identity(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        comment = cast(dict[str, object], comment_payload["comment"])
        comment["node_id"] = "---"
        comment.pop("id")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(issue_response["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-slug-unsafe-feedback",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 400)
        self.assertEqual(feedback_response["status"], "rejected")
        self.assertEqual(feedback_response["error"]["code"], "invalid_request")
        self.assertEqual(
            feedback_response["error"]["message"], "GitHub webhook payload is invalid."
        )
        self.assertEqual(feedback_records, ())

    def test_every_code_github_webhook_ignores_other_labels(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload(label="bug")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "label_not_matched")

    def test_every_code_github_webhook_requires_configured_secret(self) -> None:
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": ""},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(
                        webhook_payload, "unused-secret"
                    ),
                },
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "webhook_secret_not_configured")


if __name__ == "__main__":
    unittest.main()
