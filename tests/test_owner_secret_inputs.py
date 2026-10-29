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
from control_plane.artifact_scan import GHCR_ARTIFACT_READ_INTEGRATION
from control_plane.contracts.owner_control_shadow_verifier import (
    OwnerControlShadowVerifierConflictError,
)
from control_plane.owner_secret_inputs import resolve_owner_secret_submission
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
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
    def seed_director(self) -> None:
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="director-policy",
                source="test",
                updated_at="2026-10-23T00:00:00Z",
                policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_humans": [
                            {
                                "github_ids": [9004],
                                "roles": ["admin"],
                                "actions": ["authz_policy_grant.write"],
                                "products": ["launchplane"],
                                "contexts": ["launchplane"],
                            }
                        ]
                    }
                ),
            )
        )

    async def service_read(
        self, human: GitHubHumanIdentity, environment: str = GHCR_ARTIFACT_READ_INTEGRATION
    ) -> Response:
        session = self.sessions.issue(human)
        return await _asgi_get(
            self.app,
            f"/v1/owner-secret-inputs?product=launchplane&environment={environment}",
            headers={"Cookie": self.sessions.session_cookie_header(session)},
        )

    async def test_service_inputs_keep_onboarding_and_maintenance_secrets_separate(self) -> None:
        self.seed_director()
        director = _human("director", 9004, "admin")
        for environment in ("onboarding_github_app", "trusted_maintenance_webhook"):
            with self.subTest(environment=environment):
                read = await self.service_read(director, environment)
                self.assertEqual(read.status_code, 200, read.text)
                (field,) = read.json()["fields"]
                self.assertEqual(field["multiline"], environment == "onboarding_github_app")
                value = (
                    "synthetic-line-one\nsynthetic-line-two\n"
                    if field["multiline"]
                    else f"synthetic-{environment}-value"
                )
                payload = {
                    "product": "launchplane",
                    "environment": environment,
                    "request_revision": field["request_revision"],
                    "value": value,
                }
                wrong_purpose = await self.post(
                    {**payload, "environment": GHCR_ARTIFACT_READ_INTEGRATION}, human=director
                )
                self.assertEqual(wrong_purpose.status_code, 409, wrong_purpose.text)
                for human in (_human(), _human("operator", 9002, "admin")):
                    denied_read = await self.service_read(human, environment)
                    self.assertEqual(denied_read.status_code, 403, denied_read.text)
                    denied = await self.post(payload, human=human)
                    self.assertEqual(denied.status_code, 403, denied.text)
                response = await self.post(payload, human=director)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(value, response.text)
                (binding,) = self.store.list_secret_bindings(integration=environment)
                record = self.store.read_secret_record(binding.secret_id)
                self.assertEqual(
                    (record.scope, record.context, record.instance), ("context", "launchplane", "")
                )
                self.assertEqual(record.integration, environment)
                self.assertEqual(binding.binding_key, field["binding_key"])
                version = self.store.read_secret_version(record.current_version_id)
                self.assertEqual(
                    secrets._decrypt_secret_value(version.ciphertext, version.key_id), value
                )
                self.assertEqual(
                    secrets.resolve_context_secret_value(
                        integration=environment,
                        context_name="launchplane",
                        binding_key=field["binding_key"],
                        database_url=self.store.database_url,
                    ),
                    value.strip(),
                )
                stale = await self.post(payload, human=director)
                self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.store.list_runtime_environment_records(), ())

    async def test_director_stores_exact_context_bindings_without_plaintext_readback(self) -> None:
        self.seed_director()
        director = _human("renamed-director", 9004, "admin")
        read = await self.service_read(director)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertFalse(read.json()["application_required"])
        values = {"username": "synthetic-registry-user", "password": "synthetic-pull-token"}
        for field in read.json()["fields"]:
            value = values.pop(field["binding_key"])
            response = await self.post(
                {
                    "product": "launchplane",
                    "environment": "ghcr_artifact_read",
                    "request_revision": field["request_revision"],
                    "value": value,
                },
                human=director,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(value, response.text)
            record = self.store.read_secret_record(
                next(
                    binding.secret_id
                    for binding in self.store.list_secret_bindings()
                    if binding.binding_key == field["binding_key"]
                )
            )
            version = self.store.read_secret_version(record.current_version_id)
            self.assertNotIn(value, version.model_dump_json())
            self.assertEqual(
                secrets._decrypt_secret_value(version.ciphertext, version.key_id), value
            )
            self.assertEqual(version.created_by, "github:9004")
            self.assertNotIn(
                value,
                json.dumps(
                    [
                        event.model_dump(mode="json")
                        for event in self.store.list_secret_audit_events(secret_id=record.secret_id)
                    ]
                ),
            )
            self.assertEqual(
                secrets.resolve_context_secret_value(
                    integration=GHCR_ARTIFACT_READ_INTEGRATION,
                    context_name="launchplane",
                    binding_key=field["binding_key"],
                    database_url=self.store.database_url,
                ),
                value,
            )
        self.assertFalse(values)
        for binding in self.store.list_secret_bindings():
            self.assertEqual(
                (binding.integration, binding.context, binding.instance),
                (GHCR_ARTIFACT_READ_INTEGRATION, "launchplane", ""),
            )
            self.assertEqual(self.store.read_secret_record(binding.secret_id).scope, "context")
        reread = await self.service_read(director)
        self.assertTrue(all(field["submitted_at"] for field in reread.json()["fields"]))
        self.assertNotIn("synthetic-pull-token", reread.text)
        self.assertEqual(self.store.list_runtime_environment_records(), ())

    async def test_service_secret_rotation_and_policy_revocation_at_commit(self) -> None:
        self.seed_director()
        director = _human("director", 9004, "admin")
        read = await self.service_read(director)
        self.assertEqual(read.status_code, 200, read.text)
        payload = {
            "product": "launchplane",
            "environment": "ghcr_artifact_read",
            "request_revision": read.json()["fields"][1]["request_revision"],
            "value": "synthetic-first-token",
        }
        first = await self.post(payload, human=director)
        self.assertEqual(first.status_code, 200, first.text)
        payload["request_revision"] = first.json()["fields"][1]["request_revision"]
        second = await self.post({**payload, "value": "synthetic-rotated-token"}, human=director)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(
            first.json()["fields"][1]["submission_version_id"],
            second.json()["fields"][1]["submission_version_id"],
        )
        before = self.store.list_secret_records()
        payload["request_revision"] = second.json()["fields"][1]["request_revision"]
        revoked = LaunchplaneAuthzPolicyRecord(
            record_id="revoked",
            source="test",
            updated_at="2026-10-23T00:00:00Z",
            policy=LaunchplaneAuthzPolicy(),
        )
        with patch.object(self.store, "_locked_active_authz_policy_record", return_value=revoked):
            refused = await self.post({**payload, "value": "must-not-be-stored"}, human=director)
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.store.list_secret_records(), before)
        with patch.object(
            self.store,
            "_locked_active_authz_policy_record",
            side_effect=OwnerControlShadowVerifierConflictError("ambiguous policy"),
        ):
            refused = await self.post({**payload, "value": "must-not-be-stored"}, human=director)
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.store.list_secret_records(), before)
        self.assertEqual(
            secrets.resolve_context_secret_value(
                integration=GHCR_ARTIFACT_READ_INTEGRATION,
                context_name="launchplane",
                binding_key="password",
                database_url=self.store.database_url,
            ),
            "synthetic-rotated-token",
        )

    async def test_service_secret_bundle_failure_rolls_back_all_records(self) -> None:
        self.seed_director()
        director = _human("director", 9004, "admin")
        read = await self.service_read(director)
        self.assertEqual(read.status_code, 200, read.text)
        with (
            self.assertRaisesRegex(RuntimeError, "synthetic storage failure"),
            patch.object(
                self.store,
                "_after_product_authority_bundle_step",
                side_effect=RuntimeError("synthetic storage failure"),
            ),
        ):
            await self.post(
                {
                    "product": "launchplane",
                    "environment": "ghcr_artifact_read",
                    "request_revision": read.json()["fields"][0]["request_revision"],
                    "value": "must-not-be-stored",
                },
                human=director,
            )
        self.assertEqual(self.store.list_secret_records(), ())
        self.assertEqual(self.store.list_secret_bindings(), ())

    async def test_service_secret_refuses_stale_tabs_and_changes_during_commit(self) -> None:
        self.seed_director()
        director = _human("director", 9004, "admin")
        write = self.store.write_director_secret_bundle
        for value in ("concurrent-creation", "concurrent-rotation"):
            read = await self.service_read(director)
            self.assertEqual(read.status_code, 200, read.text)
            payload = {
                "product": "launchplane",
                "environment": "ghcr_artifact_read",
                "request_revision": read.json()["fields"][0]["request_revision"],
                "value": "stale-paste",
            }

            def race_then_write(
                bundle: ProductAuthorityBundle, *, identity: GitHubHumanIdentity
            ) -> None:
                secrets.write_secret_value(
                    record_store=self.store,
                    scope="context",
                    integration=GHCR_ARTIFACT_READ_INTEGRATION,
                    name="username",
                    binding_key="username",
                    context_name="launchplane",
                    plaintext_value=value,
                )
                write(bundle, identity=identity)

            with patch.object(
                self.store, "write_director_secret_bundle", side_effect=race_then_write
            ):
                raced = await self.post(payload, human=director)
            self.assertEqual(raced.status_code, 409, raced.text)
            before = self.store.list_secret_records()
            stale = await self.post(payload, human=director)
            self.assertEqual(stale.status_code, 409, stale.text)
            self.assertEqual(self.store.list_secret_records(), before)
            self.assertEqual(
                secrets.resolve_context_secret_value(
                    integration=GHCR_ARTIFACT_READ_INTEGRATION,
                    context_name="launchplane",
                    binding_key="username",
                    database_url=self.store.database_url,
                ),
                value,
            )

    async def test_service_secret_denies_client_scoped_admin_and_login_impersonation(self) -> None:
        self.seed_director()
        for human in (
            _human(),
            _human("operator", 9003, "admin"),
            _human("director", 9005, "admin"),
            _human("director", 9004),
        ):
            response = await self.service_read(human)
            self.assertEqual(response.status_code, 403, response.text)
            response = await self.post(
                {
                    "product": "launchplane",
                    "environment": "ghcr_artifact_read",
                    "request_revision": "a" * 64,
                    "value": "synthetic-pull-token",
                },
                human=human,
            )
            self.assertEqual(response.status_code, 403, response.text)
            self.assertNotIn("synthetic-pull-token", response.text)
        self.assertEqual(self.store.list_secret_records(), ())

    async def test_service_secret_missing_policy_stale_request_and_missing_key_never_write(
        self,
    ) -> None:
        director = _human("director", 9004, "admin")
        self.assertEqual((await self.service_read(director)).status_code, 403)
        self.seed_director()
        read = await self.service_read(director)
        self.assertEqual(read.status_code, 200, read.text)
        payload = {
            "product": "launchplane",
            "environment": "ghcr_artifact_read",
            "request_revision": read.json()["fields"][0]["request_revision"],
            "value": "synthetic-pull-token",
        }
        stale = await self.post({**payload, "request_revision": "a" * 64}, human=director)
        self.assertEqual(stale.status_code, 409, stale.text)
        session = self.sessions.issue(director)
        csrf = await _asgi_request(
            self.app,
            "POST",
            _SUBMIT,
            payload=payload,
            headers={"Cookie": self.sessions.session_cookie_header(session)},
        )
        self.assertEqual(csrf.status_code, 403, csrf.text)
        with patch.dict(os.environ, {}, clear=True):
            unavailable = await self.post(payload, human=director)
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertNotIn("synthetic-pull-token", unavailable.text)
        self.assertEqual(self.store.list_secret_records(), ())

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
        self.assertTrue(before.json()["application_required"])
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
        self.assertEqual(version.value_origin, "supplied")

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
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"product": "other-site"})
        )
        runtime_before = self.store.list_runtime_environment_records()
        secrets_before = self.store.list_secret_records()
        bindings_before = self.store.list_secret_bindings()
        with patch.object(self.store, "write_product_authority_bundle") as writer:
            refused_replay = await self.post(
                {**payload, "mode": "apply", "confirmation": "APPLY example-site/testing"},
                human=operator,
                path=_CONFIG,
                key="apply-mail-submission",
            )
        self.assertEqual(refused_replay.status_code, 403, refused_replay.text)
        self.assertEqual(refused_replay.json()["error"]["code"], "product_config_lane_not_owned")
        writer.assert_not_called()
        self.assertEqual(self.store.list_runtime_environment_records(), runtime_before)
        self.assertEqual(self.store.list_secret_records(), secrets_before)
        self.assertEqual(self.store.list_secret_bindings(), bindings_before)
        self.assertNotIn("mail-secret-value", refused_replay.text)
