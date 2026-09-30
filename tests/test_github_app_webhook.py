import hashlib
import hmac
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

        requests = {r.target_key: r for r in self.store.list_product_reconcile_requests()}
        self.assertEqual(set(requests), {"site:preview:7", "site:preview:8", "site:preview:9"})
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
