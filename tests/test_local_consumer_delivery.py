import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from click.testing import CliRunner
from cryptography.fernet import Fernet
from httpx2 import Response

from control_plane import secrets
from control_plane.cli_local_consumer import local_consumer
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.local_consumer import prepare_consumer, receive_consumer
from control_plane.local_consumer_contract import (
    BRIDGE_BINDING,
    DELIVERY_ROUTE,
    VERIFY_ROUTE,
    ConsumerPackage,
    ConsumerRequest,
    canonical,
    digest,
)
from control_plane.local_consumer_delivery import (
    issue_delivery,
    plan_delivery,
    prepare_delivery,
    verify_delivery,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneAuthzPolicy
from control_plane.service_auth import BearerIdentityConfig
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import sqlite_database_url, _write_runtime_key_safety_policy
from tests.support.auth import _local_operator_policy

FAKE_VALUE = "fictional-testing-bridge-credential"


class LocalConsumerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        database_url = sqlite_database_url(self.root / "store.sqlite3")
        _write_runtime_key_safety_policy(database_url=database_url)
        self.store = PostgresRecordStore(database_url=database_url)
        self.addCleanup(self.store.close)
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
        payload = _generic_site_profile_payload()
        payload["expected_config"] = {
            "managed_secret_bindings": [
                {
                    "binding_key": BRIDGE_BINDING,
                    "integration": "runtime_environment",
                    "context": "example-site",
                    "instance": "testing",
                }
            ]
        }
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.write_value(FAKE_VALUE)
        self.endpoint = PrivateHealthEndpointRecord(
            endpoint_key="testing-bridge",
            product="example-site",
            context="example-site",
            instance="testing",
            url="https://10.77.0.9:9443/health",
            updated_at="2026-10-15T12:00:00Z",
        )
        self.store.write_private_health_endpoint_record(self.endpoint)
        self.directory = self.root / "testing-client"
        self.request = self.prepare(self.directory)
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "logins": ["operator"],
                            "roles": ["admin"],
                            "products": ["example-site"],
                            "contexts": ["example-site"],
                            "instances": ["testing"],
                            "actions": ["product_config.plan", "product_config.apply"],
                        }
                    ],
                }
            ),
        )

    def prepare(self, directory: Path) -> ConsumerRequest:
        return prepare_consumer(
            directory=directory,
            service_origin="https://launchplane.example",
            product="example-site",
            context="example-site",
            endpoint_key="testing-bridge",
            server_url="wss://10.77.0.9:9443/agent-session/connect",
        )

    def write_value(self, value: str) -> None:
        secrets.write_secret_value(
            record_store=self.store,
            scope="context_instance",
            integration="runtime_environment",
            name="testing-bridge",
            plaintext_value=value,
            binding_key=BRIDGE_BINDING,
            context_name="example-site",
            instance_name="testing",
            actor="fixture",
        )
        binding = self.store.list_secret_bindings(integration="runtime_environment", limit=None)[0]
        self.store.write_secret_binding(
            binding.model_copy(update={"declared_secret_class": "testing"})
        )

    def issue(self) -> ConsumerPackage:
        plan = prepare_delivery(self.store, self.request, actor="fixture", trace_id="plan")
        return issue_delivery(
            self.store,
            self.request,
            actor="fixture",
            trace_id="issue",
            reviewed_plan_digest=digest(plan),
        )

    def package_file(self, package: ConsumerPackage) -> Path:
        path = self.root / "download.json"
        path.write_bytes(canonical(package))
        return path

    async def post(
        self, body: dict[str, object], *, role: str = "admin", csrf: bool = True
    ) -> Response:
        human = GitHubHumanIdentity(
            login="operator",
            github_id=100,
            name="Operator",
            email="",
            organizations=frozenset(),
            teams=frozenset(),
            role=role,  # type: ignore[arg-type]
        )
        session = self.sessions.issue(human)
        headers = _browser_mutation_headers(self.sessions, session)
        if not csrf:
            headers.pop("X-CSRF-Token", None)
        return await _asgi_request(self.app, "POST", DELIVERY_ROUTE, headers=headers, payload=body)

    def test_dry_run_reads_no_value_and_exact_replay_returns_same_ciphertext(self) -> None:
        with patch("control_plane.local_consumer_delivery.copy_source_value") as value:
            prepare_delivery(self.store, self.request, actor="fixture", trace_id="plan")
        value.assert_not_called()
        package = self.issue()
        self.assertEqual(package, self.issue())
        self.assertEqual(verify_delivery(self.store, package), digest(package))
        self.assertNotIn(FAKE_VALUE, canonical(package).decode())
        with patch("control_plane.local_consumer_delivery.copy_source_value") as value:
            verify_delivery(self.store, package)
        value.assert_not_called()

    def test_rotation_invalidates_review_and_issued_delivery(self) -> None:
        package = self.issue()
        self.write_value("fictional-rotated-bridge-value")
        with self.assertRaises(ValueError):
            verify_delivery(self.store, package)
        with self.assertRaises(ValueError):
            self.issue()

    def test_binding_revocation_and_class_changes_refuse(self) -> None:
        package = self.issue()
        binding = self.store.list_secret_bindings(limit=None)[0]
        for change in (
            {"status": "disabled"},
            {"declared_secret_class": "prod_only"},
            {"declared_secret_class": "shared_safe"},
            {"instance": "prod"},
        ):
            with self.subTest(change=change):
                self.store.write_secret_binding(binding.model_copy(update=change))
                with self.assertRaises(ValueError):
                    verify_delivery(self.store, package)
        self.store.write_secret_binding(binding)

    def test_cross_product_missing_declaration_and_endpoint_changes_refuse(self) -> None:
        for changes in (
            {"product": "another-product"},
            {"context": "another-context"},
            {"endpoint_key": "absent"},
            {"server_url": "wss://production.internal.example/agent-session/connect"},
        ):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                plan_delivery(self.store, self.request.model_copy(update=changes))
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "expected_config": self.profile.expected_config.model_copy(
                        update={"managed_secret_bindings": ()}
                    )
                }
            )
        )
        with self.assertRaises(ValueError):
            plan_delivery(self.store, self.request)

    def test_endpoint_disabled_or_production_never_delivers(self) -> None:
        for change in (
            {"status": "disabled"},
            {"instance": "prod"},
            {"url": "http://10.77.0.9:9443/health"},
        ):
            with self.subTest(change=change):
                self.store.write_private_health_endpoint_record(
                    self.endpoint.model_copy(update=change)
                )
                with self.assertRaises(ValueError):
                    plan_delivery(self.store, self.request)

    def test_no_apply_without_original_plan_or_matching_administrator(self) -> None:
        with self.assertRaises(ValueError):
            issue_delivery(
                self.store, self.request, actor="fixture", trace_id="x", reviewed_plan_digest=""
            )
        plan = prepare_delivery(self.store, self.request, actor="fixture", trace_id="plan")
        with self.assertRaises(ValueError):
            issue_delivery(
                self.store,
                self.request,
                actor="someone-else",
                trace_id="x",
                reviewed_plan_digest=digest(plan),
            )

    def test_package_tampering_and_wrong_recipient_never_verify(self) -> None:
        package = self.issue()
        for changed in (
            package.model_copy(update={"ciphertext": "forged"}),
            package.model_copy(update={"request": self.prepare(self.root / "another-client")}),
        ):
            with self.assertRaises(ValueError):
                verify_delivery(self.store, changed)

    def test_production_preview_and_plain_transport_are_schema_refusals(self) -> None:
        for change in (
            {"environment": "prod"},
            {"environment": "pr-1"},
            {"server_url": "ws://10.77.0.9:9443/agent-session/connect"},
            {"service_origin": "http://launchplane.example"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ConsumerRequest.model_validate({**self.request.model_dump(), **change})

    def test_expired_request_and_changed_profile_invalidate_receipt(self) -> None:
        package = self.issue()
        with self.assertRaises(ValueError):
            self.request.require_fresh(now=self.request.expires_at)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"display_name": "Renamed product"})
        )
        with self.assertRaises(ValueError):
            verify_delivery(self.store, package)

    def test_receiver_installs_private_file_and_recovers_original_partial_delivery(self) -> None:
        package = self.issue()
        downloaded = self.package_file(package)
        with (
            patch("control_plane.local_consumer._verify_service") as verify,
            patch("control_plane.local_consumer._verify_bridge") as bridge,
        ):
            result = receive_consumer(self.directory, downloaded)
            # A lost local receipt is recovered from the original authenticated package.
            (self.directory / "delivery.json").unlink()
            self.assertEqual(result, receive_consumer(self.directory, downloaded))
        self.assertEqual(verify.call_count, 4)
        self.assertEqual(bridge.call_args.args, (self.request, FAKE_VALUE))
        self.assertEqual((self.directory / "bridge.token").read_text(), FAKE_VALUE)
        for name in ("bridge.token", "codex-bridge.toml", "recipient.pem", "delivery.json"):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o600)
        self.assertNotIn(FAKE_VALUE, json.dumps(result))

    def test_receiver_does_not_write_when_server_or_service_verification_fails(self) -> None:
        package_file = self.package_file(self.issue())
        for boundary in ("_verify_service", "_verify_bridge"):
            with (
                self.subTest(boundary=boundary),
                patch("control_plane.local_consumer._verify_service"),
                patch("control_plane.local_consumer._verify_bridge"),
                patch(
                    f"control_plane.local_consumer.{boundary}",
                    side_effect=ValueError("unavailable"),
                ),
                self.assertRaises(ValueError),
            ):
                receive_consumer(self.directory, package_file)
            self.assertFalse((self.directory / "bridge.token").exists())

    def test_existing_paths_symlinks_and_changed_files_are_preserved(self) -> None:
        with self.assertRaises(FileExistsError):
            self.prepare(self.directory)
        token_path = self.directory / "bridge.token"
        untouched = self.root / "production-token"
        untouched.write_text("leave-production-alone")
        token_path.symlink_to(untouched)
        with (
            patch("control_plane.local_consumer._verify_service"),
            patch("control_plane.local_consumer._verify_bridge"),
            self.assertRaises((ValueError, OSError)),
        ):
            receive_consumer(self.directory, self.package_file(self.issue()))
        self.assertEqual(untouched.read_text(), "leave-production-alone")

    def test_cli_failure_cannot_print_a_transport_exception_containing_a_value(self) -> None:
        with patch(
            "control_plane.cli_local_consumer.receive_consumer", side_effect=ValueError(FAKE_VALUE)
        ):
            result = CliRunner().invoke(
                local_consumer,
                ["receive", "--directory", str(self.directory), "--package-file", "not-used.json"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertNotIn(FAKE_VALUE, result.output)

    async def test_human_csrf_review_apply_and_public_verification(self) -> None:
        body: dict[str, object] = {
            "request": self.request.model_dump(mode="json"),
            "mode": "dry-run",
        }
        response = await self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        body.update(mode="apply", reviewed_plan_digest=response.json()["plan_digest"])
        response = await self.post(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(FAKE_VALUE, response.text)
        verified = await _asgi_request(
            self.app, "POST", VERIFY_ROUTE, payload=response.json()["package"]
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(set(verified.json()), {"status", "package_digest"})

    async def test_read_only_human_and_missing_csrf_cannot_issue(self) -> None:
        body = {"request": self.request.model_dump(mode="json")}
        for kwargs in ({"role": "read_only"}, {"csrf": False}):
            response = await self.post(body, **kwargs)  # type: ignore[arg-type]
            self.assertEqual(response.status_code, 403, response.text)

    async def test_destination_can_be_verified_before_first_value_entry(self) -> None:
        database_url = sqlite_database_url(self.root / "empty.sqlite3")
        _write_runtime_key_safety_policy(database_url=database_url)
        empty = PostgresRecordStore(database_url=database_url)
        self.addCleanup(empty.close)
        empty.write_product_profile_record(self.profile)
        empty.write_private_health_endpoint_record(self.endpoint)
        self.store = empty
        with patch("control_plane.local_consumer_delivery.copy_source_value") as value:
            response = await self.post({"request": self.request.model_dump(mode="json")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ready_to_deliver"])
        self.assertEqual(response.json()["request_fingerprint"], digest(self.request))
        self.assertIsNone(response.json()["package"])
        value.assert_not_called()

    async def test_nonhuman_with_existing_config_actions_cannot_issue(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            record_store_factory=lambda: self.store,
            authz_policy=_local_operator_policy(
                actions=("product_config.plan", "product_config.apply")
            ),
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        with patch("control_plane.local_consumer_delivery.copy_source_value") as value:
            response = await _asgi_request(
                app,
                "POST",
                DELIVERY_ROUTE,
                headers={"Authorization": "Bearer fixture-operator-token"},
                payload={"request": self.request.model_dump(mode="json")},
            )
        self.assertEqual(response.status_code, 403, response.text)
        value.assert_not_called()

    def test_shared_secret_reference_refuses_before_decryption(self) -> None:
        binding = self.store.list_secret_bindings(limit=None)[0]
        self.store.write_secret_binding(
            binding.model_copy(
                update={
                    "binding_id": "another-consumer",
                    "context": "another-context",
                    "instance": "prod",
                }
            )
        )
        with self.assertRaises(ValueError):
            self.issue()

    def test_actual_transport_checks_identity_and_only_authenticates_exact_websocket(self) -> None:
        from control_plane.local_consumer import _verify_bridge

        identity = {
            "product": self.request.product,
            "context": self.request.context,
            "instance": "testing",
            "deployment_record_id": "fixture-deployment",
            "artifact_id": "fixture-artifact",
            "source_git_ref": "fixture-source",
            "environment_kind": "stable",
        }
        with (
            patch("control_plane.local_consumer.requests.Session") as factory,
            patch("control_plane.local_consumer.socket.create_connection") as socket_factory,
            patch("control_plane.local_consumer.connect") as websocket,
        ):
            session = factory.return_value.__enter__.return_value
            response = session.get.return_value
            response.status_code = 200
            response.content = b"fixture-health"
            response.json.return_value = {"status": "ok", "runtime_identity": identity}
            _verify_bridge(self.request, FAKE_VALUE)
            self.assertFalse(session.trust_env)
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
            self.assertNotIn("headers", session.get.call_args.kwargs)
            self.assertIsNone(websocket.call_args.kwargs["proxy"])
            self.assertIs(
                websocket.call_args.kwargs["sock"],
                socket_factory.return_value.__enter__.return_value,
            )
            self.assertEqual(websocket.call_args.args, (self.request.server_url,))
            self.assertEqual(
                websocket.call_args.kwargs["additional_headers"],
                {"Authorization": f"Bearer {FAKE_VALUE}"},
            )
            with self.assertNoLogs(level="DEBUG"):
                websocket.call_args.kwargs["logger"].debug("Authorization: %s", FAKE_VALUE)
            websocket.return_value.__enter__.return_value.send.assert_not_called()
            websocket.reset_mock()
            response.json.return_value = {
                "status": "ok",
                "runtime_identity": {**identity, "instance": "prod"},
            }
            with self.assertRaises(ValueError):
                _verify_bridge(self.request, FAKE_VALUE)
            websocket.assert_not_called()


class LocalConsumerTransportTests(unittest.TestCase):
    def test_service_verification_uses_original_https_origin_without_redirects_or_credentials(
        self,
    ) -> None:
        from control_plane.local_consumer import _verify_service

        package = Mock()
        package.request.service_origin = "https://control.example"
        package.model_dump.return_value = {"ciphertext": "encrypted-fixture"}
        response = Mock(status_code=302, content=b"")
        with patch("control_plane.local_consumer.requests.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value = response
            with self.assertRaises(ValueError):
                _verify_service(package)
            self.assertFalse(session.trust_env)
            self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
            self.assertNotIn("headers", session.post.call_args.kwargs)
