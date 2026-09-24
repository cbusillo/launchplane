import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import Literal
from httpx2 import Response
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle

from cryptography.fernet import Fernet

from control_plane import secrets
from control_plane.owner_secret_inputs import resolve_owner_secret_submission
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_key_safety_policy import RuntimeKeySafetyPolicyRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import sqlite_database_url

_READ = "/v1/owner-secret-inputs?product=example-site&environment=testing"
_SUBMIT = "/v1/owner-secret-inputs/submit"
_CONFIG = "/v1/products/example-site/environments/testing/config/apply"


def _human(
    login: str = "owner", github_id: int = 9001, role: Literal["read_only", "admin"] = "read_only"
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name=login,
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role=role,
    )


class OwnerSecretInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary.name) / "test.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        payload = _generic_site_profile_payload()
        payload["owner"] = {"github_id": "9001", "github_login": "owner"}
        payload["expected_config"] = {
            "managed_secret_bindings": [
                {
                    "binding_key": "SMTP_PASSWORD",
                    "context": "example-site",
                    "instance": "testing",
                    "owner_input": {
                        "label": "Mail credential",
                        "instructions": "Provide the existing account's app credential.",
                    },
                },
                {
                    "binding_key": "DATABASE_PASSWORD",
                    "context": "example-site",
                    "instance": "testing",
                },
            ]
        }
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["operator"],
                            "roles": ["admin"],
                            "products": ["example-site"],
                            "contexts": ["launchplane", "example-site"],
                            "actions": [
                                "product_profile.read",
                                "product_config.plan",
                                "product_config.apply",
                            ],
                        }
                    ]
                }
            ),
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_SECRET_KEYS_JSON": json.dumps(
                        {"active_key_id": "test", "keys": {"test": Fernet.generate_key().decode()}}
                    )
                },
            )
        )

    async def read(self, human: GitHubHumanIdentity | None = None) -> Response:
        session = self.sessions.issue(human or _human())
        return await _asgi_get(
            self.app, _READ, headers={"Cookie": self.sessions.session_cookie_header(session)}
        )

    async def post(
        self,
        payload: dict[str, object],
        *,
        human: GitHubHumanIdentity | None = None,
        path: str = _SUBMIT,
        key: str = "",
    ) -> Response:
        session = self.sessions.issue(human or _human())
        headers = _browser_mutation_headers(self.sessions, session)
        if key:
            headers["Idempotency-Key"] = key
        return await _asgi_request(self.app, "POST", path, headers=headers, payload=payload)

    async def submission(self, value: str = "mail-secret-value") -> dict[str, object]:
        field = (await self.read()).json()["fields"][0]
        return {
            "product": "example-site",
            "environment": "testing",
            "request_revision": field["request_revision"],
            "value": value,
        }

    async def test_owner_submission_is_encrypted_and_has_no_runtime_binding(self) -> None:
        before = await self.read()
        self.assertEqual([field["label"] for field in before.json()["fields"]], ["Mail credential"])
        response = await self.post(await self.submission(), human=_human("renamed-owner"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("mail-secret-value", response.text)
        self.assertEqual(self.store.list_secret_bindings(), ())
        self.assertEqual(self.store.list_runtime_environment_records(), ())
        field = (await self.read()).json()["fields"][0]
        version = self.store.read_secret_version(field["submission_version_id"])
        self.assertNotIn("mail-secret-value", version.model_dump_json())
        self.assertEqual(
            secrets._decrypt_secret_value(version.ciphertext, version.key_id), "mail-secret-value"
        )
        self.assertEqual(version.created_by, "github:9001")

    async def test_non_owner_and_stale_owner_cannot_submit(self) -> None:
        payload = await self.submission()
        for human in (_human(github_id=9002), _human("operator", 9003, "admin")):
            response = await self.post(payload, human=human)
            self.assertEqual(response.status_code, 403, response.text)
        changed = self.profile.model_dump()
        changed["owner"] = {"github_id": "9010", "github_login": "next-owner"}
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(changed)
        )
        self.assertEqual((await self.post(payload)).status_code, 403)
        self.assertEqual(
            (await self.post(payload, human=_human("next-owner", 9010))).status_code, 409
        )
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_changed_request_and_cross_environment_are_rejected_without_leaking_value(
        self,
    ) -> None:
        payload = await self.submission()
        wrong_environment = await self.post({**payload, "environment": "prod"})
        self.assertEqual(wrong_environment.status_code, 409)
        changed = self.profile.model_dump()
        changed["expected_config"]["managed_secret_bindings"][0]["owner_input"] = None
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(changed)
        )
        disabled = await self.post(payload)
        malformed = await self.post({**payload, "value": {"secret": "mail-secret-value"}})
        self.assertEqual(disabled.status_code, 409)
        self.assertEqual(malformed.status_code, 400)
        self.assertNotIn(
            "mail-secret-value", wrong_environment.text + disabled.text + malformed.text
        )
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_missing_csrf_and_missing_encryption_never_write(self) -> None:
        payload = await self.submission()
        session = self.sessions.issue(_human())
        response = await _asgi_request(
            self.app,
            "POST",
            _SUBMIT,
            headers={"Cookie": self.sessions.session_cookie_header(session)},
            payload=payload,
        )
        self.assertEqual(response.status_code, 403)
        with patch.dict(os.environ, {}, clear=True):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_key_rotation_preserves_the_owner_receipt_and_applicable_value(self) -> None:
        with patch(
            "control_plane.owner_secret_inputs.utc_now_timestamp",
            return_value="2026-09-24T01:00:00Z",
        ):
            written = await self.post(await self.submission())
        before = written.json()["fields"][0]
        key_ring = json.loads(os.environ["LAUNCHPLANE_SECRET_KEYS_JSON"])
        key_ring["keys"]["rotated"] = Fernet.generate_key().decode()
        key_ring["active_key_id"] = "rotated"
        with (
            patch.dict(os.environ, {"LAUNCHPLANE_SECRET_KEYS_JSON": json.dumps(key_ring)}),
            patch("control_plane.secrets.utc_now_timestamp", return_value="2026-09-24T02:00:00Z"),
        ):
            plan = secrets.reencrypt_secrets(record_store=self.store)
            plan_digest = plan["plan_digest"]
            assert isinstance(plan_digest, str)
            applied = secrets.reencrypt_secrets(
                record_store=self.store,
                apply=True,
                expected_plan_digest=plan_digest,
                actor="operator:key-rotation",
                reason="Rotate the test key.",
            )
            self.assertEqual(applied["status"], "ok")
            after = (await self.read()).json()["fields"][0]
            self.assertNotEqual(before["submission_version_id"], after["submission_version_id"])
            self.assertEqual(after["submitted_at"], "2026-09-24T01:00:00Z")
            value = resolve_owner_secret_submission(
                self.store,
                profile=self.profile,
                lane=self.profile.lanes[0],
                requirement=self.profile.expected_config.managed_secret_bindings[0],
                version_id=after["submission_version_id"],
            )
            self.assertEqual(value, "mail-secret-value")
        self.assertEqual(self.store.list_secret_bindings(), ())

    async def test_shared_product_context_request_has_one_receipt_for_its_named_environments(
        self,
    ) -> None:
        payload = self.profile.model_dump()
        payload["expected_config"]["managed_secret_bindings"][0]["instance"] = ""
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        written = await self.post(await self.submission())
        field = written.json()["fields"][0]
        self.assertEqual(field["environments"], ["prod", "testing"])
        session = self.sessions.issue(_human())
        production = await _asgi_get(
            self.app,
            _READ.replace("environment=testing", "environment=prod"),
            headers={"Cookie": self.sessions.session_cookie_header(session)},
        )
        self.assertEqual(
            production.json()["fields"][0]["submission_version_id"], field["submission_version_id"]
        )
        value = resolve_owner_secret_submission(
            self.store,
            profile=self.profile,
            lane=self.profile.lanes[1],
            requirement=self.profile.expected_config.managed_secret_bindings[0],
            version_id=field["submission_version_id"],
        )
        self.assertEqual(value, "mail-secret-value")
        self.assertEqual(self.store.list_secret_bindings(), ())

    async def test_operator_receives_a_bounded_error_when_the_saved_key_is_unavailable(
        self,
    ) -> None:
        written = await self.post(await self.submission())
        payload: dict[str, object] = {
            "mode": "dry-run",
            "reason": "Apply the mail credential.",
            "managed_secrets": [
                {
                    "integration": "runtime_environment",
                    "binding_key": "SMTP_PASSWORD",
                    "owner_submission_version_id": written.json()["fields"][0][
                        "submission_version_id"
                    ],
                }
            ],
        }
        with patch.dict(os.environ, {}, clear=True):
            response = await self.post(
                payload, human=_human("operator", 9003, "admin"), path=_CONFIG
            )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "secret_storage_unavailable")
        self.assertNotIn("mail-secret-value", response.text)

    async def test_owner_change_between_authorization_and_commit_rolls_back_submission(
        self,
    ) -> None:
        payload = await self.submission()
        write_bundle = self.store.write_product_authority_bundle

        def change_owner_then_write(bundle: ProductAuthorityBundle) -> None:
            changed = self.profile.model_dump()
            changed["owner"] = {"github_id": "9010", "github_login": "next-owner"}
            self.store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(changed)
            )
            write_bundle(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=change_owner_then_write
        ):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_operator_applies_pinned_submission_and_later_owner_input_does_not_rotate_runtime(
        self,
    ) -> None:
        written = await self.post(await self.submission())
        version_id = written.json()["fields"][0]["submission_version_id"]
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord.model_validate(
                {
                    "record_id": "test-policy",
                    "source": "test",
                    "updated_at": "2026-09-24T00:00:00Z",
                    "rules": [
                        {
                            "binding_key": "SMTP_PASSWORD",
                            "secret_class": "testing",
                            "allowed_contexts": ["example-site"],
                            "allowed_instances": ["testing"],
                        }
                    ],
                }
            )
        )
        payload: dict[str, object] = {
            "mode": "dry-run",
            "reason": "Configure the supplied mail credential.",
            "managed_secrets": [
                {
                    "integration": "runtime_environment",
                    "binding_key": "SMTP_PASSWORD",
                    "owner_submission_version_id": version_id,
                }
            ],
        }
        operator = _human("operator", 9003, "admin")
        denied = await self.post(payload, path=_CONFIG)
        self.assertEqual(denied.status_code, 404)
        planned = await self.post(payload, human=operator, path=_CONFIG)
        self.assertEqual(planned.status_code, 202, planned.text)
        applied = await self.post(
            {**payload, "mode": "apply", "confirmation": "APPLY example-site/testing"},
            human=operator,
            path=_CONFIG,
            key="apply-mail-submission",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        active_before = secrets.resolve_secret_values_for_integration_from_store(
            record_store=self.store,
            integration="runtime_environment",
            context_name="example-site",
            instance_name="testing",
        )
        self.assertEqual(active_before["SMTP_PASSWORD"], "mail-secret-value")
        await self.post(await self.submission("replacement-stays-pending"))
        active_after = secrets.resolve_secret_values_for_integration_from_store(
            record_store=self.store,
            integration="runtime_environment",
            context_name="example-site",
            instance_name="testing",
        )
        self.assertEqual(active_after, active_before)
        # A lost response remains recoverable even after replacement and key retirement.
        with patch.dict(os.environ, {}, clear=True):
            replay = await self.post(
                {**payload, "mode": "apply", "confirmation": "APPLY example-site/testing"},
                human=operator,
                path=_CONFIG,
                key="apply-mail-submission",
            )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
        conflicting_retry = await self.post(
            {
                **payload,
                "mode": "apply",
                "confirmation": "APPLY example-site/testing",
                "reason": "Changed request",
            },
            human=operator,
            path=_CONFIG,
            key="apply-mail-submission",
        )
        self.assertEqual(conflicting_retry.status_code, 409)
        self.assertEqual(conflicting_retry.json()["error"]["code"], "idempotency_key_reused")
        stale_plan = await self.post(payload, human=operator, path=_CONFIG)
        self.assertEqual(stale_plan.status_code, 409)
        self.assertIn("Refresh", stale_plan.json()["error"]["message"])
        self.assertNotIn("mail-secret-value", planned.text + applied.text + stale_plan.text)
