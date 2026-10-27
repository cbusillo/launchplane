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
from control_plane.guest_capture_credentials import CAPTURE_KEYS as BACKUP_CREDENTIAL_KEYS
from control_plane.guest_capture_credentials import (
    capture_boundary,
    validate_guest_capture_enrollment,
)
from control_plane.contracts.backup_policy_proposal import BackupPolicyProposalInput
from control_plane.contracts.privileged_operation import PrivilegedOperationLocalOperatorActor
from control_plane.privileged_operation_service import create_typed_privileged_operation_plan
from control_plane.production_backup_authority import ProductionBackupAuthorityWriteEnvelope
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
from tests.support.auth import scoped_agent_test
from control_plane.storage.guest_capture_bindings import GUEST_CAPTURE_BINDING_WRITE_STEP
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
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

    def make_app(self, actions: tuple[str, ...], product: str = "example-product") -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_github_human_product_config_policy(
                actions=actions,
                product=product,
                context=product,
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

    async def test_generate_capture_key_stays_in_custody_and_replays_public_output(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from control_plane import secrets

        host_key = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        request: dict[str, object] = {
            "generate_capture_key": True,
            "known_hosts": f"{_source_target().destination.host} {host_key}\n",
            "promotion_action": _policy().promotion_action,
            "reason": "Enroll one approved guest capture credential",
        }
        with patch.dict("os.environ", {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "fixture"}):
            before = self.store.list_secret_records()
            dry = await self.post(request)
            self.assertEqual(dry.status_code, 202, dry.text)
            self.assertEqual(self.store.list_secret_records(), before)
            self.assertNotIn("public_key", dry.json()["result"])
            request.update(mode="apply", reviewed_plan_sha256=dry.json()["result"]["plan_sha256"])
            applied = await self.post(request, "generate-once")
            self.assertEqual(applied.status_code, 202, applied.text)
            result = applied.json()["result"]
            records = self.store.list_secret_records()
            replay = await self.post(request, "generate-once")
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(replay.json()["result"], result)
            self.assertEqual(self.store.list_secret_records(), records)
            private = next(record for record in records if record.name == BACKUP_CREDENTIAL_KEYS[0])
            value = secrets._decrypt_secret_value(
                self.store.read_secret_version(private.current_version_id).ciphertext,
                self.store.read_secret_version(private.current_version_id).key_id,
            )
            parsed = serialization.load_ssh_private_key(value.encode(), password=None)
            public = (
                parsed.public_key()
                .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
                .decode()
            )
            self.assertEqual(public, result["public_key"])
            self.assertNotIn(value, applied.text + replay.text)
            self.assertEqual(
                (private.integration, private.context, private.instance),
                ("launchplane_worker", "example-product", "prod"),
            )
            self.assertIsNotNone(
                self.store.read_guest_capture_binding(
                    capture_boundary(_policy(), _source_target(), _destination_target())
                )
            )
            readback = await _asgi_request(
                self.app,
                method="GET",
                path=self.route.removesuffix("/apply"),
                headers=_browser_mutation_headers(self.sessions, self.session),
            )
            self.assertEqual(
                readback.json()["result"]["generated_capture_keys"][0]["public_key"], public
            )
            self.assertEqual((await self.post(request, "different-operation")).status_code, 409)
            self.assertEqual(self.store.list_secret_records(), records)

    async def test_capture_key_generation_refuses_changed_authority_and_untrusted_pin(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        host_key = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        body: dict[str, object] = {
            "generate_capture_key": True,
            "known_hosts": f"{_source_target().destination.host} {host_key}",
            "promotion_action": _policy().promotion_action,
            "reason": "Approved capture enrollment",
        }
        before = self.store.list_secret_records()
        for known_hosts in (
            f"wrong.example.invalid {host_key}",
            "* " + host_key,
            str(body["known_hosts"]) + "\nother.example.invalid " + host_key,
        ):
            response = await self.post({**body, "known_hosts": known_hosts})
            self.assertEqual(response.status_code, 409, response.text)
        self.app = self.make_app(("product_environment.read",))
        self.assertEqual((await self.post(body)).status_code, 403)
        self.app = self.make_app(("product_config.plan", "product_config.apply"))
        review = await self.post(body)
        self.assertEqual(review.status_code, 202, review.text)
        body.update(mode="apply", reviewed_plan_sha256=review.json()["result"]["plan_sha256"])
        current = _policy()
        self.store.write_production_backup_policy_record(
            _policy(revision=2, supersedes_record_id=current.record_id)
        )
        with patch(
            "control_plane.production_backup_key_generation.Ed25519PrivateKey.generate"
        ) as generate:
            self.assertEqual((await self.post(body, "changed-authority")).status_code, 409)
        generate.assert_not_called()
        self.assertEqual(self.store.list_secret_records(), before)

    async def test_capture_key_generation_rolls_back_secrets_pin_and_receipt_together(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        host_key = (
            Ed25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        body: dict[str, object] = {
            "generate_capture_key": True,
            "known_hosts": f"{_source_target().destination.host} {host_key}",
            "promotion_action": _policy().promotion_action,
            "reason": "Approved capture enrollment",
        }
        review = await self.post(body)
        self.assertEqual(review.status_code, 202, review.text)
        body.update(mode="apply", reviewed_plan_sha256=review.json()["result"]["plan_sha256"])
        before = self.store.list_secret_records()

        def fail_after_pin(step: str) -> None:
            if step == GUEST_CAPTURE_BINDING_WRITE_STEP:
                raise RuntimeError("injected")

        with patch.dict("os.environ", {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "fixture"}):
            with patch.object(
                self.store, "_after_product_authority_bundle_step", side_effect=fail_after_pin
            ):
                with self.assertRaises(RuntimeError):
                    await self.post(body, "atomic-generation")
            self.assertEqual(self.store.list_secret_records(), before)
            self.assertFalse(self.store.guest_capture_secret_ids())
            self.assertEqual((await self.post(body, "atomic-generation")).status_code, 202)
        for record in self.originals:
            self.assertEqual(self.store.read_secret_record(record.secret_id), record)

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
            host.source.host,
            host.storage,
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

    async def test_other_product_preflight_uses_guest_credentials_without_copying(self) -> None:
        self.assertEqual((await self.post(await self.reviewed(), "adopt")).status_code, 202)
        payload = _profile().model_dump(mode="json")
        payload["product"] = "other"
        payload["lanes"][0]["context"] = "other"
        self.store.write_product_profile_record(type(_profile()).model_validate(payload))
        payload = _policy().model_dump(mode="json")
        payload.update(
            product="other", context="other", record_id="", policy_id="", policy_digest=""
        )
        self.store.write_production_backup_policy_record(type(_policy()).model_validate(payload))
        host = BackupHost()
        with (
            patch("control_plane.secrets._decrypt_secret_value", return_value="fixture"),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            ),
        ):
            response = await _asgi_request(
                self.make_app(("production_backup_authority.read",), product="other"),
                "GET",
                "/v1/production-backup-authority/preflight?product=other&context=other&instance=prod&promotion_action=generic_web_prod_promotion.execute",
                headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["status"], "pass")
        self.assertFalse(response.json()["result"]["capture_started"])
        self.assertEqual(len(self.store.list_secret_records()), len(self.originals))
        self.assertFalse(
            any(
                command[0] == "vzdump"
                or len(command) > 1
                and command[1] in {"snapshot", "delsnapshot"}
                for command in host.commands
            )
        )

    async def test_saved_pending_proposal_preflight_does_not_apply_policy(self) -> None:
        self.assertEqual((await self.post(await self.reviewed(), "adopt")).status_code, 202)
        current = _policy()
        proposed = _policy(revision=2, supersedes_record_id=current.record_id)
        proposal = create_typed_privileged_operation_plan(
            record_store=self.store,
            descriptor_id="production-backup-policy",
            actor=PrivilegedOperationLocalOperatorActor(principal_sha256="a" * 64),
            source_kind="agent_api",
            source_event_id="preflight-proposal",
            request=BackupPolicyProposalInput(
                authority=ProductionBackupAuthorityWriteEnvelope(
                    mode="dry_run",
                    policy=proposed,
                    expected_current_policy_record_id=current.record_id,
                ),
                reason="Qualify without capture",
            ),
            expires_in_seconds=3600,
        ).record
        host = BackupHost()
        with (
            patch("control_plane.secrets._decrypt_secret_value", return_value="fixture"),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            ),
        ):
            response = await _asgi_request(
                self.app,
                "GET",
                f"/v1/production-backup-authority/preflight?product=example-product&context=example-product&instance=prod&promotion_action={current.promotion_action}&proposal_id={proposal.operation_id}",
                headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["policy_record_id"], proposed.record_id)
        self.assertEqual(response.json()["result"]["status"], "pass")
        self.assertEqual(
            self.store.list_production_backup_policy_records(status="active"), (current,)
        )
        self.assertEqual(
            self.store.read_privileged_operation_record(proposal.operation_id).status, "planned"
        )
        self.assertFalse(
            any(
                command[0] == "vzdump"
                or len(command) > 1
                and command[1] in {"snapshot", "delsnapshot"}
                for command in host.commands
            )
        )

    @scoped_agent_test
    async def test_scoped_caller_can_qualify_active_policy_but_cannot_probe_pending_proposal(
        self,
    ) -> None:
        self.assertEqual((await self.post(await self.reviewed(), "bind")).status_code, 202)
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "local_operators": [
                    {
                        "subjects": ["scoped"],
                        "token_labels": ["scoped"],
                        "products": ["example-product"],
                        "contexts": ["example-product"],
                        "instances": ["prod"],
                        "actions": ["production_backup_authority.read"],
                    }
                ],
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="scoped-fixture",
                local_operator_subject="scoped",
                local_operator_token_label="scoped",
            ),
        )
        query = "/v1/production-backup-authority/preflight?product=example-product&context=example-product&instance=prod&promotion_action=generic_web_prod_promotion.execute"
        host = BackupHost()
        with (
            patch("control_plane.secrets._decrypt_secret_value", return_value="fixture"),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            ) as provider,
        ):
            active = await _asgi_request(
                app, "GET", query, headers={"Authorization": "Bearer scoped-fixture"}
            )
            self.assertEqual(active.status_code, 200, active.text)
            self.assertEqual(active.json()["result"]["status"], "pass")
            provider.reset_mock()
            denied = await _asgi_request(
                app,
                "GET",
                query + "&proposal_id=pending-fixture",
                headers={"Authorization": "Bearer scoped-fixture"},
            )
        self.assertEqual(denied.status_code, 403, denied.text)
        provider.assert_not_called()

    async def test_canonical_pair_needs_pin_apply_and_replays_without_relabelling(self) -> None:
        for binding in self.store.list_secret_bindings():
            key = next(
                key
                for key, identifier in self.selection.items()
                if identifier == binding.binding_id
            )
            self.store.write_secret_binding(binding.model_copy(update={"binding_key": key}))
        dry = await self.post(
            {"bindings": self.selection, "reason": "Bind the existing guest pair"}
        )
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertTrue(dry.json()["result"]["changed"])
        body = {
            "mode": "apply",
            "bindings": self.selection,
            "reason": "Bind the existing guest pair",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        self.assertEqual((await self.post(body, "canonical-pin")).status_code, 202)
        self.assertTrue((await self.post(body, "canonical-pin")).json()["replayed"])
        after = await self.post({"bindings": self.selection, "reason": "Already guest-bound"})
        self.assertEqual(after.status_code, 202, after.text)
        self.assertFalse(after.json()["result"]["changed"])
        self.assertEqual(
            self.store.guest_capture_secret_ids(),
            frozenset(record.secret_id for record in self.originals),
        )

    def revise_capture_endpoint(self, host: str) -> None:
        for current in self.store.list_production_backup_target_records(status="active"):
            if current.target_id not in _policy().target_ids:
                continue
            payload = current.model_dump(mode="json")
            payload.update(
                record_id="",
                target_digest="",
                target_revision=current.target_revision + 1,
                supersedes_record_id=current.record_id,
            )
            payload["destination"].update(host=host, username="capture")
            self.store.write_production_backup_target_record(type(current).model_validate(payload))

    async def test_reviewed_endpoint_transition_reuses_the_pair_without_changing_secret_data(
        self,
    ) -> None:
        self.assertEqual((await self.post(await self.reviewed(), "original-pin")).status_code, 202)
        old_boundary = capture_boundary(_policy(), _source_target(), _destination_target())
        old_pin = self.store.read_guest_capture_binding(old_boundary)
        assert old_pin is not None
        records = self.store.list_secret_records()
        versions = tuple(
            self.store.list_secret_versions(secret_id=record.secret_id) for record in records
        )
        self.revise_capture_endpoint("new-endpoint.invalid")
        with patch.object(
            self.store, "read_secret_version", side_effect=AssertionError("No key reads")
        ):
            self.store.seed_guest_capture_bindings()
            state = self.store.guest_capture_enrollment_state("example-product", "prod")
            body = await self.reviewed()
            result = await self.post(body, "endpoint-transition")
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(self.store.read_guest_capture_binding(old_boundary), old_pin)
        pin = self.store.read_guest_capture_binding(
            old_boundary.model_copy(update={"host": "new-endpoint.invalid", "username": "capture"})
        )
        assert pin is not None
        self.assertEqual(
            pin.model_dump(exclude={"boundary"}), old_pin.model_dump(exclude={"boundary"})
        )
        self.assertEqual(self.store.list_secret_records(), records)
        self.assertEqual(
            tuple(
                self.store.list_secret_versions(secret_id=record.secret_id) for record in records
            ),
            versions,
        )
        host = BackupHost()
        with (
            patch("control_plane.secrets._decrypt_secret_value", return_value="fixture"),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            ),
        ):
            response = await _asgi_request(
                self.app,
                "GET",
                "/v1/production-backup-authority/preflight?product=example-product&context=example-product&instance=prod&promotion_action="
                + _policy().promotion_action,
                headers={"Cookie": self.sessions.session_cookie_header(self.session)},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["status"], "pass")
        self.assertFalse(response.json()["result"]["capture_started"])
        for change in (
            {"guest_id": "999"},
            {"storage_id": "other"},
            {"snapshot_prefix": "other"},
        ):
            candidate = old_pin.model_copy(
                update={"boundary": old_boundary.model_copy(update=change)}
            )
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_guest_capture_enrollment(candidate, state)

    async def test_failed_pin_rolls_back_rebinding_and_its_receipt(self) -> None:
        body = await self.reviewed()

        def fail_after_pin(step: str) -> None:
            if step == GUEST_CAPTURE_BINDING_WRITE_STEP:
                raise RuntimeError("injected")

        with patch.object(
            self.store, "_after_product_authority_bundle_step", side_effect=fail_after_pin
        ):
            with self.assertRaises(RuntimeError):
                await self.post(body, "pin-rollback")
        self.assertFalse(self.store.guest_capture_secret_ids())
        self.assertEqual(
            {binding.binding_key for binding in self.store.list_secret_bindings()},
            {record.name for record in self.originals},
        )
        self.assertEqual((await self.post(body, "pin-rollback")).status_code, 202)
