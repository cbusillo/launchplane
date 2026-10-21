from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import (
    HumanSessionManager,
    InMemoryHumanSessionStore,
    LaunchplaneHumanSession,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.http import request
from tests.support.profiles import _generic_site_profile_payload


@contextmanager
def browser_sweep(
    *, role: str = "admin", policy: LaunchplaneAuthzPolicy | None = None
) -> Iterator[tuple[FastAPI, HumanSessionManager, LaunchplaneHumanSession]]:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = FilesystemRecordStore(state_dir=root / "state")
        for product in ("syo", "verireel"):
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _generic_site_profile_payload(product=product)
                )
            )
        manager = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        human = _github_human_identity()
        if role == "read_only":
            human = replace(human, role="read_only")
        elif role == "owner":
            human = replace(human, role="owner")
        session = manager.issue(human)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy
            or LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "github_ids": [human.github_id],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            ),
            human_session_manager=manager,
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="test-terminal-token",
                terminal_agent_subject="test-terminal",
                terminal_agent_token_label="test-read",
                local_operator_token="test-operator-token",
                local_operator_subject="test-operator",
                local_operator_token_label="test-write",
            ),
            control_plane_root_path=root,
            record_store_factory=lambda: store,
        )
        yield app, manager, session


class PreviewLifecycleBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_administrator_report_and_apply_retain_sweep_and_replay_contract(
        self,
    ) -> None:
        for apply in (False, True):
            with self.subTest(apply=apply), browser_sweep() as (app, manager, session):
                with patch("control_plane.http_app.build_preview_lifecycle_sweep") as sweep:
                    sweep.return_value = {
                        "status": "fail",
                        "apply": apply,
                        "profiles": [
                            {
                                "product": "syo",
                                "status": "blocked",
                                "error_message": "Held previews require original-intent recovery.",
                            }
                        ],
                    }
                    body = {"apply": apply}
                    headers = {
                        **_browser_mutation_headers(manager, session),
                        "Idempotency-Key": "browser-sweep",
                    }
                    first = await request(
                        app, "POST", "/v1/previews/lifecycle-sweep", headers=headers, payload=body
                    )
                    self.assertEqual(first.status_code, 202, first.text)
                    self.assertEqual(first.json()["result"]["status"], "fail")
                    self.assertEqual(sweep.call_args.kwargs["request"].apply, apply)
                    self.assertEqual(
                        {p.product for p in sweep.call_args.kwargs["requested_profiles"]},
                        {"syo", "verireel"},
                    )
                    self.assertEqual(sweep.call_args.kwargs["denied_actions_by_product"], {})
                    replay = await request(
                        app,
                        "POST",
                        "/v1/previews/lifecycle-sweep",
                        headers={
                            **_browser_mutation_headers(manager, session),
                            "Idempotency-Key": "browser-sweep",
                        },
                        payload=body,
                    )
                    self.assertEqual(replay.status_code, 202, replay.text)
                    self.assertTrue(replay.json()["replayed"])
                    self.assertEqual(replay.json()["result"], first.json()["result"])
                    conflict = await request(
                        app,
                        "POST",
                        "/v1/previews/lifecycle-sweep",
                        headers={
                            **_browser_mutation_headers(manager, session),
                            "Idempotency-Key": "browser-sweep",
                        },
                        payload={"apply": not apply},
                    )
                    self.assertEqual(conflict.status_code, 409, conflict.text)
                    sweep.assert_called_once()

    async def test_report_and_apply_require_current_same_origin_csrf(self) -> None:
        for apply in (False, True):
            for invalid in ("missing", "cross-site", "replayed"):
                with (
                    self.subTest(apply=apply, invalid=invalid),
                    browser_sweep() as (app, manager, session),
                ):
                    headers = _browser_mutation_headers(manager, session)
                    if invalid == "missing":
                        headers.pop("X-CSRF-Token")
                    elif invalid == "cross-site":
                        headers["Origin"] = "https://other.example"
                    else:
                        manager.consume_csrf_token(session, headers["X-CSRF-Token"])
                    with patch("control_plane.http_app.build_preview_lifecycle_sweep") as sweep:
                        response = await request(
                            app,
                            "POST",
                            "/v1/previews/lifecycle-sweep",
                            headers=headers,
                            payload={"apply": apply},
                        )
                        self.assertEqual(response.status_code, 403, response.text)
                        self.assertEqual(
                            response.json()["error"]["code"], "browser_mutation_denied"
                        )
                        sweep.assert_not_called()

    async def test_client_and_read_only_sessions_cannot_start_either_mode(self) -> None:
        for role in ("owner", "read_only"):
            for apply in (False, True):
                with (
                    self.subTest(role=role, apply=apply),
                    browser_sweep(role=role) as (app, manager, session),
                ):
                    with patch("control_plane.http_app.build_preview_lifecycle_sweep") as sweep:
                        response = await request(
                            app,
                            "POST",
                            "/v1/previews/lifecycle-sweep",
                            headers=_browser_mutation_headers(manager, session),
                            payload={"apply": apply},
                        )
                        self.assertIn(response.status_code, (401, 403), response.text)
                        sweep.assert_not_called()

    async def test_terminal_and_ungranted_operator_bearers_remain_denied(self) -> None:
        for token in ("test-terminal-token", "test-operator-token"):
            for apply in (False, True):
                with self.subTest(token=token, apply=apply), browser_sweep() as (app, _, _session):
                    with patch("control_plane.http_app.build_preview_lifecycle_sweep") as sweep:
                        response = await request(
                            app,
                            "POST",
                            "/v1/previews/lifecycle-sweep",
                            headers={"Authorization": f"Bearer {token}"},
                            payload={"apply": apply},
                        )
                        self.assertEqual(response.status_code, 403, response.text)
                        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
                        sweep.assert_not_called()

    async def test_admin_role_without_existing_product_authority_is_denied(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_humans": [
                    {
                        "github_ids": [_github_human_identity().github_id],
                        "roles": ["admin"],
                        "products": ["syo"],
                        "contexts": ["syo"],
                        "actions": ["preview_lifecycle.plan"],
                    }
                ]
            }
        )
        with browser_sweep(policy=policy) as (app, manager, session):
            with patch("control_plane.http_app.build_preview_lifecycle_sweep") as sweep:
                response = await request(
                    app,
                    "POST",
                    "/v1/previews/lifecycle-sweep",
                    headers=_browser_mutation_headers(manager, session),
                    payload={"apply": True},
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    response.json()["authz"]["request"]["action"], "preview_lifecycle.cleanup"
                )
                sweep.assert_not_called()
