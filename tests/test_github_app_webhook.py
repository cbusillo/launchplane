import hashlib
import hmac
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.github_app_webhook import (
    GitHubAppWebhookDependencies,
    handle_github_app_webhook_request,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import (
    LaunchplaneProductReconcileRequestRow,
    LaunchplaneGitHubAppWebhookDeliveryRow,
    PostgresRecordStore,
)
from tests.support.auth import StubVerifier, identity
from tests.support.http import lifespan_client
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url

_SECRET = "app-webhook-secret"
_REPOSITORY_ID = 424242
_OWNER_ID = "1"
_REPOSITORY = "example/site"
_BUILD_PATH = ".github/workflows/build.yml"


def _signature(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _profile(
    product: str,
    *,
    repository: str = _REPOSITORY,
    stored_repository_id: int | None = None,
    lifecycle_state: str = "active",
) -> LaunchplaneProductProfileRecord:
    """A profile that, by default, stores no repository ids: the inventory is the authority."""
    payload = product_profile_payload(product)
    payload["repository"] = repository
    if lifecycle_state != "active":
        payload["lifecycle_state"] = lifecycle_state
        payload["preview"] = {"enabled": False}
    if stored_repository_id is not None:
        payload["repository_id"] = str(stored_repository_id)
        payload["repository_owner_id"] = _OWNER_ID
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _inventory(
    *,
    repository_id: int = _REPOSITORY_ID,
    repository: str = _REPOSITORY,
    inventory_state: str = "tracked",
    inventory_revision: int = 1,
) -> RepositoryInventoryRecord:
    return RepositoryInventoryRecord.model_validate(
        {
            "repository_id": str(repository_id),
            "repository_owner_id": _OWNER_ID,
            "repository": repository,
            "inventory_state": inventory_state,
            "inventory_revision": inventory_revision,
            "recorded_at": "2026-09-29T11:00:00Z",
            "source": "test",
            "reason": "Track the site repository.",
            "supersedes_record_id": (
                None
                if inventory_revision == 1
                else f"repository-inventory-{repository_id}-r{inventory_revision - 1}"
            ),
        }
    )


def _workflow_run(
    *, trigger: str, path: str = _BUILD_PATH, pull_requests: tuple[int, ...] = ()
) -> dict[str, object]:
    return {
        "action": "completed",
        "repository": {"id": _REPOSITORY_ID},
        "workflow_run": {
            "path": path,
            "event": trigger,
            "pull_requests": [{"number": number} for number in pull_requests],
        },
    }


def _pull_request(action: str, number: int = 7) -> dict[str, object]:
    return {"action": action, "number": number, "repository": {"id": _REPOSITORY_ID}}


class GitHubAppWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory.name) / "lp.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_repository_inventory_record(_inventory())
        self.store.write_product_profile_record(_profile("site"))
        self.secret = _SECRET
        self.clock = "2026-09-29T12:00:00Z"

    def deliver(
        self,
        payload: dict[str, object],
        *,
        event: str,
        delivery_id: str = "delivery-1",
        signature: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode()
        return handle_github_app_webhook_request(
            body,
            event,
            delivery_id,
            _signature(body) if signature is None else signature,
            self.store,
            Path("."),
            "trace-test",
            dependencies=GitHubAppWebhookDependencies(
                webhook_secret=lambda: self.secret, now=lambda: self.clock
            ),
        )

    def assert_nothing_recorded(self) -> None:
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    def test_bad_signature_records_nothing(self) -> None:
        status, _ = self.deliver(
            _workflow_run(trigger="push"), event="workflow_run", signature="sha256=" + "0" * 64
        )

        self.assertEqual(status, 401)
        self.assert_nothing_recorded()

    def test_missing_secret_fails_closed(self) -> None:
        self.secret = ""

        status, body = self.deliver(_workflow_run(trigger="push"), event="workflow_run")

        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "github_app_webhook_unconfigured")  # type: ignore[index]
        self.assert_nothing_recorded()

    def test_push_build_requests_testing_reconcile(self) -> None:
        status, body = self.deliver(_workflow_run(trigger="push"), event="workflow_run")

        self.assertEqual(status, 202)
        self.assertEqual(
            body["result"], {"status": "recorded", "reason": "", "target_keys": ["site:testing"]}
        )
        request = self.store.read_product_reconcile_request("site:testing")
        self.assertEqual((request.state, request.request_count), ("pending", 1))
        self.assertEqual(request.last_delivery_id, "delivery-1")

    def test_pull_request_build_and_events_request_preview_reconcile(self) -> None:
        self.deliver(
            _workflow_run(trigger="pull_request", pull_requests=(7, 8)),
            event="workflow_run",
            delivery_id="build",
        )
        self.deliver(_pull_request("labeled"), event="pull_request", delivery_id="label")
        self.deliver(_pull_request("closed", number=9), event="pull_request", delivery_id="close")
        # Draft state doesn't change a preview, so its events request nothing.
        self.deliver(
            _pull_request("converted_to_draft", number=11),
            event="pull_request",
            delivery_id="draft",
        )
        # A retarget re-checks a carried acceptance; a title edit changes nothing.
        self.deliver(
            {**_pull_request("edited", number=12), "changes": {"base": {"ref": {"from": "a"}}}},
            event="pull_request",
            delivery_id="retarget",
        )
        self.deliver(
            {**_pull_request("edited", number=13), "changes": {"title": {"from": "x"}}},
            event="pull_request",
            delivery_id="title",
        )

        requests = {r.target_key: r for r in self.store.list_product_reconcile_requests()}
        self.assertEqual(set(requests), {f"site:preview:{number}" for number in (7, 8, 9, 12)})
        self.assertEqual(requests["site:preview:7"].request_count, 2)
        self.assertEqual(requests["site:preview:7"].pull_request_number, 7)

    def test_duplicate_delivery_changes_nothing(self) -> None:
        self.deliver(_workflow_run(trigger="push"), event="workflow_run")

        status, body = self.deliver(_workflow_run(trigger="push"), event="workflow_run")

        self.assertEqual(status, 202)
        self.assertEqual(body["result"]["status"], "duplicate")  # type: ignore[index]
        self.assertEqual(self.store.read_product_reconcile_request("site:testing").request_count, 1)

    def test_irrelevant_deliveries_are_accepted_and_ignored(self) -> None:
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("workflow_run", _workflow_run(trigger="push", path=".github/workflows/ci.yml")),
            ("workflow_run", _workflow_run(trigger="pull_request")),
            ("workflow_run", {**_workflow_run(trigger="push"), "action": "requested"}),
            ("pull_request", _pull_request("edited")),
            ("issues", _pull_request("opened")),
            ("ping", {"zen": "hi", "repository": {"id": _REPOSITORY_ID}}),
        )
        for index, (event, payload) in enumerate(cases):
            with self.subTest(event=event, index=index):
                status, body = self.deliver(payload, event=event, delivery_id=f"d-{index}")
                self.assertEqual(status, 202)
                self.assertEqual(body["result"]["status"], "ignored")  # type: ignore[index]
        self.assert_nothing_recorded()

    def assert_ignored(self, reason: str, *, delivery_id: str = "delivery-1") -> None:
        status, body = self.deliver(
            _pull_request("opened"), event="pull_request", delivery_id=delivery_id
        )
        self.assertEqual(
            (status, body["result"]["status"], body["result"]["reason"]),  # type: ignore[index]
            (202, "ignored", reason),
        )
        self.assert_nothing_recorded()

    def test_repository_is_mapped_through_the_inventory_by_case_insensitive_name(self) -> None:
        self.store.write_product_profile_record(_profile("site", repository="Example/Site"))

        status, body = self.deliver(_pull_request("opened"), event="pull_request")

        self.assertEqual((status, body["result"]["status"]), (202, "recorded"))  # type: ignore[index]
        self.assertEqual(
            self.store.read_product_reconcile_request("site:preview:7").product, "site"
        )

    def test_repository_missing_from_inventory_is_ignored(self) -> None:
        unknown = {**_pull_request("opened"), "repository": {"id": 1}}
        status, body = self.deliver(unknown, event="pull_request")

        self.assertEqual(
            (status, body["result"]["status"], body["result"]["reason"]),  # type: ignore[index]
            (202, "ignored", "repository_not_mapped"),
        )
        self.assert_nothing_recorded()

    def test_profile_ids_without_inventory_record_are_not_mapped(self) -> None:
        # Stored ids alone never map an event: the inventory is the authority.
        self.store.write_product_profile_record(
            _profile("other", repository="example/other", stored_repository_id=515151)
        )
        unknown = {**_pull_request("opened"), "repository": {"id": 515151}}

        status, body = self.deliver(unknown, event="pull_request")

        self.assertEqual(body["result"]["reason"], "repository_not_mapped")  # type: ignore[index]
        self.assert_nothing_recorded()

    def test_retired_inventory_repository_is_ignored(self) -> None:
        self.store.write_repository_inventory_record(
            _inventory(inventory_state="retired", inventory_revision=2)
        )

        self.assert_ignored("repository_not_mapped")

    def test_repository_naming_no_active_profile_is_ignored(self) -> None:
        self.store.write_product_profile_record(_profile("site", repository="example/elsewhere"))
        self.store.write_product_profile_record(_profile("retired-site", lifecycle_state="retired"))

        self.assert_ignored("repository_not_mapped")

    def test_repository_naming_two_active_profiles_is_ignored(self) -> None:
        self.store.write_product_profile_record(_profile("site-copy"))

        self.assert_ignored("repository_not_mapped")

    def test_stored_ids_that_disagree_with_inventory_fail_closed(self) -> None:
        self.store.write_product_profile_record(_profile("site", stored_repository_id=999))

        self.assert_ignored("repository_identity_mismatch")

    def test_stored_ids_that_agree_with_inventory_are_mapped(self) -> None:
        self.store.write_product_profile_record(
            _profile("site", stored_repository_id=_REPOSITORY_ID)
        )

        status, body = self.deliver(_pull_request("opened"), event="pull_request")

        self.assertEqual(body["result"]["status"], "recorded")  # type: ignore[index]

    def test_request_error_preserves_preview_and_bad_signature_never_selects_scan(self) -> None:
        payload = {
            "repository": {"id": _REPOSITORY_ID},
            "action": "synchronize",
            "number": 7,
            "pull_request": {"base": {"ref": "main"}, "head": {"sha": "b" * 40}},
        }
        body = json.dumps(payload).encode()
        select_scan = Mock(side_effect=ValueError("private request error"))
        dependencies = GitHubAppWebhookDependencies(
            webhook_secret=lambda: self.secret, config_authority=select_scan
        )
        status, _ = handle_github_app_webhook_request(
            body,
            "pull_request",
            "config-2",
            "bad",
            self.store,
            Path("."),
            "trace",
            dependencies=dependencies,
        )
        self.assertEqual(status, 401)
        select_scan.assert_not_called()
        status, _ = handle_github_app_webhook_request(
            body,
            "pull_request",
            "config-2",
            _signature(body),
            self.store,
            Path("."),
            "trace",
            dependencies=dependencies,
        )
        self.assertEqual(status, 202)
        self.assertEqual(
            self.store.read_product_reconcile_request("site:preview:7").state, "pending"
        )
        delivery = self.store.read_github_app_webhook_delivery("config-2")
        self.assertEqual(delivery.config_authority_state, "pending")
        self.assertEqual(delivery.config_authority["request_error"], "source_request_unavailable")
        self.assertNotIn("private request error", delivery.model_dump_json())

    def test_pending_scan_is_durable_replayed_and_worker_refuses_unavailable_source(self) -> None:
        from control_plane.product_config_authority_events import run_product_config_authority_once

        body = json.dumps(
            {"repository": {"id": _REPOSITORY_ID}, "before": "a" * 40, "after": "b" * 40}
        ).encode()
        dependencies = GitHubAppWebhookDependencies(
            webhook_secret=lambda: self.secret,
            config_authority=lambda *_args: {
                "status": "pending",
                "request": {"before": "a" * 40, "after": "b" * 40},
            },
        )
        for _ in range(2):
            status, _response = handle_github_app_webhook_request(
                body,
                "push",
                "scan-durable",
                _signature(body),
                self.store,
                Path("."),
                "trace",
                dependencies=dependencies,
            )
            self.assertEqual(status, 202)
        pending = self.store.read_github_app_webhook_delivery("scan-durable")
        self.assertEqual(pending.config_authority_state, "pending")
        self.assertEqual(pending.config_authority_attempt, 0)
        scan = Mock(side_effect=OSError("private provider error"))
        pending_states = []

        def publish(_inventory: object, evidence: dict[str, object], _root: Path) -> dict[str, str]:
            pending_states.append(evidence["retry_pending"])
            self.assertEqual(evidence["head_sha"], "b" * 40)
            return {"status": "projected"}

        completed = run_product_config_authority_once(
            self.store, "worker", scan=scan, publish=publish
        )
        assert completed is not None
        self.assertEqual(completed.config_authority_state, "pending")
        self.assertEqual(completed.config_authority["status"], "unavailable")
        self.assertNotIn("private provider error", completed.model_dump_json())
        self.assertIsNone(run_product_config_authority_once(self.store, "worker", scan=scan))
        for _ in range(2):
            with self.store._session_factory() as session:
                row = session.get(LaunchplaneGitHubAppWebhookDeliveryRow, "scan-durable")
                assert row is not None
                row.payload = {**row.payload, "config_authority_next_attempt_at": ""}
                session.commit()
            completed = run_product_config_authority_once(
                self.store, "worker", scan=scan, publish=publish
            )
        assert completed is not None
        self.assertEqual(completed.config_authority_state, "failed")
        self.assertEqual(pending_states, [True, True, False])
        self.assertIsNone(run_product_config_authority_once(self.store, "worker", scan=scan))
        # Native signed redelivery resets only the failed unavailable scan, preserving deploy dedupe.
        status, _ = handle_github_app_webhook_request(
            body,
            "push",
            "scan-durable",
            _signature(body),
            self.store,
            Path("."),
            "trace",
            dependencies=dependencies,
        )
        self.assertEqual(status, 202)
        repaired = run_product_config_authority_once(
            self.store,
            "worker",
            scan=lambda *_args: {"status": "pass", "head_sha": "b" * 40},
            publish=lambda *_args: {"status": "projected"},
        )
        assert repaired is not None
        self.assertEqual(repaired.config_authority_state, "done")
        self.assertEqual(repaired.config_authority["status"], "pass")
        self.assert_nothing_recorded()

    def test_deploy_only_inventory_error_preserves_webhook_unavailable_contract(self) -> None:
        body = json.dumps(_workflow_run(trigger="push")).encode()
        with patch.object(
            self.store,
            "list_repository_inventory_records",
            side_effect=OSError("private storage detail"),
        ):
            code, response = handle_github_app_webhook_request(
                body,
                "workflow_run",
                "inventory-failed",
                _signature(body),
                self.store,
                Path("."),
                "trace",
                dependencies=GitHubAppWebhookDependencies(webhook_secret=lambda: self.secret),
            )
        self.assertEqual(code, 503)
        self.assertNotIn("private storage detail", json.dumps(response))
        self.assertIn("github_app_webhook_unavailable", json.dumps(response))

    def test_busy_repository_backlog_does_not_starve_another_repository(self) -> None:
        from control_plane.contracts.product_reconcile import GitHubAppWebhookDeliveryRecord

        busy = GitHubAppWebhookDeliveryRecord(
            delivery_id="busy-00",
            event="push",
            repository_id=str(_REPOSITORY_ID),
            received_at=self.clock,
            config_authority_state="pending",
        )
        for index in range(25):
            delivery = busy.model_copy(update={"delivery_id": f"busy-{index:02}"})
            self.store.record_github_app_webhook_delivery(delivery, (), self.clock)
        self.assertIsNotNone(self.store.claim_next_config_authority_delivery("busy", 600))
        other = busy.model_copy(update={"delivery_id": "other", "repository_id": "99999"})
        self.store.record_github_app_webhook_delivery(other, (), self.clock)
        claim = self.store.claim_next_config_authority_delivery("free", 600)
        assert claim is not None
        self.assertEqual(claim.delivery_id, "other")

    def test_config_scan_lease_recovery_fences_old_attempt_even_with_same_worker(self) -> None:
        from control_plane.contracts.product_reconcile import (
            GitHubAppWebhookDeliveryRecord,
            ProductReconcileLeaseLostError,
        )

        delivery = GitHubAppWebhookDeliveryRecord(
            delivery_id="scan-leased",
            event="push",
            repository_id=str(_REPOSITORY_ID),
            received_at=self.clock,
            config_authority_state="pending",
            config_authority_request={"before": "a" * 40, "after": "b" * 40},
        )
        self.store.record_github_app_webhook_delivery(delivery, (), self.clock)
        followup = delivery.model_copy(update={"delivery_id": "scan-leased-second"})
        self.store.record_github_app_webhook_delivery(followup, (), self.clock)
        old = self.store.claim_next_config_authority_delivery("same-worker", 60)
        assert old is not None
        self.assertIsNone(self.store.claim_next_config_authority_delivery("other", 60))
        # Expire the actual persisted lease instead of sleeping or assuming clock offsets.
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneGitHubAppWebhookDeliveryRow, delivery.delivery_id)
            assert row is not None
            row.payload = {
                **row.payload,
                "config_authority_lease_expires_at": "2000-01-01T00:00:00Z",
            }
            session.commit()
        recovered = self.store.claim_next_config_authority_delivery("same-worker", 60)
        assert recovered is not None
        publisher = Mock()
        with self.assertRaises(ProductReconcileLeaseLostError):
            self.store.complete_config_authority_delivery(
                old, {"status": "pass"}, publish=publisher
            )
        publisher.assert_not_called()
        completed = self.store.complete_config_authority_delivery(recovered, {"status": "fail"})
        self.assertEqual(completed.config_authority_state, "failed")
        self.assertEqual(
            self.store.read_github_app_webhook_delivery(delivery.delivery_id), completed
        )
        next_claim = self.store.claim_next_config_authority_delivery("other", 60)
        assert next_claim is not None
        self.assertEqual(next_claim.delivery_id, followup.delivery_id)


class ProductReconcileFoldingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory.name) / "lp.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.target = ProductReconcileTarget(product="site", target_kind="testing")

    def set_state(self, state: str, *, last_error: str = "") -> None:
        # Stand-in for the future worker, which owns these transitions.
        current = self.store.read_product_reconcile_request(self.target.target_key)
        record = current.model_copy(update={"state": state, "last_error": last_error, "attempt": 2})
        self.store._write_row(
            LaunchplaneProductReconcileRequestRow(
                target_key=record.target_key,
                product=record.product,
                target_kind=record.target_kind,
                state=record.state,
                requested_at=record.requested_at,
                updated_at=record.updated_at,
                payload=record.model_dump(mode="json", exclude_none=True),
            )
        )

    def test_pending_request_folds_into_one(self) -> None:
        self.store.request_product_reconcile(self.target, "2026-09-29T12:00:00Z")
        folded = self.store.request_product_reconcile(self.target, "2026-09-29T12:05:00Z")

        self.assertEqual((folded.state, folded.request_count), ("pending", 2))
        self.assertEqual(folded.requested_at, "2026-09-29T12:00:00Z")
        self.assertEqual(len(self.store.list_product_reconcile_requests(state="pending")), 1)

    def test_request_while_running_is_remembered(self) -> None:
        self.store.request_product_reconcile(self.target, "2026-09-29T12:00:00Z")
        self.set_state("running")

        folded = self.store.request_product_reconcile(self.target, "2026-09-29T12:05:00Z")

        self.assertEqual(folded.state, "running")
        self.assertTrue(folded.rerequested_while_running)

    def test_finished_request_becomes_pending_again(self) -> None:
        self.store.request_product_reconcile(self.target, "2026-09-29T12:00:00Z")
        self.set_state("failed", last_error="deploy failed")
        after_failure = self.store.request_product_reconcile(self.target, "2026-09-29T12:05:00Z")
        self.assertEqual(
            (after_failure.state, after_failure.last_error), ("pending", "deploy failed")
        )

        self.set_state("done", last_error="stale")
        after_done = self.store.request_product_reconcile(self.target, "2026-09-29T12:10:00Z")
        self.assertEqual((after_done.state, after_done.last_error), ("pending", ""))
        self.assertEqual(after_done.attempt, 2)
        self.assertEqual(after_done.requested_at, "2026-09-29T12:10:00Z")


class GitHubAppWebhookRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_verifies_with_managed_secret_and_records_request(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = PostgresRecordStore(
                database_url=sqlite_database_url(Path(temporary_directory) / "lp.sqlite3")
            )
            self.addCleanup(store.close)
            store.ensure_schema()
            store.write_repository_inventory_record(_inventory())
            store.write_product_profile_record(_profile("site"))
            app = create_launchplane_fastapi_app(
                verifier=StubVerifier(identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                github_app_webhook_handler=handle_github_app_webhook_request,
            )
            body = json.dumps(_workflow_run(trigger="push")).encode()

            def managed_secret(*, integration: str, context_name: str, binding_key: str) -> str:
                self.assertEqual(
                    (integration, context_name, binding_key),
                    ("github_app_webhook", "launchplane", "webhook_secret"),
                )
                return _SECRET

            with patch(
                "control_plane.github_app_webhook.secrets.resolve_context_secret_value",
                side_effect=managed_secret,
            ):
                async with lifespan_client(app) as client:
                    response = await client.post(
                        "/v1/github/app-webhook",
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-GitHub-Event": "workflow_run",
                            "X-GitHub-Delivery": "delivery-route",
                            "X-Hub-Signature-256": _signature(body),
                        },
                    )

            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(store.read_product_reconcile_request("site:testing").state, "pending")


if __name__ == "__main__":
    unittest.main()
