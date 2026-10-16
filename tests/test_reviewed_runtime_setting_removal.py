from __future__ import annotations

import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.runtime_setting_removal import RuntimeSettingRemovalRequest, REVIEW_ROUTE, ROUTE
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LocalAdminIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_authz_administration_read import (
    _record,
    _database_app,
    _human_session_manager,
    _browser_headers,
)
from tests.support.http import lifespan_client


def admin() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="admin",
        github_id=101,
        name="",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_humans": [
                {
                    "github_ids": [101],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["authz_policy_grant.write", "product_config.apply", "secret.list"],
                }
            ],
        }
    )


def runtime(**changes: object) -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord.model_validate(
        {
            "scope": "global",
            "env": {"REMOVE": "private-value", "KEEP": "surviving-value"},
            "updated_at": "2026-10-15T12:00:00Z",
            "source_label": "existing",
            **changes,
        }
    )


def request(**changes: object) -> RuntimeSettingRemovalRequest:
    return RuntimeSettingRemovalRequest.model_validate(
        {
            "product": "launchplane",
            "scope": "global",
            "keys": ["REMOVE"],
            "reason": "Reviewed obsolete key",
            **changes,
        }
    )


class RuntimeRemovalTests(unittest.TestCase):
    database_url: str = ""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{directory.name}/db"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.active = self.store.seed_authz_policy_if_absent(_record(policy=policy()))
        self.before = runtime()
        self.store.write_runtime_environment_record(self.before)

    def call(self, body: RuntimeSettingRemovalRequest, key: str = "") -> dict[str, object]:
        return self.store.runtime_setting_removal(
            request=body, identity=admin(), trace_id="trace", idempotency_key=key
        )

    def reviewed(self, **changes: object) -> RuntimeSettingRemovalRequest:
        dry = self.call(request(**changes))
        return request(
            **changes,
            mode="apply",
            review_id=dry["review_id"],
            reviewed_plan_sha256=dry["plan_sha256"],
        )

    def test_exact_deletion_survivors_archive_readback_and_lost_reply_replay(self) -> None:
        body = self.reviewed()
        self.assertEqual(self.store.list_runtime_environment_records(), (self.before,))
        result = self.call(body, "original-key")
        after = self.store.list_runtime_environment_records()[0]
        self.assertEqual(after.env, {"KEEP": self.before.env["KEEP"]})
        self.assertEqual(after.source_label, self.before.source_label)
        self.assertNotIn("private-value", json.dumps(result))
        self.assertNotIn("surviving-value", json.dumps(result))
        archive = self.store.read_idempotency_record(
            scope="runtime-setting-review", route_path=REVIEW_ROUTE, idempotency_key=body.review_id
        )
        assert archive is not None
        self.assertEqual(archive.response_payload["before"], self.before.model_dump(mode="json"))
        self.assertTrue(self.call(body, "original-key")["replayed"])
        self.assertTrue(self.call(body.model_copy(update={"mode": "readback"}))["readback_matches"])
        self.store.write_runtime_environment_record(
            after.model_copy(update={"env": {"KEEP": "drift"}})
        )
        self.assertFalse(
            self.call(body.model_copy(update={"mode": "readback"}))["readback_matches"]
        )

    def test_previews_use_private_nonces_instead_of_a_value_guessing_digest(self) -> None:
        first = self.call(request())
        second = self.call(request())
        self.assertNotEqual(first["record_sha256"], second["record_sha256"])
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])
        archive = self.store.read_idempotency_record(
            scope="runtime-setting-review",
            route_path=REVIEW_ROUTE,
            idempotency_key=str(first["review_id"]),
        )
        assert archive is not None
        self.assertNotIn(archive.response_payload["private_nonce"], json.dumps(first))
        self.assertEqual(self.store.list_runtime_environment_records(), (self.before,))

    def test_last_key_removes_record_and_keeps_delete_event_and_preimage(self) -> None:
        self.before = runtime(env={"REMOVE": "private-value"})
        self.store.write_runtime_environment_record(self.before)
        body = self.reviewed()
        self.call(body, "last-key")
        self.assertEqual(self.store.list_runtime_environment_records(), ())
        events = self.store.list_runtime_environment_delete_events()
        self.assertEqual(len(events), 1)
        self.assertIn(body.review_id, events[0].detail)
        self.assertTrue(self.call(body.model_copy(update={"mode": "readback"}))["readback_matches"])

    def test_last_value_preserves_retirement_intent(self) -> None:
        from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
        from tests.support.profiles import _odoo_profile_payload_with_prod_lane

        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(
                _odoo_profile_payload_with_prod_lane("site")
            )
        )
        payload = policy().model_dump(mode="json")
        payload["github_humans"].append(
            {
                "github_ids": [101],
                "roles": ["admin"],
                "products": ["site"],
                "contexts": ["cm"],
                "instances": ["testing"],
                "actions": ["product_config.apply", "secret.list"],
            }
        )
        active = _record(policy=LaunchplaneAuthzPolicy.model_validate(payload), revision=2)
        self.store.compare_and_write_authz_policy_record(
            expected_record=self.active, replacement_record=active
        )
        record = runtime(
            scope="instance",
            context="cm",
            instance="testing",
            schema_version=2,
            env={"REMOVE": "value"},
            retired_provider_keys=["OLD_PROVIDER_KEY"],
        )
        self.store.write_runtime_environment_record(record)
        body = self.reviewed(product="site", scope="instance", context="cm", instance="testing")
        self.call(body, "last-with-intent")
        after = self.store.list_runtime_environment_records(
            scope="instance", context_name="cm", instance_name="testing"
        )[0]
        self.assertEqual(after.env, {})
        self.assertEqual(after.retired_provider_keys, record.retired_provider_keys)
        self.assertTrue(self.call(body.model_copy(update={"mode": "readback"}))["readback_matches"])
        with self.assertRaises(ValueError):
            RuntimeEnvironmentRecord.model_validate(
                {**record.model_dump(), "env": {}, "retired_provider_keys": []}
            )

    def test_drift_type_changes_and_key_or_reason_changes_refuse(self) -> None:
        for changes in (
            {"env": {"REMOVE": True, "KEEP": "surviving-value"}},
            {"source_label": "changed"},
            {"updated_at": "2026-10-15T12:01:00Z"},
        ):
            with self.subTest(changes=changes):
                self.store.write_runtime_environment_record(self.before)
                body = self.reviewed()
                changed = self.before.model_copy(update=changes)
                self.store.write_runtime_environment_record(changed)
                with self.assertRaises(ValueError):
                    self.call(body, "drift")
                self.assertEqual(self.store.list_runtime_environment_records(), (changed,))
        self.store.write_runtime_environment_record(self.before)
        body = self.reviewed()
        for change in ({"keys": ("KEEP",)}, {"reason": "other"}, {"review_id": "missing"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.call(body.model_copy(update=change), "changed")

    def test_transaction_failure_keeps_values_and_no_success_receipt(self) -> None:
        body = self.reviewed()

        def fail(step: str) -> None:
            if step == "runtime_setting_removal_receipt":
                raise RuntimeError("injected before commit")

        with patch.object(self.store, "_after_product_authority_bundle_step", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.call(body, "atomic")
        self.assertEqual(self.store.list_runtime_environment_records(), (self.before,))
        self.assertNotIn("replayed", self.call(body, "atomic"))

    def test_policy_drift_and_nonhuman_apply_fail_closed(self) -> None:
        body = self.reviewed()
        changed = _record(policy=policy(), revision=2)
        self.store.compare_and_write_authz_policy_record(
            expected_record=self.active, replacement_record=changed
        )
        with self.assertRaises(ValueError):
            self.call(body, "policy-drift")
        nonhuman = LocalAdminIdentity(subject="authz-admin", token_label="authz-admin-label")
        with self.assertRaises(PermissionError):
            self.store.runtime_setting_removal(
                request=body, identity=nonhuman, trace_id="trace", idempotency_key="no"
            )

    def test_unreviewed_ambiguous_or_wrong_scope_never_changes_values(self) -> None:
        for body in (
            request(keys=["MISSING"]),
            request(product="other", scope="context", context="other"),
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.call(body)
        for changes in (
            {"mode": "apply"},
            {"scope": "global", "context": "site"},
            {"keys": ["bad\nkey"]},
            {"keys": ["REMOVE", "REMOVE"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                request(**changes)
        self.assertEqual(self.store.list_runtime_environment_records(), (self.before,))

    def test_unprintable_survivors_report_coverage_gap_without_echo(self) -> None:
        unsafe = "unsafe\nvalue-like-name"
        self.store.write_runtime_environment_record(runtime(env={"REMOVE": "x", unsafe: "private"}))
        plan = self.call(request())
        self.assertEqual(plan["unprintable_survivor_count"], 1)
        self.assertNotIn(unsafe, json.dumps(plan))
        self.assertNotIn("private", json.dumps(plan))


class RuntimeRemovalHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_can_plan_and_bearer_cannot_apply(self) -> None:
        with _database_app(policy(), director_agent=True) as (store, _, app):
            store.write_runtime_environment_record(runtime())
            headers = {"Authorization": "Bearer reader-token"}
            async with lifespan_client(app) as client:
                dry = await client.post(
                    ROUTE,
                    json=request().model_dump(mode="json"),
                    headers=headers,
                )
                self.assertEqual(dry.status_code, 200, dry.text)
                result = dry.json()["result"]
                applied = await client.post(
                    ROUTE,
                    json=request(
                        mode="apply",
                        review_id=result["review_id"],
                        reviewed_plan_sha256=result["plan_sha256"],
                    ).model_dump(mode="json"),
                    headers={**headers, "Idempotency-Key": "denied"},
                )
                self.assertEqual(applied.status_code, 403, applied.text)
                self.assertEqual(store.list_runtime_environment_records(), (runtime(),))

    async def test_browser_review_apply_and_value_free_readback(self) -> None:
        sessions, _ = _human_session_manager()
        with _database_app(policy(), human_session_manager=sessions) as (store, _, app):
            store.write_runtime_environment_record(runtime())
            headers, _, _ = _browser_headers(sessions, admin())
            async with lifespan_client(app) as client:
                dry = await client.post(
                    ROUTE,
                    json=request().model_dump(mode="json"),
                    headers=headers,
                )
                self.assertEqual(dry.status_code, 200, dry.text)
                plan = dry.json()["result"]
                body = request(
                    mode="apply",
                    review_id=plan["review_id"],
                    reviewed_plan_sha256=plan["plan_sha256"],
                )
                applied = await client.post(
                    ROUTE,
                    json=body.model_dump(mode="json"),
                    headers={**_browser_headers(sessions, admin())[0], "Idempotency-Key": "http"},
                )
                self.assertEqual(applied.status_code, 200, applied.text)
                self.assertTrue(applied.json()["result"]["applied"])
                self.assertNotIn("private-value", applied.text)


class RuntimeRemovalPostgresScenarios:
    """Invoked by the PostgreSQL integration gate, using its isolated database."""

    @staticmethod
    def simultaneous_keys(store: PostgresRecordStore) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        gate = Barrier(2)
        dry = store.runtime_setting_removal(request=request(), identity=admin(), trace_id="dry")
        body = request(
            mode="apply", review_id=dry["review_id"], reviewed_plan_sha256=dry["plan_sha256"]
        )

        def apply() -> dict[str, object]:
            gate.wait(timeout=30)
            return store.runtime_setting_removal(
                request=body, identity=admin(), trace_id="concurrent", idempotency_key="shared-key"
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: apply(), range(2)))
        assert sum(bool(result.get("replayed")) for result in results) == 1
        assert store.list_runtime_environment_records()[0].env == {"KEEP": "surviving-value"}
