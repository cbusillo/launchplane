import asyncio
import threading
import unittest
from datetime import UTC, datetime, timedelta
import click
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit
from typing import cast
from unittest.mock import patch

from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.contracts.preview_pr_feedback_remediation import (
    PreviewPrFeedbackObservation,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.preview_pr_feedback import DEFAULT_PREVIEW_FEEDBACK_MARKER
from control_plane.preview_pr_feedback_remediation import observe_managed_preview_pr_feedback
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.filesystem import FilesystemRecordStore
from fastapi import FastAPI

from tests.http_app_test_support import (
    _AsgiResponse,
    _asgi_get,
    _asgi_request,
    _local_operator_bearer_config,
)
from tests.support.auth import _identity, _local_operator_policy, _StubVerifier
from tests.support.profiles import _generic_site_profile_payload


def _installation_token() -> GitHubAppInstallationToken:
    return GitHubAppInstallationToken(
        token="token",
        app_id=76,
        installation_id=12,
        repository_id=7001,
        repository="cbusillo/verireel",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        app_slug="example-delivery",
    )


def _payload(*, mode: str) -> dict[str, object]:
    pull_request_url = "https://github.com/cbusillo/verireel/pull/311"
    payload: dict[str, object] = {
        "mode": mode,
        "product": "verireel",
        "context": "verireel-preview",
        "repository": "cbusillo/verireel",
        "pull_request_url": pull_request_url,
        "terminal_status": "cleared",
        "reason": "Clear a stale cleanup warning from a retired workflow identity.",
        "related_issue": "cbusillo/launchplane#2076",
    }
    if mode == "apply":
        payload["confirmation"] = f"remediate preview feedback {pull_request_url} to cleared"
    return payload


def _operator_app(root: Path, store: FilesystemRecordStore) -> FastAPI:
    profile_payload = _generic_site_profile_payload(product="verireel")
    profile_payload["repository"] = "cbusillo/verireel"
    preview_payload = cast(dict[str, object], profile_payload["preview"])
    preview_payload["context"] = "verireel-preview"
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord.model_validate(profile_payload)
    )
    return create_launchplane_fastapi_app(
        verifier=_StubVerifier(_identity()),
        authz_policy=_local_operator_policy(
            actions=(
                "preview_pr_feedback_remediation.plan",
                "preview_pr_feedback_remediation.apply",
            ),
            products=("verireel",),
            contexts=("verireel-preview",),
        ),
        bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
        control_plane_root_path=root,
        record_store_factory=lambda: store,
    )


class PreviewPrFeedbackRemediationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_destroyed_feedback_dry_run_observes_with_installation_token(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = _operator_app(root, store)
            calls: list[tuple[str, str]] = []

            def provider(*, path: str, token: str, method: str = "GET") -> object:
                calls.append((method, path))
                if path == "/user":
                    raise click.ClickException(
                        "GitHub API request failed for /user: HTTP Error 403: Forbidden"
                    )
                if unquote(path) == "/users/example-delivery[bot]":
                    return {"id": 101, "login": "example-delivery[bot]", "type": "Bot"}
                if urlsplit(path).path.endswith("/issues/311/comments"):
                    return []
                if path == "/installation/token" and method == "DELETE":
                    return None
                raise AssertionError((method, path))

            payload = _payload(mode="dry-run")
            payload["terminal_status"] = "destroyed"
            with (
                patch(
                    "control_plane.preview_pr_feedback_remediation.resolve_delivery_github_installation_token",
                    return_value=_installation_token(),
                ),
                patch(
                    "control_plane.preview_pr_feedback_remediation.github_api_request",
                    side_effect=provider,
                ),
                patch(
                    "control_plane.http_app.observe_managed_preview_pr_feedback",
                    side_effect=lambda **kwargs: observe_managed_preview_pr_feedback(
                        api_request=provider, **kwargs
                    ),
                ),
                patch(
                    "control_plane.preview_pr_feedback_remediation.delete_github_issue_comment"
                ) as delete,
                patch(
                    "control_plane.preview_pr_feedback_remediation.update_github_issue_comment"
                ) as update,
            ):
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "destroyed-preview-observation",
                    },
                    payload=payload,
                )
            self.assertEqual(response.status_code, 202, response.text)
            result = response.json()["result"]
            self.assertEqual(result["observation"]["state"], "absent")
            self.assertEqual(result["observation"]["token_actor_id"], 101)
            self.assertEqual(result["planned_action"], "none")
            self.assertFalse(result["mutation_evidence"]["attempted"])
            self.assertEqual(len(store.list_preview_pr_feedback_remediation_records()), 1)
            self.assertNotIn(("GET", "/user"), calls)
            self.assertIn(("DELETE", "/installation/token"), calls)
            delete.assert_not_called()
            update.assert_not_called()

    async def test_bot_user_id_controls_managed_marker_ownership(self) -> None:
        bot = {"id": 101, "login": "example-delivery[bot]", "type": "Bot"}
        issued = _installation_token()
        for author_id in (bot["id"], issued.app_id, issued.installation_id):
            with self.subTest(author_id=author_id), TemporaryDirectory() as name:
                root = Path(name)
                store = FilesystemRecordStore(state_dir=root / "state")
                app = _operator_app(root, store)
                calls: list[tuple[str, str]] = []

                def provider(*, path: str, token: str, method: str = "GET") -> object:
                    calls.append((method, path))
                    if unquote(path) == f"/users/{bot['login']}":
                        return bot
                    if urlsplit(path).path.endswith("/issues/311/comments"):
                        return [
                            {
                                "id": 55,
                                "body": DEFAULT_PREVIEW_FEEDBACK_MARKER,
                                "user": {"id": author_id, "login": bot["login"]},
                            }
                        ]
                    if path == "/installation/token" and method == "DELETE":
                        return None
                    raise AssertionError((method, path))

                payload = _payload(mode="dry-run")
                payload["terminal_status"] = "destroyed"
                with (
                    patch(
                        "control_plane.preview_pr_feedback_remediation.resolve_delivery_github_installation_token",
                        return_value=issued,
                    ),
                    patch(
                        "control_plane.preview_pr_feedback_remediation.github_api_request",
                        side_effect=provider,
                    ),
                    patch(
                        "control_plane.http_app.observe_managed_preview_pr_feedback",
                        side_effect=lambda **kwargs: observe_managed_preview_pr_feedback(
                            api_request=provider, **kwargs
                        ),
                    ),
                    patch(
                        "control_plane.preview_pr_feedback_remediation.update_github_issue_comment"
                    ) as update,
                    patch(
                        "control_plane.preview_pr_feedback_remediation.delete_github_issue_comment"
                    ) as delete,
                ):
                    response = await _asgi_request(
                        app,
                        "POST",
                        "/v1/previews/pr-feedback/remediation",
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": "marker-owner",
                        },
                        payload=payload,
                    )
                if author_id == bot["id"]:
                    self.assertEqual(response.status_code, 202, response.text)
                    result = response.json()["result"]
                    self.assertEqual(result["planned_action"], "replace_comment")
                    self.assertEqual(result["observation"]["comment_author_id"], bot["id"])
                    self.assertFalse(result["mutation_evidence"]["attempted"])
                else:
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(store.list_preview_pr_feedback_remediation_records(), ())
                self.assertIn(("DELETE", "/installation/token"), calls)
                update.assert_not_called()
                delete.assert_not_called()

    async def test_operator_dry_run_and_apply_record_already_absent(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = _operator_app(root, store)
            observation = PreviewPrFeedbackObservation(
                state="absent",
                digest_sha256="a" * 64,
                token_actor_id=101,
                token_actor_login="launchplane-bot",
            )
            headers = {
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "preview-feedback-remediation-verireel-311",
            }
            revoked: list[str] = []

            def revoke(**kwargs: object) -> object:
                if str(kwargs["path"]).startswith("/users/"):
                    return {"id": 101, "login": "example-delivery[bot]", "type": "Bot"}
                self.assertEqual(kwargs["path"], "/installation/token")
                self.assertEqual(kwargs["method"], "DELETE")
                token = kwargs["token"]
                assert isinstance(token, str)
                revoked.append(token)
                return None

            with (
                patch(
                    "control_plane.preview_pr_feedback_remediation.resolve_delivery_github_installation_token",
                    return_value=_installation_token(),
                ),
                patch(
                    "control_plane.preview_pr_feedback_remediation.github_api_request",
                    side_effect=revoke,
                ),
                patch(
                    "control_plane.http_app.observe_managed_preview_pr_feedback",
                    return_value=observation,
                ),
            ):
                missing_key = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=_payload(mode="dry-run"),
                )
                unmatched_apply = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "preview-feedback-remediation-unmatched",
                    },
                    payload=_payload(mode="apply"),
                )
                dry_run = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers=headers,
                    payload=_payload(mode="dry-run"),
                )
                apply = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers=headers,
                    payload=_payload(mode="apply"),
                )

            self.assertEqual(revoked, ["token", "token", "token"])
            self.assertEqual(missing_key.status_code, 400, missing_key.text)
            self.assertEqual(unmatched_apply.status_code, 409, unmatched_apply.text)
            self.assertEqual(unmatched_apply.json()["error"]["code"], "matching_dry_run_required")
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(apply.status_code, 202, apply.text)
            self.assertEqual(apply.json()["result"]["outcome"], "already_absent")
            self.assertFalse(apply.json()["result"]["mutation_evidence"]["mutated"])
            records = store.list_preview_pr_feedback_remediation_records(
                actor="local-operator:local-owner-agent",
                idempotency_key="preview-feedback-remediation-verireel-311",
            )
            self.assertEqual({record.mode for record in records}, {"apply", "dry-run"})

    async def test_slow_provider_keeps_event_loop_free_and_revokes_after_cancellation(
        self,
    ) -> None:
        loop_thread = threading.get_ident()
        started = threading.Event()
        release = threading.Event()
        revoked = threading.Event()
        revocations: list[tuple[str, int]] = []

        def slow_observation(**_: object) -> PreviewPrFeedbackObservation:
            started.set()
            if not release.wait(5):
                raise ValueError("fixture timed out")
            return PreviewPrFeedbackObservation(
                state="absent",
                digest_sha256="a" * 64,
                token_actor_id=101,
                token_actor_login="launchplane-bot",
            )

        def revoke(**kwargs: object) -> object:
            if str(kwargs["path"]).startswith("/users/"):
                return {"id": 101, "login": "example-delivery[bot]", "type": "Bot"}
            revocations.append((str(kwargs["token"]), threading.get_ident()))
            revoked.set()
            return None

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = _operator_app(root, FilesystemRecordStore(state_dir=root / "state"))
            with (
                patch(
                    "control_plane.preview_pr_feedback_remediation.resolve_delivery_github_installation_token",
                    return_value=_installation_token(),
                ),
                patch(
                    "control_plane.preview_pr_feedback_remediation.github_api_request",
                    side_effect=revoke,
                ),
                patch(
                    "control_plane.http_app.observe_managed_preview_pr_feedback",
                    side_effect=slow_observation,
                ),
            ):
                task = asyncio.ensure_future(
                    _asgi_request(
                        app,
                        "POST",
                        "/v1/previews/pr-feedback/remediation",
                        headers={
                            "Authorization": "Bearer local-operator-token",
                            "Idempotency-Key": "slow-provider",
                        },
                        payload=_payload(mode="dry-run"),
                    )
                )
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 3))
                    healthy = await asyncio.wait_for(_asgi_get(app, "/v1/health"), 2)
                    self.assertEqual(healthy.status_code, 200)
                    self.assertFalse(task.done(), "the event loop waited for the blocked provider")
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
                self.assertTrue(await asyncio.to_thread(revoked.wait, 5))

        self.assertEqual([token for token, _ in revocations], ["token"])
        self.assertNotEqual(revocations[0][1], loop_thread)

    async def test_same_key_apply_waits_for_the_running_worker_after_disconnect(self) -> None:
        started = threading.Event()
        release = threading.Event()
        observations = 0

        def observation(**_: object) -> PreviewPrFeedbackObservation:
            nonlocal observations
            observations += 1
            if observations == 2:
                started.set()
                if not release.wait(5):
                    raise ValueError("fixture timed out")
            return PreviewPrFeedbackObservation(
                state="absent",
                digest_sha256="a" * 64,
                token_actor_id=101,
                token_actor_login="launchplane-bot",
            )

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = _operator_app(root, store)

            async def remediate(mode: str) -> _AsgiResponse:
                return await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "same-key",
                    },
                    payload=_payload(mode=mode),
                )

            with (
                patch(
                    "control_plane.preview_pr_feedback_remediation.resolve_delivery_github_installation_token",
                    return_value=_installation_token(),
                ),
                patch(
                    "control_plane.preview_pr_feedback_remediation.github_api_request",
                    side_effect=lambda **kwargs: (
                        {"id": 101, "login": "example-delivery[bot]", "type": "Bot"}
                        if str(kwargs["path"]).startswith("/users/")
                        else None
                    ),
                ),
                patch(
                    "control_plane.http_app.observe_managed_preview_pr_feedback",
                    side_effect=observation,
                ),
            ):
                dry_run = await remediate("dry-run")
                self.assertEqual(dry_run.status_code, 202, dry_run.text)
                first_apply = asyncio.ensure_future(remediate("apply"))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 3))
                    first_apply.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await first_apply
                    duplicate = await asyncio.wait_for(remediate("apply"), 2)
                finally:
                    release.set()
                self.assertEqual(duplicate.status_code, 409, duplicate.text)
                self.assertEqual(duplicate.json()["error"]["code"], "mutation_in_progress")
                replay = None
                for _ in range(50):
                    replay = await remediate("apply")
                    if replay.status_code != 409:
                        break
                    await asyncio.sleep(0.1)

            applies = [
                record
                for record in store.list_preview_pr_feedback_remediation_records(
                    actor="local-operator:local-owner-agent",
                    idempotency_key="same-key",
                )
                if record.mode == "apply"
            ]

        assert replay is not None
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(len(applies), 1)

    async def test_github_actions_identity_cannot_use_operator_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_policy(
                actions=("preview_pr_feedback_remediation.plan",),
            ),
            record_store_factory=lambda: FilesystemRecordStore(
                state_dir=Path("/tmp/launchplane-remediation-test")
            ),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/previews/pr-feedback/remediation",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "denied",
            },
            payload=_payload(mode="dry-run"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")


if __name__ == "__main__":
    unittest.main()
