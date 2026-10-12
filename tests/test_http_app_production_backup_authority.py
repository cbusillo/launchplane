from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.production_backup_authority import ProductionBackupAuthorityWriteEnvelope
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.support.auth import _StubVerifier, _identity
from tests.support.http import get as http_get
from tests.support.http import request as http_request
from tests.test_production_backup_authority import _dry_run_envelope, _policy
from tests.test_production_backup_migration import _request
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
    _RejectingVerifier,
)


_WORKFLOW_REF = "example/example-product/.github/workflows/promote.yml@refs/heads/main"
_JOB_WORKFLOW_REF = (
    "example/launchplane/.github/workflows/reusable-promote.yml@"
    "0123456789abcdef0123456789abcdef01234567"
)


def _authz_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": [
                {
                    "managed_set_id": "test.production-backup-authority",
                    "managed_rule_id": "example-product.production-backup-authority",
                    "repository": "example/example-product",
                    "repository_id": "1001",
                    "repository_owner_id": "1000",
                    "workflow_refs": [_WORKFLOW_REF],
                    "job_workflow_refs": [_JOB_WORKFLOW_REF],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-product"],
                    "contexts": ["example-product"],
                    "instances": ["prod"],
                    "actions": [
                        "production_backup_authority.read",
                        "production_backup_authority.write",
                    ],
                }
            ],
        }
    )


class ProductionBackupAuthorityHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_cannot_revise_a_target_used_by_an_unauthorized_lane(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            draft = _dry_run_envelope()
            for target in draft.targets:
                store.write_production_backup_target_record(target)
            other = _policy().model_dump(mode="json") | {
                "product": "other-product",
                "context": "other-product",
                "policy_id": "",
                "record_id": "",
                "policy_digest": "",
            }
            store.write_production_backup_policy_record(type(_policy()).model_validate(other))
            revised_targets = tuple(
                type(target).model_validate(
                    target.model_dump(mode="json")
                    | {
                        "target_revision": 2,
                        "record_id": "",
                        "target_digest": "",
                        "supersedes_record_id": target.record_id,
                        "effective_at": "2026-10-01T00:00:00Z",
                        "destination": target.destination.model_dump(mode="json")
                        | {
                            "host": "different-host.example.invalid",
                        },
                    }
                )
                for target in draft.targets
            )
            draft = draft.model_copy(
                update={
                    "targets": revised_targets,
                    "expected_current_target_record_ids": {
                        target.target_id: target.record_id for target in draft.targets
                    },
                }
            )
            sessions = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = sessions.issue(_github_human_identity())
            policy = _github_human_product_config_policy(
                action="production_backup_authority.write",
                product="example-product",
                context="example-product",
                instances=("prod",),
                schema_version=2,
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=sessions,
            )
            response = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=_browser_mutation_headers(sessions, session),
                payload=draft.model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 403, response.text)
            self.assertTrue(
                all(
                    target.target_revision == 1
                    for target in store.list_production_backup_target_records()
                )
            )
            wide_policy = policy.model_copy(
                update={
                    "github_humans": tuple(
                        rule.model_copy(
                            update={
                                "products": ("example-product", "other-product"),
                                "contexts": ("example-product", "other-product"),
                            }
                        )
                        for rule in policy.github_humans
                    )
                }
            )
            wide_app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=wide_policy,
                record_store_factory=lambda: store,
                human_session_manager=sessions,
            )
            allowed = await http_request(
                wide_app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=_browser_mutation_headers(sessions, session),
                payload=draft.model_dump(mode="json"),
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)
            # Simulate a foreign policy becoming visible only inside the locked write.
            with patch.object(store, "list_production_backup_policy_records", return_value=()):
                locked = await http_request(
                    app,
                    "POST",
                    "/v1/production-backup-authority/apply",
                    headers=_browser_mutation_headers(sessions, session)
                    | {"Idempotency-Key": "locked-shared-target"},
                    payload=draft.model_dump(mode="json")
                    | {
                        "mode": "apply",
                        "reviewed_authority_digest": allowed.json()["result"]["authority_digest"],
                    },
                )
            self.assertEqual(locked.status_code, 403, locked.text)
            self.assertTrue(
                all(
                    target.target_revision == 1
                    for target in store.list_production_backup_target_records()
                )
            )

    async def test_browser_approval_preserves_csrf_scope_digest_and_replay(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            )
            store.ensure_schema()
            self.addCleanup(store.close)
            sessions = HumanSessionManager(
                config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
            )
            session = sessions.issue(_github_human_identity())
            policy = _github_human_product_config_policy(
                actions=("production_backup_authority.read", "production_backup_authority.write"),
                product="example-product",
                context="example-product",
                instances=("prod",),
                schema_version=2,
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=policy,
                record_store_factory=lambda: store,
                human_session_manager=sessions,
            )
            route = "/v1/production-backup-authority/apply"
            draft = _dry_run_envelope().model_dump(mode="json")
            refused = await http_request(
                app,
                "POST",
                route,
                headers={"Cookie": sessions.session_cookie_header(session)},
                payload=draft,
            )
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["error"]["code"], "browser_mutation_denied")
            other_lane = draft | {
                "policy": draft["policy"]
                | {
                    "context": "other-context",
                    "policy_id": "",
                    "record_id": "",
                    "policy_digest": "",
                }
            }
            refused = await http_request(
                app,
                "POST",
                route,
                headers=_browser_mutation_headers(sessions, session),
                payload=other_lane,
            )
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["error"]["code"], "authorization_denied")
            dry = await http_request(
                app,
                "POST",
                route,
                headers=_browser_mutation_headers(sessions, session),
                payload=draft,
            )
            self.assertEqual(dry.status_code, 200, dry.text)
            self.assertEqual(store.list_production_backup_policy_records(), ())
            self.assertEqual(store.list_production_backup_target_records(), ())
            digest = dry.json()["result"]["authority_digest"]
            request = draft | {"mode": "apply", "reviewed_authority_digest": digest}
            changed_policy = draft["policy"] | {
                "reason": "Changed after review",
                "policy_digest": "",
            }
            refused = await http_request(
                app,
                "POST",
                route,
                headers=_browser_mutation_headers(sessions, session)
                | {"Idempotency-Key": "changed"},
                payload=request | {"policy": changed_policy},
            )
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(store.list_production_backup_policy_records(), ())
            for replayed in (False, True):
                result = await http_request(
                    app,
                    "POST",
                    route,
                    headers=_browser_mutation_headers(sessions, session)
                    | {"Idempotency-Key": "browser-backup-approval"},
                    payload=request,
                )
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(bool(result.json().get("replayed")), replayed)
            self.assertEqual(len(store.list_production_backup_policy_records()), 1)
            self.assertEqual(
                len(store.list_production_backup_target_records()), len(draft["targets"])
            )
            revoked_app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                human_session_manager=sessions,
            )
            revoked_retry = await http_request(
                revoked_app,
                "POST",
                route,
                headers=_browser_mutation_headers(sessions, session)
                | {"Idempotency-Key": "browser-backup-approval"},
                payload=request,
            )
            self.assertEqual(revoked_retry.status_code, 403)
            self.assertEqual(len(store.list_production_backup_policy_records()), 1)

    async def test_read_dry_run_apply_replay_and_redaction(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            identity = _identity(
                repository="example/example-product",
                workflow_ref=_WORKFLOW_REF,
                job_workflow_ref=_JOB_WORKFLOW_REF,
                event_name="workflow_dispatch",
                environment="prod",
                repository_id="1001",
                repository_owner_id="1000",
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_authz_policy(),
                record_store_factory=lambda: store,
            )
            headers = {"Authorization": "Bearer valid-token"}
            query = urlencode(
                {
                    "product": "example-product",
                    "context": "example-product",
                    "instance": "prod",
                    "promotion_action": "verireel_prod_promotion.execute",
                }
            )
            missing = await http_get(
                app,
                f"/v1/production-backup-authority?{query}",
                headers=headers,
            )
            self.assertEqual(missing.status_code, 200)
            self.assertEqual(missing.json()["authority"]["state"], "missing")

            dry_envelope = _dry_run_envelope()
            dry_response = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=headers,
                payload=dry_envelope.model_dump(mode="json"),
            )
            self.assertEqual(dry_response.status_code, 200)
            dry_payload = dry_response.json()
            self.assertEqual(dry_payload["result"]["status"], "would_apply")
            self.assertNotIn("proxmox.example.invalid", dry_response.text)
            self.assertNotIn("pbs-production", dry_response.text)

            apply_envelope = ProductionBackupAuthorityWriteEnvelope.model_validate(
                dry_envelope.model_dump(mode="json")
                | {
                    "mode": "apply",
                    "reviewed_authority_digest": dry_payload["result"]["authority_digest"],
                }
            )
            apply_headers = headers | {"Idempotency-Key": "issue-2306-example-apply"}
            applied = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=apply_headers,
                payload=apply_envelope.model_dump(mode="json"),
            )
            self.assertEqual(applied.status_code, 200)
            self.assertEqual(applied.json()["result"]["status"], "applied")
            replayed = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=apply_headers,
                payload=apply_envelope.model_dump(mode="json"),
            )
            self.assertEqual(replayed.status_code, 200)
            self.assertTrue(replayed.json()["replayed"])

            unchanged = dry_envelope.model_copy(
                update={
                    "expected_current_policy_record_id": dry_envelope.policy.record_id,
                    "expected_current_target_record_ids": {
                        target.target_id: target.record_id for target in dry_envelope.targets
                    },
                }
            )
            unchanged_review = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=headers,
                payload=unchanged.model_dump(mode="json"),
            )
            self.assertEqual(unchanged_review.status_code, 200, unchanged_review.text)
            unchanged_apply = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=headers | {"Idempotency-Key": "new-reviewed-no-op"},
                payload=unchanged.model_dump(mode="json")
                | {
                    "mode": "apply",
                    "reviewed_authority_digest": unchanged_review.json()["result"][
                        "authority_digest"
                    ],
                },
            )
            self.assertEqual(unchanged_apply.status_code, 200, unchanged_apply.text)
            self.assertEqual(unchanged_apply.json()["result"]["status"], "replayed")

            ready = await http_get(
                app,
                f"/v1/production-backup-authority?{query}",
                headers=headers,
            )
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["authority"]["state"], "ready")
            self.assertNotIn("proxmox.example.invalid", ready.text)
            self.assertNotIn("pbs-production", ready.text)
            store.close()

    async def test_authority_negative_paths_and_legacy_migration_refusal(self) -> None:
        identity = _identity(
            repository="example/example-product",
            workflow_ref=_WORKFLOW_REF,
            job_workflow_ref=_JOB_WORKFLOW_REF,
            event_name="workflow_dispatch",
            environment="prod",
            repository_owner_id="1000",
        )
        payload = _dry_run_envelope().model_dump(mode="json") | {
            "mode": "apply",
            "reviewed_authority_digest": "a" * 64,
        }
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            for authorized, key, status, code in (
                (False, "key", 403, "authorization_denied"),
                (True, "", 400, "missing_idempotency_key"),
                (True, "key", 503, "database_storage_required"),
            ):
                with self.subTest(code=code):
                    app = create_launchplane_fastapi_app(
                        verifier=_StubVerifier(identity),
                        authz_policy=_authz_policy() if authorized else LaunchplaneAuthzPolicy(),
                        record_store_factory=lambda: store,
                    )
                    response = await http_request(
                        app,
                        "POST",
                        "/v1/production-backup-authority/apply",
                        headers={"Authorization": "Bearer valid-token", "Idempotency-Key": key},
                        payload=payload,
                    )
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.json()["error"]["code"], code)
                    self.assertEqual(store.list_production_backup_policy_records(), ())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(identity),
                authz_policy=_authz_policy(),
                record_store_factory=lambda: store,
            )
            migration = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/legacy-runtime-migration",
                headers={"Authorization": "Bearer valid-token"},
                payload=_request() | {"context": "EXAMPLE-PRODUCT", "instance": "PROD"},
            )
            self.assertEqual(migration.status_code, 409, migration.text)
            self.assertEqual(migration.json()["error"]["code"], "legacy_backup_migration_conflict")
            self.assertIn("/v1/production-backup-authority/apply", migration.text)
            self.assertEqual(store.list_production_backup_policy_records(), ())
            self.assertEqual(store.list_production_backup_target_records(), ())

    async def test_local_operator_revises_only_its_own_policy_targets(self) -> None:
        policy = _authz_policy().model_dump(mode="json")
        policy["github_actions"][0]["products"] = ["example-product", "other-product"]
        policy["github_actions"][0]["contexts"] = ["example-product", "other-product"]
        policy["local_operators"] = [
            {
                "managed_set_id": "operator.agent-product-setup",
                "managed_rule_id": "example-product.prod-backup-policy",
                "subjects": ["operator-agent"],
                "token_labels": ["operator-agent-token"],
                "products": ["example-product"],
                "contexts": ["example-product"],
                "instances": ["prod"],
                "actions": ["production_backup_authority.write"],
            }
        ]
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    _identity(
                        repository="example/example-product",
                        workflow_ref=_WORKFLOW_REF,
                        job_workflow_ref=_JOB_WORKFLOW_REF,
                        event_name="workflow_dispatch",
                        environment="prod",
                        repository_id="1001",
                        repository_owner_id="1000",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(policy),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="local-operator-token",
                    local_operator_subject="operator-agent",
                    local_operator_token_label="operator-agent-token",
                ),
            )
            operator = {"Authorization": "Bearer local-operator-token"}
            workflow = {"Authorization": "Bearer valid-token"}
            own = _dry_run_envelope().model_dump(mode="json")

            allowed = await http_request(
                app, "POST", "/v1/production-backup-authority/apply", headers=operator, payload=own
            )
            self.assertEqual(allowed.status_code, 200, allowed.text)

            extra = _dry_run_envelope().model_dump(mode="json")
            foreign = dict(extra["targets"][0])
            foreign.update({"target_id": "foreign-target", "record_id": ""})
            foreign.pop("target_digest", None)
            extra["targets"] = [*extra["targets"], foreign]
            refused = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=operator,
                payload=extra,
            )
            self.assertEqual(refused.status_code, 403, refused.text)
            self.assertEqual(refused.json()["error"]["code"], "local_operator_lane_scope_required")

            other = _dry_run_envelope().model_dump(mode="json")
            other["policy"].update(
                {
                    "product": "other-product",
                    "context": "other-product",
                    "record_id": "",
                    "policy_id": "",
                    "policy_digest": "",
                }
            )
            reviewed = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=workflow,
                payload=other,
            )
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
            applied = await http_request(
                app,
                "POST",
                "/v1/production-backup-authority/apply",
                headers=workflow | {"Idempotency-Key": "other-product-policy"},
                payload=other
                | {
                    "mode": "apply",
                    "reviewed_authority_digest": reviewed.json()["result"]["authority_digest"],
                },
            )
            self.assertEqual(applied.status_code, 200, applied.text)

            shared = await http_request(
                app, "POST", "/v1/production-backup-authority/apply", headers=operator, payload=own
            )
            self.assertEqual(shared.status_code, 403, shared.text)
            self.assertEqual(shared.json()["error"]["code"], "local_operator_lane_scope_required")
            store.close()

    async def test_openapi_exposes_bounded_authority_routes(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(
                    _identity(
                        repository="example/example-product",
                        workflow_ref=_WORKFLOW_REF,
                        job_workflow_ref=_JOB_WORKFLOW_REF,
                        event_name="workflow_dispatch",
                        environment="prod",
                        repository_id="1001",
                        repository_owner_id="1000",
                    )
                ),
                authz_policy=_authz_policy(),
                record_store_factory=lambda: store,
            )
            paths = app.openapi()["paths"]
            self.assertIn("/v1/production-backup-authority", paths)
            self.assertIn("/v1/production-backup-authority/apply", paths)
            self.assertIn(
                "/v1/production-backup-authority/legacy-runtime-migration",
                paths,
            )
            store.close()


if __name__ == "__main__":
    unittest.main()
