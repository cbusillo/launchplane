from __future__ import annotations

import json
from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.runtime_setting_removal import (
    RuntimeSettingRemovalRequest,
    RuntimeSettingRemovalPlan,
    RuntimeSettingRemovalStale,
    REVIEW_ROUTE,
    ROUTE,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.postgres import PostgresRecordStore, LaunchplaneProductionRecipientRow
from tests.test_authz_administration_read import (
    _record,
    _database_app,
    _human_session_manager,
    _browser_headers,
)
from tests.support.http import lifespan_client
from tests.support.stored_profiles import persist_historical_preview_transport
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


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


def preview_policy() -> LaunchplaneAuthzPolicy:
    payload = policy().model_dump(mode="json")
    rule = {
        "subjects": ["record-reader"],
        "token_labels": ["record-reader-label"],
        "products": ["site"],
        "contexts": ["cm"],
        "instances": ["preview", "testing", "prod"],
        "actions": ["product_config.apply"],
    }
    payload["local_operators"] = [rule]
    payload["terminal_agents"] = [rule]
    return LaunchplaneAuthzPolicy.model_validate(payload)


def preview_removal(
    store: PostgresRecordStore,
) -> tuple[RuntimeEnvironmentRecord, RuntimeSettingRemovalRequest]:
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane("site"))
    )
    record = runtime(scope="instance", context="cm", instance="preview")
    store.write_runtime_environment_record(record)
    return record, request(product="site", scope="instance", context="cm", instance="preview")


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
        ).model_dump(mode="json", exclude_none=True)

    def reviewed(self, **changes: object) -> RuntimeSettingRemovalRequest:
        dry = self.call(request(**changes))
        return request(
            **changes,
            mode="apply",
            review_id=dry["review_id"],
            reviewed_plan_sha256=dry["plan_sha256"],
        )

    def reviewed_preview(
        self,
    ) -> tuple[RuntimeSettingRemovalRequest, LocalOperatorIdentity]:
        replacement = _record(policy=preview_policy(), revision=self.active.revision + 1)
        self.store.compare_and_write_authz_policy_record(
            expected_record=self.active, replacement_record=replacement
        )
        self.active = replacement
        _, body = preview_removal(self.store)
        operator = LocalOperatorIdentity(
            subject="record-reader", token_label="record-reader-label", director_agent=True
        )
        dry = self.store.runtime_setting_removal(request=body, identity=operator, trace_id="trace")
        return body.model_copy(
            update={
                "mode": "apply",
                "review_id": dry.review_id,
                "reviewed_plan_sha256": dry.plan_sha256,
            }
        ), operator

    def reviewed_testing(self) -> tuple[RuntimeSettingRemovalRequest, GitHubHumanIdentity]:
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
        replacement = _record(
            policy=LaunchplaneAuthzPolicy.model_validate(payload), revision=self.active.revision + 1
        )
        self.store.compare_and_write_authz_policy_record(
            expected_record=self.active, replacement_record=replacement
        )
        self.active = replacement
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(
                _odoo_profile_payload_with_prod_lane("site")
            )
        )
        self.store.write_runtime_environment_record(
            runtime(scope="instance", context="cm", instance="testing")
        )
        body = request(product="site", scope="instance", context="cm", instance="testing")
        identity = admin()
        dry = self.store.runtime_setting_removal(request=body, identity=identity, trace_id="trace")
        return body.model_copy(
            update={
                "mode": "apply",
                "review_id": dry.review_id,
                "reviewed_plan_sha256": dry.plan_sha256,
            }
        ), identity

    def test_preview_review_omits_context_fallback_and_ignores_context_edits(self) -> None:
        body, operator = self.reviewed_preview()
        self.store.write_runtime_environment_record(
            runtime(scope="context", context="cm", env={"REMOVE": "different-private-value"})
        )
        result = self.store.runtime_setting_removal(
            request=body, identity=operator, trace_id="apply", idempotency_key="preview-context"
        )
        self.assertTrue(result.applied)
        self.assertIsNone(result.instance_plain_setting_effects)
        self.assertIsNone(result.context_values_sha256)

    def test_instance_review_reports_context_fallback_without_values(self) -> None:
        body, operator = self.reviewed_testing()
        self.store.write_runtime_environment_record(
            runtime(scope="context", context="cm", env={"REMOVE": "different-private-value"})
        )
        result = self.store.runtime_setting_removal(
            request=body.model_copy(update={"mode": "dry-run"}), identity=operator, trace_id="trace"
        ).model_dump(mode="json", exclude_none=True)
        self.assertEqual(
            result["instance_plain_setting_effects"],
            [{"key": "REMOVE", "source_after": "context", "effect": "changed"}],
        )
        self.assertNotIn("private-value", json.dumps(result))

    def test_instance_apply_refuses_changed_or_new_context_fallback(self) -> None:
        for original_context in (None, "original-context-value"):
            with self.subTest(original_context=original_context):
                if original_context is not None:
                    self.store.write_runtime_environment_record(
                        runtime(scope="context", context="cm", env={"REMOVE": original_context})
                    )
                body, operator = self.reviewed_testing()
                self.store.write_runtime_environment_record(
                    runtime(scope="context", context="cm", env={"REMOVE": "changed-context-value"})
                )
                with self.assertRaises(RuntimeSettingRemovalStale):
                    self.store.runtime_setting_removal(
                        request=body,
                        identity=operator,
                        trace_id="apply",
                        idempotency_key=f"drift-{original_context is not None}",
                    )
                remaining = self.store.list_runtime_environment_records(
                    context_name="cm", instance_name="testing"
                )
                lane = next(record for record in remaining if record.scope == "instance")
                self.assertIn("REMOVE", lane.env)

    def test_instance_review_compares_normalized_site_values_without_global_fallback(self) -> None:
        body, operator = self.reviewed_testing()
        self.store.write_runtime_environment_record(
            runtime(scope="context", context="cm", env={"SAME": 1, "EMPTY": ""})
        )
        self.store.write_runtime_environment_record(
            runtime(
                scope="instance",
                context="cm",
                instance="testing",
                env={"SAME": "1", "EMPTY": "", "GLOBAL_ONLY": "global-value"},
            )
        )
        self.store.write_runtime_environment_record(runtime(env={"GLOBAL_ONLY": "global-value"}))
        result = self.store.runtime_setting_removal(
            request=body.model_copy(
                update={"mode": "dry-run", "keys": ("SAME", "GLOBAL_ONLY", "EMPTY")}
            ),
            identity=operator,
            trace_id="trace",
        )
        effects = result.instance_plain_setting_effects
        assert effects is not None
        self.assertEqual(
            {item.key: (item.source_after, item.effect) for item in effects},
            {
                "EMPTY": ("context", "unchanged"),
                "GLOBAL_ONLY": (None, "absent"),
                "SAME": ("context", "unchanged"),
            },
        )

    def test_instance_apply_ignores_unselected_context_and_global_changes(self) -> None:
        body, operator = self.reviewed_testing()
        self.store.write_runtime_environment_record(
            runtime(scope="context", context="cm", env={"UNRELATED": "new-context-value"})
        )
        self.store.write_runtime_environment_record(runtime(env={"REMOVE": "new-global-value"}))
        result = self.store.runtime_setting_removal(
            request=body, identity=operator, trace_id="apply", idempotency_key="unrelated-context"
        )
        self.assertTrue(result.applied)
        self.assertTrue(result.readback_matches)

    def test_preview_removal_refuses_other_scopes_and_identity_types(self) -> None:
        body, operator = self.reviewed_preview()
        before = self.store.list_runtime_environment_records()
        for changes in (
            {"scope": "global", "product": "launchplane", "context": "", "instance": ""},
            {"scope": "context", "instance": ""},
            {"instance": "testing"},
            {"instance": "prod"},
        ):
            with self.subTest(changes=changes), self.assertRaises(PermissionError):
                self.store.runtime_setting_removal(
                    request=body.model_copy(update=changes),
                    identity=operator,
                    trace_id="trace",
                    idempotency_key="other-scope",
                )
        for identity in (
            replace(operator, director_agent=False),
            TerminalAgentIdentity(
                subject=operator.subject, token_label=operator.token_label, director_agent=True
            ),
            replace(operator, subject="ungranted"),
        ):
            with self.subTest(identity=type(identity).__name__), self.assertRaises(PermissionError):
                self.store.runtime_setting_removal(
                    request=body,
                    identity=identity,
                    trace_id="trace",
                    idempotency_key="other-caller",
                )
        self.assertEqual(self.store.list_runtime_environment_records(), before)

    def test_preview_removal_requires_enabled_unaliased_preview_after_fresh_review(self) -> None:
        body, operator = self.reviewed_preview()
        profile = self.store.read_product_profile_record(body.product)
        before = self.store.list_runtime_environment_records()
        profiles = (
            profile.model_copy(
                update={"preview": profile.preview.model_copy(update={"enabled": False})}
            ),
            profile.model_copy(
                update={
                    "lanes": (
                        *profile.lanes,
                        ProductLaneProfile(context=body.context, instance=body.instance),
                    )
                }
            ),
        )
        for current in profiles:
            with self.subTest(enabled=current.preview.enabled, lanes=len(current.lanes)):
                self.store.write_product_profile_record(current)
                dry = self.store.runtime_setting_removal(
                    request=body.model_copy(update={"mode": "dry-run"}),
                    identity=operator,
                    trace_id="trace",
                )
                with self.assertRaises(PermissionError):
                    self.store.runtime_setting_removal(
                        request=body.model_copy(
                            update={
                                "review_id": dry.review_id,
                                "reviewed_plan_sha256": dry.plan_sha256,
                            }
                        ),
                        identity=operator,
                        trace_id="trace",
                        idempotency_key="unqualified-preview",
                    )
        self.assertEqual(self.store.list_runtime_environment_records(), before)

    def test_preview_removal_rechecks_eligibility_and_write_authority_on_replay(self) -> None:
        body, operator = self.reviewed_preview()
        before = self.store.list_runtime_environment_records(
            scope=body.scope, context_name=body.context, instance_name=body.instance
        )[0]
        self.store.runtime_setting_removal(
            request=body, identity=operator, trace_id="trace", idempotency_key="preview"
        )
        after = self.store.list_runtime_environment_records()
        written = self.store.list_runtime_environment_records(
            scope=body.scope, context_name=body.context, instance_name=body.instance
        )[0]
        self.assertEqual(written.env, {"KEEP": before.env["KEEP"]})
        profile = self.store.read_product_profile_record(body.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={"preview": profile.preview.model_copy(update={"enabled": False})}
            )
        )
        with self.assertRaises(PermissionError):
            self.store.runtime_setting_removal(
                request=body, identity=operator, trace_id="trace", idempotency_key="preview"
            )
        self.store.write_product_profile_record(profile)
        self.store.compare_and_write_authz_policy_record(
            expected_record=self.active,
            replacement_record=_record(policy=policy(), revision=self.active.revision + 1),
        )
        with self.assertRaises(PermissionError):
            self.store.runtime_setting_removal(
                request=body, identity=operator, trace_id="trace", idempotency_key="preview"
            )
        self.assertEqual(self.store.list_runtime_environment_records(), after)

    def test_preview_removal_retains_exclusive_current_product_ownership(self) -> None:
        body, operator = self.reviewed_preview()
        before = self.store.list_runtime_environment_records()
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(
                _odoo_profile_payload_with_prod_lane("other-product")
            )
        )
        with self.assertRaises(ValueError):
            self.store.runtime_setting_removal(
                request=body, identity=operator, trace_id="trace", idempotency_key="ambiguous-owner"
            )
        self.assertEqual(self.store.list_runtime_environment_records(), before)

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
        replay = self.call(body, "original-key")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.store.list_runtime_environment_records()[0].env, {"KEEP": "drift"})

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
        persist_historical_preview_transport(
            self.store, "site", seed_command="", migration_command=""
        )
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

    def test_result_validation_failure_rolls_back_values_and_completion_receipt(self) -> None:
        body = self.reviewed()
        validate = RuntimeSettingRemovalPlan.model_validate

        def reject_completion(value: object) -> RuntimeSettingRemovalPlan:
            if isinstance(value, dict) and value.get("applied") is True:
                raise ValueError("invalid completion result")
            return validate(value)

        with patch.object(
            RuntimeSettingRemovalPlan, "model_validate", side_effect=reject_completion
        ):
            with self.assertRaises(ValueError):
                self.call(body, "invalid-result")
        self.assertEqual(self.store.list_runtime_environment_records(), (self.before,))
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=idempotency_scope(admin()),
                route_path=ROUTE,
                idempotency_key="invalid-result",
            )
        )

    def test_completed_receipt_remains_denied_after_authority_revocation(self) -> None:
        body = self.reviewed()
        self.call(body, "completed")
        after = self.store.list_runtime_environment_records()
        payload = policy().model_dump(mode="json")
        payload["github_humans"][0]["github_ids"] = [202]
        self.store.compare_and_write_authz_policy_record(
            expected_record=self.active,
            replacement_record=_record(
                policy=LaunchplaneAuthzPolicy.model_validate(payload), revision=2
            ),
        )
        with self.assertRaises(PermissionError):
            self.call(body, "completed")
        self.assertEqual(self.store.list_runtime_environment_records(), after)

    def test_replay_rechecks_authority_after_reservation_collision_rollback(self) -> None:
        body = self.reviewed()
        self.call(body, "completed")
        after = self.store.list_runtime_environment_records()
        begin_write = self.store._begin_serialized_write
        calls = 0

        def revoke_before_replay_lock(session: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                payload = policy().model_dump(mode="json")
                payload["github_humans"][0]["github_ids"] = [202]
                self.store.compare_and_write_authz_policy_record(
                    expected_record=self.active,
                    replacement_record=_record(
                        policy=LaunchplaneAuthzPolicy.model_validate(payload), revision=2
                    ),
                )
            begin_write(session)

        with patch.object(
            self.store, "_begin_serialized_write", side_effect=revoke_before_replay_lock
        ):
            with self.assertRaises(PermissionError):
                self.call(body, "completed")
        self.assertEqual(self.store.list_runtime_environment_records(), after)

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
    async def test_invalid_inventory_row_is_not_reported_as_a_stale_removal_review(self) -> None:
        with _database_app(policy(), director_agent=True) as (store, _, app):
            store.write_runtime_environment_record(runtime())
            with patch.object(
                store,
                "global_setting_consumer_inventory",
                side_effect=ValueError("invalid inventory row"),
            ):
                async with lifespan_client(app) as client:
                    with self.assertRaisesRegex(ValueError, "invalid inventory row"):
                        await client.post(
                            ROUTE,
                            json=request().model_dump(mode="json"),
                            headers={"Authorization": "Bearer reader-token"},
                        )

    async def test_global_review_inventories_orphan_records_only_after_authorization(self) -> None:
        with _database_app(policy(), director_agent=True) as (store, _, app):
            records = (
                runtime(),
                runtime(scope="instance", context="historical", instance="retained"),
            )
            for record in records:
                store.write_runtime_environment_record(record)
            store.request_product_reconcile(
                ProductReconcileTarget(product="unknown-product", target_kind="testing"),
                requested_at=runtime().updated_at,
            )
            with store._session_factory() as session:
                session.add(
                    LaunchplaneProductionRecipientRow(
                        product="retained-product",
                        context="old-context",
                        payload={
                            "phase": "prepared",
                            "authority_snapshot": {"runtime": "private-captured-value"},
                        },
                    )
                )
                session.commit()
            headers = {"Authorization": "Bearer reader-token"}
            async with lifespan_client(app) as client:
                with patch.object(
                    store,
                    "global_setting_consumer_inventory",
                    wraps=store.global_setting_consumer_inventory,
                ) as inventory_read:
                    denied = await client.post(
                        ROUTE,
                        json=request().model_dump(mode="json"),
                        headers={"Authorization": "Bearer admin-token"},
                    )
                    self.assertEqual(denied.status_code, 403)
                    inventory_read.assert_not_called()
                    dry = await client.post(
                        ROUTE, json=request().model_dump(mode="json"), headers=headers
                    )
                    self.assertEqual(dry.status_code, 200, dry.text)
                    inventory_read.assert_called_once()
                inventory = dry.json()["result"]["consumer_inventory"]
                self.assertEqual(inventory["runtime_record_count"], len(records))
                self.assertTrue(any(c["context"] == "historical" for c in inventory["consumers"]))
                self.assertEqual(
                    {c["product"] for c in inventory["authority_records"]},
                    {"unknown-product", "retained-product"},
                )
                self.assertNotIn("private-captured-value", dry.text)
                for record in records:
                    for value in record.env.values():
                        self.assertNotIn(str(value), dry.text)
                self.assertEqual(store.list_runtime_environment_records(), records)

    async def test_director_operator_removes_reviewed_preview_key_and_replays_receipt(self) -> None:
        with _database_app(preview_policy(), director_agent=True) as (store, _, app):
            before, body = preview_removal(store)
            headers = {"Authorization": "Bearer reader-token"}
            async with lifespan_client(app) as client:
                dry = await client.post(ROUTE, json=body.model_dump(mode="json"), headers=headers)
                self.assertEqual(dry.status_code, 200, dry.text)
                plan = dry.json()["result"]
                apply = body.model_copy(
                    update={
                        "mode": "apply",
                        "review_id": plan["review_id"],
                        "reviewed_plan_sha256": plan["plan_sha256"],
                    }
                )
                apply_headers = {**headers, "Idempotency-Key": "preview-removal"}
                applied = await client.post(
                    ROUTE, json=apply.model_dump(mode="json"), headers=apply_headers
                )
                self.assertEqual(applied.status_code, 200, applied.text)
                self.assertTrue(applied.json()["result"]["applied"])
                after = store.list_runtime_environment_records(
                    scope="instance", context_name=before.context, instance_name=before.instance
                )[0]
                self.assertEqual(after.env, {"KEEP": before.env["KEEP"]})
                self.assertEqual(after.source_label, before.source_label)
                replay = await client.post(
                    ROUTE, json=apply.model_dump(mode="json"), headers=apply_headers
                )
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertTrue(replay.json()["result"]["replayed"])
                readback = await client.post(
                    ROUTE,
                    json=apply.model_copy(update={"mode": "readback"}).model_dump(mode="json"),
                    headers=headers,
                )
                self.assertTrue(readback.json()["result"]["readback_matches"])
                operator = LocalOperatorIdentity(
                    subject="record-reader", token_label="record-reader-label", director_agent=True
                )
                self.assertIsNotNone(
                    store.read_idempotency_record(
                        scope=idempotency_scope(operator),
                        route_path=ROUTE,
                        idempotency_key=apply_headers["Idempotency-Key"],
                    )
                )
                for response in (applied, replay, readback):
                    for value in before.env.values():
                        self.assertNotIn(str(value), response.text)

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
                readback = await client.post(
                    ROUTE,
                    json=body.model_copy(update={"mode": "readback"}).model_dump(mode="json"),
                    headers=_browser_headers(sessions, admin())[0],
                )
                self.assertEqual(readback.status_code, 200, readback.text)
                self.assertTrue(readback.json()["result"]["readback_matches"])

    async def test_stale_apply_reports_rolled_back_attempt_and_allows_fresh_review(self) -> None:
        sessions, _ = _human_session_manager()
        with _database_app(policy(), human_session_manager=sessions) as (store, _, app):
            store.write_runtime_environment_record(runtime())
            async with lifespan_client(app) as client:
                dry = await client.post(
                    ROUTE,
                    json=request().model_dump(mode="json"),
                    headers=_browser_headers(sessions, admin())[0],
                )
                plan = dry.json()["result"]
                changed = runtime(env={"KEEP": "changed-survivor"})
                store.write_runtime_environment_record(changed)
                body = request(
                    mode="apply",
                    review_id=plan["review_id"],
                    reviewed_plan_sha256=plan["plan_sha256"],
                )
                rejected = await client.post(
                    ROUTE,
                    json=body.model_dump(mode="json"),
                    headers={
                        **_browser_headers(sessions, admin())[0],
                        "Idempotency-Key": "stale-attempt",
                    },
                )
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertEqual(rejected.json()["error"]["code"], "runtime_setting_removal_stale")
                self.assertEqual(store.list_runtime_environment_records(), (changed,))
                self.assertIsNone(
                    store.read_idempotency_record(
                        scope=idempotency_scope(admin()),
                        route_path=ROUTE,
                        idempotency_key="stale-attempt",
                    )
                )
                fresh = await client.post(
                    ROUTE,
                    json=request(keys=["KEEP"]).model_dump(mode="json"),
                    headers=_browser_headers(sessions, admin())[0],
                )
                self.assertEqual(fresh.status_code, 200, fresh.text)
                self.assertFalse(fresh.json()["result"]["applied"])


class RuntimeRemovalPostgresScenarios:
    """Invoked by the PostgreSQL integration gate, using its isolated database."""

    @staticmethod
    def simultaneous_keys(store: PostgresRecordStore) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        gate = Barrier(2)
        dry = store.runtime_setting_removal(
            request=request(), identity=admin(), trace_id="dry"
        ).model_dump(mode="json", exclude_none=True)
        body = request(
            mode="apply", review_id=dry["review_id"], reviewed_plan_sha256=dry["plan_sha256"]
        )

        def apply() -> dict[str, object]:
            gate.wait(timeout=30)
            return store.runtime_setting_removal(
                request=body, identity=admin(), trace_id="concurrent", idempotency_key="shared-key"
            ).model_dump(mode="json", exclude_none=True)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: apply(), range(2)))
        assert sum(bool(result.get("replayed")) for result in results) == 1
        assert store.list_runtime_environment_records()[0].env == {"KEEP": "surviving-value"}
