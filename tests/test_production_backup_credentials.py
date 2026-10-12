from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
import subprocess
from typing import Any
from fastapi import FastAPI
from unittest.mock import patch

from control_plane.contracts.secret_record import SecretRecord, SecretBinding, SecretVersion
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.production_backup_credentials import BACKUP_CREDENTIAL_KEYS
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _AsgiResponse,
    _RejectingVerifier,
    _asgi_request,
    _browser_mutation_headers,
    _github_human_identity,
    _github_human_product_config_policy,
    _github_oauth_config,
)
from tests.support.stores import sqlite_database_url
from tests.test_production_backup_authority import (
    _profile,
    _policy,
    _source_target,
    _destination_target,
)
from tests.test_production_backup_provider import BackupHost, setup_memory_files


class BackupCredentialHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "db")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_product_profile_record(_profile())
        self.store.write_production_backup_target_record(_source_target())
        self.store.write_production_backup_target_record(_destination_target())
        self.store.write_production_backup_policy_record(_policy())
        self.originals = []
        self.selection = {}
        for index, key in enumerate(BACKUP_CREDENTIAL_KEYS):
            record = SecretRecord(
                secret_id=f"credential-{index}",
                scope="context_instance",
                integration="launchplane_worker",
                name=f"OLD_{key}",
                context="example-product",
                instance="prod",
                current_version_id=f"version-{index}",
                created_at="2026-10-01T00:00:00Z",
                updated_at="2026-10-01T00:00:00Z",
            )
            binding = SecretBinding(
                binding_id=f"binding-{index}",
                secret_id=record.secret_id,
                integration=record.integration,
                binding_key=record.name,
                context=record.context,
                instance=record.instance,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            self.store.write_secret_record(record)
            self.store.write_secret_binding(binding)
            self.store.write_secret_version(
                SecretVersion(
                    secret_id=record.secret_id,
                    version_id=record.current_version_id,
                    created_at=record.created_at,
                    ciphertext="synthetic-encrypted-payload",
                )
            )
            self.originals.append(record)
            self.selection[key] = binding.binding_id
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.sessions.issue(_github_human_identity())
        self.app = self.make_app(
            (
                "product_config.plan",
                "product_config.apply",
                "product_environment.read",
                "production_backup_authority.read",
            )
        )
        self.route = "/v1/products/example-product/environments/prod/backup-worker-bindings/apply"
        setup_memory_files(self)

    def make_app(self, actions: tuple[str, ...]) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                actions=actions,
                product="example-product",
                context="example-product",
                instances=("prod",),
                schema_version=2,
            ),
            human_session_manager=self.sessions,
            record_store_factory=lambda: self.store,
        )

    async def post(
        self, payload: dict[str, object], key: str = "", app: FastAPI | None = None
    ) -> _AsgiResponse:
        return await _asgi_request(
            app or self.app,
            "POST",
            self.route,
            headers={
                **_browser_mutation_headers(self.sessions, self.session),
                "Idempotency-Key": key,
            },
            payload=payload,
        )

    async def reviewed(self) -> dict[str, object]:
        body: dict[str, object] = {
            "bindings": self.selection,
            "reason": "Reuse the stored exact-lane backup credentials",
        }
        dry = await self.post(body)
        self.assertEqual(dry.status_code, 202, dry.text)
        return {
            **body,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }

    async def test_reference_rebind_preserves_versions_is_atomic_and_replays(self) -> None:
        with patch(
            "control_plane.secrets._decrypt_secret_value", side_effect=AssertionError("No decrypt")
        ):
            body = await self.reviewed()
            self.assertEqual(
                {
                    b.binding_key
                    for b in self.store.list_secret_bindings(integration="launchplane_worker")
                },
                {record.name for record in self.originals},
            )
            self.assertEqual((await self.post(body)).status_code, 400)
            applied = await self.post(body, "rebind")
            self.assertEqual(applied.status_code, 202, applied.text)
            replay = await self.post(body, "rebind")
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(
                (await self.post({**body, "reason": "Other reason"}, "rebind")).status_code, 409
            )
        for record in self.originals:
            self.assertEqual(self.store.read_secret_record(record.secret_id), record)
            self.assertEqual(len(self.store.list_secret_versions(secret_id=record.secret_id)), 1)
        self.assertEqual(
            set(
                b.binding_key
                for b in self.store.list_secret_bindings(integration="launchplane_worker")
            ),
            set(BACKUP_CREDENTIAL_KEYS),
        )

    async def test_disabled_cross_lane_and_destination_collision_refuse(self) -> None:
        for change in [
            {"instance": "testing"},
            {"integration": "runtime_environment"},
            {"status": "disabled"},
            {"scope": "global", "context": "", "instance": ""},
        ]:
            with self.subTest(change=change):
                record = self.originals[0]
                self.store.write_secret_record(record.model_copy(update=change))
                response = await self.post(
                    {"bindings": self.selection, "reason": "Review references"}
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.store.write_secret_record(record)
        other = SecretBinding(
            binding_id="collision",
            secret_id=self.originals[0].secret_id,
            integration="launchplane_worker",
            binding_key=BACKUP_CREDENTIAL_KEYS[0],
            context="example-product",
            instance="prod",
            created_at=self.originals[0].created_at,
            updated_at=self.originals[0].updated_at,
        )
        self.store.write_secret_binding(other)
        self.assertEqual(
            (
                await self.post({"bindings": self.selection, "reason": "Review references"})
            ).status_code,
            409,
        )

    async def test_rotation_after_review_and_commit_race_are_refused(self) -> None:
        body = await self.reviewed()
        record = self.originals[0]
        self.store.write_secret_record(record.model_copy(update={"current_version_id": "rotated"}))
        self.assertEqual((await self.post(body, "rotation")).status_code, 409)
        self.store.write_secret_record(record)
        original = self.store.compare_and_rebind_worker_credentials

        def race(**kwargs: Any) -> Any:
            self.store.write_secret_record(record.model_copy(update={"status": "disabled"}))
            return original(**kwargs)

        with patch.object(self.store, "compare_and_rebind_worker_credentials", side_effect=race):
            self.assertEqual((await self.post(body, "race")).status_code, 409)
        self.assertEqual(
            set(
                b.binding_key
                for b in self.store.list_secret_bindings(integration="launchplane_worker")
            ),
            set(r.name for r in self.originals),
        )

    async def test_failure_between_bindings_rolls_back_all_and_receipt(self) -> None:
        body = await self.reviewed()
        with patch.object(
            self.store, "_after_product_authority_bundle_step", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                await self.post(body, "rollback")
        self.assertEqual(
            set(
                b.binding_key
                for b in self.store.list_secret_bindings(integration="launchplane_worker")
            ),
            set(r.name for r in self.originals),
        )
        self.assertEqual((await self.post(body, "rollback")).status_code, 202)

    async def test_read_only_probe_has_only_boundary_and_storage_commands(self) -> None:
        body = await self.reviewed()
        self.assertEqual((await self.post(body, "ready")).status_code, 202)
        host = BackupHost()
        with (
            patch("control_plane.secrets._decrypt_secret_value", return_value="fixture-value"),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            ),
        ):
            response = await _asgi_request(
                self.app,
                "GET",
                "/v1/production-backup-authority/preflight?product=example-product&context=example-product&instance=prod&promotion_action=generic_web_prod_promotion.execute",
                headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result["capture_started"])
        self.assertEqual(
            host.commands,
            [
                ["launchplane-backup-boundary"],
                ["pvesm", "status", "--storage", host.storage],
            ],
        )
        for private in [
            "fixture-value",
            "proxmox.example.invalid",
            "pbs-production",
            "capture_status",
        ]:
            self.assertNotIn(private, response.text)

    async def test_denial_and_csrf_never_change_credentials(self) -> None:
        body = await self.reviewed()
        denied = await self.post(body, "denied", self.make_app(("product_config.plan",)))
        self.assertEqual(denied.status_code, 403)
        response = await _asgi_request(
            self.app,
            "POST",
            self.route,
            headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            payload=body,
        )
        self.assertEqual(response.status_code, 403)

    async def test_other_product_claiming_context_during_commit_refuses(self) -> None:
        body = await self.reviewed()
        original = self.store.compare_and_rebind_worker_credentials

        def race(**kwargs: Any) -> Any:
            self.store.write_product_profile_record(
                _profile().model_copy(update={"product": "other-product"})
            )
            return original(**kwargs)

        with patch.object(self.store, "compare_and_rebind_worker_credentials", side_effect=race):
            response = await self.post(body, "context-race")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            {
                binding.binding_key
                for binding in self.store.list_secret_bindings(integration="launchplane_worker")
            },
            {record.name for record in self.originals},
        )

    async def test_read_only_probe_refuses_restore_access_and_inactive_storage(self) -> None:
        body = await self.reviewed()
        self.assertEqual((await self.post(body, "ready")).status_code, 202)
        for problem in ["restore", "storage"]:
            with self.subTest(problem=problem):
                host = BackupHost()

                def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                    result = host.run(command, **kwargs)
                    if problem == "restore" and host.commands[-1] == [
                        "launchplane-backup-boundary"
                    ]:
                        boundary = json.loads(result.stdout)
                        boundary["restore_allowed"] = True
                        return subprocess.CompletedProcess(
                            command, 0, stdout=json.dumps(boundary), stderr=""
                        )
                    if problem == "storage" and host.commands[-1][0] == "pvesm":
                        return subprocess.CompletedProcess(
                            command, 0, stdout=f"{host.storage} pbs inactive 0 0 0", stderr=""
                        )
                    return result

                with (
                    patch(
                        "control_plane.secrets._decrypt_secret_value", return_value="fixture-value"
                    ),
                    patch(
                        "control_plane.workflows.production_backup_provider.subprocess.run",
                        side_effect=run,
                    ),
                ):
                    response = await _asgi_request(
                        self.app,
                        "GET",
                        "/v1/production-backup-authority/preflight?product=example-product&context=example-product&instance=prod&promotion_action=generic_web_prod_promotion.execute",
                        headers={"Cookie": self.sessions.session_cookie_header(self.session)},
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["result"]["status"], "fail")
                self.assertFalse(response.json()["result"]["capture_started"])
                self.assertTrue(
                    all(
                        command[0] in {"launchplane-backup-boundary", "pvesm"}
                        for command in host.commands
                    )
                )

    async def test_completed_original_replays_after_source_invalidates_during_retry(self) -> None:
        body = await self.reviewed()
        applied = await self.post(body, "completed")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.store.write_secret_record(self.originals[0].model_copy(update={"status": "disabled"}))
        prepare = self.store.prepare_db_only_mutation
        first = True

        def earlier_preflight(**kwargs: Any) -> Any:
            nonlocal first
            result = prepare(**kwargs)
            if first:
                first = False
                return result._replace(status="missing", record=None)
            return result

        with patch.object(self.store, "prepare_db_only_mutation", side_effect=earlier_preflight):
            replay = await self.post(body, "completed")
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
