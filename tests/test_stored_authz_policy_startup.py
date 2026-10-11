from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane import service_bootstrap
from control_plane.contracts.authz_policy_record import authz_policy_sha256
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubActionsPolicyRule,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.postgres import LaunchplaneAuthzPolicyRow, PostgresRecordStore
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import lifespan_client
from tests.support.stores import _sqlite_database_url


class _RequestRefreshStore(PostgresRecordStore):
    @property
    def database_dialect_name(self) -> str:
        # Exercise production request-refresh selection with isolated SQLite rows.
        return "postgresql"


class StoredAuthzPolicyStartupTests(unittest.TestCase):
    def test_stored_context_runtime_rules_survive_restart_descriptor_registration(self) -> None:
        self._exercise_startup()

    def test_explicit_restart_selectors_authorize_only_the_named_instance(self) -> None:
        identity = _identity()
        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            github_actions=(
                GitHubActionsPolicyRule(
                    repository=identity.repository,
                    products=("example-site",),
                    contexts=("example-site",),
                    actions=("live_target_runtime.plan", "live_target_runtime.apply"),
                    instances=("testing",),
                ),
            ),
        )
        for action in ("live_target_runtime.plan", "live_target_runtime.apply"):
            for instance in ("testing", "prod", None):
                with self.subTest(action=action, instance=instance):
                    self.assertEqual(
                        instance == "testing",
                        policy.allows(
                            identity=identity,
                            action=action,
                            product="example-site",
                            context="example-site",
                            target=(
                                AuthorizationTarget(scope="instance", instances=(instance,))
                                if instance is not None
                                else None
                            ),
                        ),
                    )

    def test_invalid_stored_policy_keeps_liveness_but_blocks_authorized_api(self) -> None:
        for corruption in ("instance_rule", "digest"):
            with self.subTest(corruption=corruption):
                self._exercise_startup(corruption=corruption)

    def test_request_refresh_recovers_only_after_a_valid_policy_is_read(self) -> None:
        self._exercise_startup(corruption="digest", request_refresh=True)

    def _exercise_startup(self, *, corruption: str = "", request_refresh: bool = False) -> None:
        identity = _identity()
        # This is a stored schema-v2 shape supported before service_restart was
        # registered. Construct it without today's policy-level validation, then
        # insert its serialized row: startup must perform the real stored read.
        historical_policy = LaunchplaneAuthzPolicy.model_construct(
            schema_version=2,
            github_actions=(
                GitHubActionsPolicyRule(
                    repository=identity.repository,
                    products=("example-site",),
                    contexts=("example-site",),
                    actions=("live_target_runtime.plan", "live_target_runtime.apply"),
                ),
                GitHubActionsPolicyRule(
                    repository=identity.repository,
                    products=("example-site",),
                    contexts=("example-site",),
                    instances=("testing",),
                    actions=("deployment.write",),
                ),
            ),
        )
        payload: dict[str, Any] = {
            "record_id": "stored-policy",
            "revision": 4,
            "status": "active",
            "source": "managed-policy",
            "updated_at": "2026-10-10T12:00:00Z",
            "policy": historical_policy.model_dump(mode="json"),
            "policy_sha256": authz_policy_sha256(historical_policy),
            "audit": {},
        }
        valid_payload = deepcopy(payload)
        if corruption == "instance_rule":
            payload["policy"]["github_actions"][1]["instances"] = []
        elif corruption == "digest":
            payload["policy_sha256"] = "f" * 64
        original_payload = deepcopy(payload)
        captured_runtime: LaunchplaneAuthzPolicyRuntime | None = None

        def build_runtime(policy: LaunchplaneAuthzPolicy, **kwargs: Any) -> Any:
            nonlocal captured_runtime
            captured_runtime = LaunchplaneAuthzPolicyRuntime(policy, **kwargs)
            return captured_runtime

        async def exercise_http(app: Any) -> None:
            async with lifespan_client(app) as client:
                health = await client.get("/v1/health")
                self.assertEqual(200, health.status_code)
                if corruption:
                    response = await client.get(
                        "/v1/products",
                        headers={"Authorization": "Bearer valid-token"},
                    )
                    self.assertEqual(503, response.status_code)
                    self.assertEqual("authz_policy_unavailable", response.json()["error"]["code"])
                    assert captured_runtime is not None
                    self.assertFalse(
                        captured_runtime.policy.allows(
                            identity=identity,
                            action="product_profile.read",
                            product="another-site",
                            context="another-site",
                        )
                    )
                    if request_refresh:
                        with store._session_factory() as session:
                            row = session.get(LaunchplaneAuthzPolicyRow, "stored-policy")
                            assert row is not None
                            self.assertEqual(original_payload, row.payload)
                            # Simulate an independently corrected local fixture row.
                            row.payload = valid_payload
                            row.policy_sha256 = str(valid_payload["policy_sha256"])
                            session.commit()
                        response = await client.get(
                            "/v1/products",
                            headers={"Authorization": "Bearer valid-token"},
                        )
                        # Refresh succeeded; the stored policy still denies this
                        # action rather than granting the broad bootstrap policy.
                        self.assertEqual(403, response.status_code)
                        self.assertEqual("authorization_denied", response.json()["error"]["code"])

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy_file = root / "bootstrap.toml"
            # A broad bootstrap grant must never become a fallback for a bad DB row.
            policy_file.write_text(
                f'schema_version = 1\n[[github_actions]]\nrepository = "{identity.repository}"\n',
                encoding="utf-8",
            )
            store_type = _RequestRefreshStore if request_refresh else PostgresRecordStore
            store = store_type(database_url=database_url)
            self.addCleanup(store.close)
            store.ensure_schema()
            with store._session_factory() as session:
                session.add(
                    LaunchplaneAuthzPolicyRow(
                        record_id=payload["record_id"],
                        revision=payload["revision"],
                        status=payload["status"],
                        source=payload["source"],
                        updated_at=payload["updated_at"],
                        policy_sha256=payload["policy_sha256"],
                        payload=payload,
                    )
                )
                session.commit()
            with (
                patch.dict("os.environ", {}, clear=True),
                patch.object(service_bootstrap, "HealthMonitorScheduler"),
                patch.object(service_bootstrap, "build_shared_record_store", return_value=store),
                patch.object(service_bootstrap, "control_plane_root", return_value=root),
                patch.object(
                    service_bootstrap, "GitHubOidcVerifier", return_value=_StubVerifier(identity)
                ),
                patch.object(
                    service_bootstrap, "LaunchplaneAuthzPolicyRuntime", side_effect=build_runtime
                ),
                patch(
                    "uvicorn.run", side_effect=lambda app, **kwargs: asyncio.run(exercise_http(app))
                ),
            ):
                service_bootstrap.serve_launchplane_service(
                    state_dir=root / "state",
                    policy_file=policy_file,
                    host="127.0.0.1",
                    port=8080,
                    audience="launchplane-service",
                    database_url=database_url,
                )
            self.assertIsNotNone(captured_runtime)
            assert captured_runtime is not None
            policy_available = not corruption or request_refresh
            for action in ("live_target_runtime.plan", "live_target_runtime.apply"):
                self.assertEqual(
                    policy_available,
                    captured_runtime.policy.allows(
                        identity=identity,
                        action=action,
                        product="example-site",
                        context="example-site",
                    ),
                )
                # A schema-v2 context rule never becomes a restart grant.
                self.assertFalse(
                    captured_runtime.policy.allows(
                        identity=identity,
                        action=action,
                        product="example-site",
                        context="example-site",
                        target=AuthorizationTarget(scope="instance", instances=("testing",)),
                    )
                )
                self.assertFalse(
                    captured_runtime.policy.allows(
                        identity=identity,
                        action=action,
                        product="another-site",
                        context="example-site",
                    )
                )
            if policy_available:
                self.assertEqual(valid_payload["policy_sha256"], captured_runtime.policy_sha256)
                self.assertEqual("db", captured_runtime.source)
                self.assertEqual(payload["record_id"], captured_runtime.record_id)
                self.assertEqual(payload["revision"], captured_runtime.revision)
                self.assertTrue(
                    captured_runtime.policy.allows(
                        identity=identity,
                        action="deployment.write",
                        product="example-site",
                        context="example-site",
                        target=AuthorizationTarget(scope="instance", instances=("testing",)),
                    )
                )
                self.assertFalse(
                    captured_runtime.policy.allows(
                        identity=identity,
                        action="deployment.write",
                        product="example-site",
                        context="example-site",
                        target=AuthorizationTarget(scope="instance", instances=("prod",)),
                    )
                )
            with store._session_factory() as session:
                row = session.get(LaunchplaneAuthzPolicyRow, "stored-policy")
                assert row is not None
                self.assertEqual(
                    valid_payload if request_refresh else original_payload, row.payload
                )
