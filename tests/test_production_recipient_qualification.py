"""Production connection purpose, isolated recovery, custody and refusal behavior."""

from copy import deepcopy
from dataclasses import replace
import json
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.production_recipient import RecipientUpstream, RecipientRecoveryRequest
from control_plane.contracts.secret_record import (
    SecretBinding,
    SecretRecord,
    SecretSharingReason,
    SecretVersion,
)
from control_plane.production_recipient import custody, qualified_render
from control_plane.production_recipient_recovery import RECOVERY_ROUTE, RecipientRecoveryAdapter
from control_plane.production_recipient_upstreams import upstream_connections, upstream_binding
from control_plane.provider_operations import run_durable_provider_operation
from tests import test_production_recipient as preparation_tests


class RecipientUpstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = preparation_tests.ProductionRecipientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.values = {
            **self.fixture.values,
            "SOURCE_HOST": "upstream.invalid",
            "SOURCE_PORT": "3306",
            "SOURCE_USER": "reader",
            "SOURCE_DB": "source",
            "SOURCE_PASSWORD": "private-read-only-fixture",
        }
        self.record = self.fixture.adapter.current()
        self.values.update(
            {
                "DB_VOLUME": self.record.volumes["database"],
                "CONFIG_VOLUME": self.record.volumes["config"],
                "DOCKER_IMAGE_REFERENCE": self.record.image_reference,
                **{key: str(value) for key, value in self.record.request.ports.items()},
            }
        )
        stamp = self.fixture.old.updated_at
        self.binding = SecretBinding(
            binding_id="upstream-binding",
            secret_id="upstream-secret",
            integration="runtime_environment",
            binding_key="SOURCE_PASSWORD",
            context=self.fixture.context,
            instance="prod",
            declared_secret_class="shared_safe",
            sharing_reason=SecretSharingReason(
                kind="read_only_source",
                reason="Source only",
                evidence="Existing independently observed read-only grant",
            ),
            created_at=stamp,
            updated_at=stamp,
        )
        self.secret = SecretRecord(
            secret_id=self.binding.secret_id,
            scope="context_instance",
            integration="runtime_environment",
            name="source",
            context=self.fixture.context,
            instance="prod",
            current_version_id="source-v1",
            created_at=stamp,
            updated_at=stamp,
        )
        self.upstream = RecipientUpstream(
            service="web",
            binding_id=self.binding.binding_id,
            version_id=self.secret.current_version_id,
            keys={
                "host": "SOURCE_HOST",
                "port": "SOURCE_PORT",
                "user": "SOURCE_USER",
                "database": "SOURCE_DB",
                "password": "SOURCE_PASSWORD",
            },
        )
        self.record.request.upstreams = (self.upstream,)
        self.record.authority_snapshot["bindings"] = [self.binding.model_dump(mode="json")]
        self.record.authority_snapshot["secrets"] = [self.secret.model_dump(mode="json")]
        document = json.loads(self.record.compose_source)
        document["services"]["web"]["environment"].update(
            {key: "${" + key + "}" for key in self.upstream.keys.values()}
        )
        self.record.compose_source = json.dumps(document)

    def test_rendered_upstream_matches_purpose_endpoint_and_managed_version(self) -> None:
        document = qualified_render(self.record, self.values)
        connection = upstream_connections(self.record, document, self.values)[0]
        self.assertEqual(connection["version_id"], self.secret.current_version_id)
        self.assertEqual(connection["settings"]["host"], self.values["SOURCE_HOST"])
        self.assertEqual(connection["settings"]["password"], self.values["SOURCE_PASSWORD"])
        self.assertNotIn(self.values["SOURCE_PASSWORD"], upstream_binding(self.record))

    def test_wrong_scope_version_purpose_or_binding_refuses(self) -> None:
        for field, value in (
            ("instance", "testing"),
            ("current_version_id", "rotated"),
            ("scope", "context"),
        ):
            with self.subTest(field=field):
                record = self.record.model_copy(deep=True)
                secret = deepcopy(record.authority_snapshot["secrets"][0])
                secret[field] = value
                if field == "scope":
                    secret["instance"] = ""
                record.authority_snapshot["secrets"] = [secret]
                with self.assertRaises(ValueError):
                    qualified_render(record, self.values)
        for binding_field, binding_value in (
            ("declared_secret_class", "production"),
            ("binding_id", "wrong"),
            ("sharing_reason", None),
        ):
            record = self.record.model_copy(deep=True)
            record.authority_snapshot["bindings"][0][binding_field] = binding_value
            with self.assertRaises(ValueError):
                qualified_render(record, self.values)

    def test_local_writer_urls_and_undeclared_external_connections_refuse(self) -> None:
        for change in (
            {"DATABASE_URL": "mysql://writer:secret@old-production.invalid/db"},
            {"OTHER_HOST": "other-live.invalid"},
        ):
            record = self.record.model_copy(deep=True)
            document = json.loads(record.compose_source)
            document["services"]["web"]["environment"].update(change)
            record.compose_source = json.dumps(document)
            with self.assertRaises(ValueError):
                qualified_render(record, self.values)
        with self.assertRaises(ValueError):
            qualified_render(
                self.record, {**self.values, "SOURCE_PASSWORD": self.values["DB_PASSWORD"]}
            )
        record = self.record.model_copy(deep=True)
        document = json.loads(record.compose_source)
        document["services"]["web"]["environment"]["SOURCE_HOST"] = "substituted.invalid"
        record.compose_source = json.dumps(document)
        with self.assertRaises(ValueError):
            qualified_render(record, self.values)

    def test_service_runtime_rejects_missing_delivery_and_rotated_version(self) -> None:
        from control_plane.production_recipient_upstreams import require_delivered_upstreams

        with patch(
            "control_plane.production_recipient_upstreams.site_delivered_secret_bindings_from_store",
            return_value={},
        ):
            with self.assertRaises(ValueError):
                require_delivered_upstreams(self.fixture.store, self.record)

        self.fixture.store.write_secret_record(self.secret)
        self.fixture.store.write_secret_version(
            SecretVersion(
                version_id="old-v0",
                secret_id=self.secret.secret_id,
                created_at=self.secret.created_at,
                ciphertext="opaque-fixture",
            )
        )
        self.record.request.upstreams = (self.upstream.model_copy(update={"version_id": "old-v0"}),)
        with patch(
            "control_plane.production_recipient_upstreams.site_delivered_secret_bindings_from_store",
            return_value={self.binding.binding_key: self.binding},
        ):
            with self.assertRaises(ValueError):
                require_delivered_upstreams(self.fixture.store, self.record)

    def test_a_second_secret_cannot_be_disclosed_as_an_upstream_hostname(self) -> None:
        record = self.record.model_copy(deep=True)
        record.authority_snapshot["bindings"].append(
            self.binding.model_copy(
                update={"binding_id": "another-secret", "binding_key": "SOURCE_HOST"}
            ).model_dump(mode="json")
        )
        with self.assertRaises(ValueError):
            qualified_render(record, self.values)


class RecipientRecoveryTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = preparation_tests.ProductionRecipientTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.run_operation()
        record = custody(self.fixture.store, self.fixture.product, self.fixture.context)
        assert record is not None
        self.record = record
        self.request = RecipientRecoveryRequest(
            context=self.fixture.context,
            preparation_plan_sha256=self.record.plan_sha256,
            reason="Prove exact isolated recovery",
        )
        self.adapter = RecipientRecoveryAdapter(
            self.fixture.root,
            self.fixture.store,
            self.fixture.product,
            self.request,
            self.fixture.adapter.actor,
            self.fixture.adapter.scope,
            "launchplane_req_recovery",
            "recovery-key",
        )
        _, plan = self.adapter.current()
        self.adapter.request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        )
        self.calls: list[dict[str, Any]] = []

    def host(
        self, root: Any, record: Any, spec: Any, program: Any, checkpoint: Any
    ) -> dict[str, str]:
        self.calls.append(spec)
        checkpoint("isolated_capture_recovery")
        return {
            "recipient_recovery_ok": "true",
            "recipient_recovery_binding": spec["recovery_binding"],
            "recipient_recovery_capture": "f" * 64,
        }

    def run_recovery(self, adapter: Any = None, store: Any = None) -> Any:
        selected = adapter or self.adapter
        return run_durable_provider_operation(
            store=store or self.fixture.store,
            scope=selected.scope,
            route_path=RECOVERY_ROUTE.format(product=self.fixture.product),
            idempotency_key=selected.original_key,
            request_fingerprint="d" * 64,
            lease_owner="recovery-fixture",
            response_trace_id=selected.trace_id,
            adapter=selected,
        )

    def test_completed_recovery_replays_without_repeating_effect_and_keeps_production(self) -> None:
        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program",
            side_effect=self.host,
        ):
            first = self.run_recovery()
            self.assertEqual(first.response_status_code, 202)
            self.assertTrue(first.response_payload["result"]["recovered"])
            self.assertFalse(first.response_payload["result"]["bootstrap_eligible"])
            self.assertEqual(self.run_recovery().response_payload, first.response_payload)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(
                self.adapter.observe("", "", self.adapter.reconciliation_key()).outcome, "present"
            )
        self.fixture.assert_old_preserved()
        other = replace(self.adapter, original_key="different-key")
        with self.assertRaises(ValueError):
            other.current()

    def test_capture_only_wrong_binding_or_uncertain_recovery_never_qualifies(self) -> None:
        for evidence in (
            {"recipient_recovery_capture": "f" * 64},
            {"recipient_recovery_ok": "true", "recipient_recovery_binding": "wrong"},
        ):
            with self.subTest(evidence=evidence):

                def host(root: Any, record: Any, spec: Any, program: Any, checkpoint: Any) -> Any:
                    checkpoint("capture_started")
                    return evidence

                with patch(
                    "control_plane.production_recipient_recovery.run_recipient_program",
                    side_effect=host,
                ):
                    result = self.run_recovery()
                    self.assertEqual(result.status, "reconcile_required")
                    record = custody(self.fixture.store, self.fixture.product, self.fixture.context)
                    assert record is not None
                    self.assertFalse(record.recovery_proof)
                    self.assertEqual(
                        self.adapter.observe("", "", self.adapter.reconciliation_key()).outcome,
                        "unknown",
                    )
        self.fixture.assert_old_preserved()

    def test_wrong_artifact_recipient_destination_and_stale_authority_refuse(self) -> None:
        with self.assertRaises(ValueError):
            replace(
                self.adapter,
                request=self.request.model_copy(update={"preparation_plan_sha256": "e" * 64}),
            ).current()
        for field, value in (
            ("serverId", "another-destination"),
            ("composeId", "another-recipient"),
            ("composeFile", "{}"),
        ):
            old = self.fixture.provider[self.record.target_id][field]
            self.fixture.provider[self.record.target_id][field] = value
            with self.assertRaises(ValueError):
                self.adapter.current()
            self.fixture.provider[self.record.target_id][field] = old
        changed = self.fixture.old.model_copy(update={"updated_at": "changed-authority"})
        self.fixture.store.write_dokploy_target_record(changed)
        with self.assertRaises(ValueError):
            self.adapter.current()

    def test_parallel_recovery_is_fenced_before_dispatch(self) -> None:
        second = type(self.fixture.store)(database_url=self.fixture.store.database_url)
        self.addCleanup(second.close)
        other = replace(self.adapter, store=second, original_key="competing-recovery")
        outcomes = []

        def host(*args: Any) -> Any:
            outcomes.append(self.run_recovery(other, second))
            return self.host(*args)

        with patch(
            "control_plane.production_recipient_recovery.run_recipient_program", side_effect=host
        ):
            self.assertEqual(self.run_recovery().response_status_code, 202)
        self.assertEqual(outcomes[0].status, "target_busy")
        self.assertEqual(len(self.calls), 1)
