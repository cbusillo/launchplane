import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.product_repository_identity import ProductRepositoryIdentityApplyRequest
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_request, _AsgiResponse
from tests.support.auth import StubVerifier, identity
from tests.support.profiles import product_profile_payload
from tests.support.stores import _sqlite_database_url

_ROUTE = "/v1/product-profiles/repository-identity/apply"
_PRODUCT = "sellyouroutboard"
_REPOSITORY = "cbusillo/sellyouroutboard"


def _operator_identity() -> GitHubActionsIdentity:
    return identity(
        workflow_ref="every/verireel/.github/workflows/product-repository-identity.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _write_policy(*, product: str = _PRODUCT) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/"
                        "product-repository-identity.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": [product],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.write"],
                }
            ]
        }
    )


def _profile(
    *,
    product: str = _PRODUCT,
    repository: str = "",
    repository_id: str = "",
    repository_owner_id: str = "",
) -> LaunchplaneProductProfileRecord:
    payload = product_profile_payload(product)
    if repository:
        payload["repository"] = repository
    payload["repository_id"] = repository_id
    payload["repository_owner_id"] = repository_owner_id
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _inventory(
    *,
    repository_id: str = "7001",
    repository_owner_id: str = "8001",
    repository: str = _REPOSITORY,
    inventory_state: str = "tracked",
    inventory_revision: int = 1,
    supersedes_record_id: str | None = None,
) -> RepositoryInventoryRecord:
    return RepositoryInventoryRecord.model_validate(
        {
            "repository_id": repository_id,
            "repository_owner_id": repository_owner_id,
            "repository": repository,
            "inventory_state": inventory_state,
            "inventory_revision": inventory_revision,
            "recorded_at": "2026-09-01T00:00:00Z",
            "source": "test:repository-inventory",
            "reason": "Track the product repository.",
            "supersedes_record_id": supersedes_record_id,
        }
    )


def _payload(*, mode: str = "dry-run", reviewed_plan_sha256: str = "") -> dict[str, object]:
    payload: dict[str, object] = {
        "product": _PRODUCT,
        "mode": mode,
        "reason": "Record the repository identity at switch-over.",
    }
    if reviewed_plan_sha256:
        payload["reviewed_plan_sha256"] = reviewed_plan_sha256
    return payload


class ProductRepositoryIdentityRequestTests(unittest.TestCase):
    def test_request_rejects_caller_supplied_ids(self) -> None:
        with self.assertRaises(ValidationError):
            ProductRepositoryIdentityApplyRequest.model_validate(
                {**_payload(), "repository_id": "7001"}
            )

    def test_apply_requires_reviewed_plan_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            ProductRepositoryIdentityApplyRequest.model_validate(_payload(mode="apply"))


class FastApiProductRepositoryIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(
                Path(self._temporary_directory.name) / "launchplane.sqlite3"
            )
        )
        self.store.ensure_schema()
        self.app = create_launchplane_fastapi_app(
            verifier=StubVerifier(_operator_identity()),
            authz_policy=_write_policy(),
            record_store_factory=lambda: self.store,
        )

    def tearDown(self) -> None:
        self.store.close()
        self._temporary_directory.cleanup()

    async def _post(
        self, payload: dict[str, object], *, idempotency_key: str = ""
    ) -> _AsgiResponse:
        headers = {"Authorization": "Bearer valid-token"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await _asgi_request(self.app, "POST", _ROUTE, headers=headers, payload=payload)

    async def _reviewed_plan_sha256(self) -> str:
        response = await self._post(_payload())
        self.assertEqual(response.status_code, 202, response.json())
        return str(response.json()["result"]["plan_sha256"])

    def _assert_refused(self, response: _AsgiResponse, *, status_code: int, code: str) -> None:
        self.assertEqual(response.status_code, status_code, response.json())
        self.assertEqual(response.json()["error"]["code"], code)

    async def test_dry_run_plans_identity_from_inventory_without_writing(self) -> None:
        original = _profile(repository="CBusillo/SellYourOutboard")
        self.store.write_product_profile_record(original)
        inventory = _inventory()
        self.store.write_repository_inventory_record(inventory)

        response = await self._post(_payload())

        self.assertEqual(response.status_code, 202, response.json())
        result = response.json()["result"]
        self.assertEqual(result["operation"], "record")
        self.assertEqual(
            result["identity_before"], {"repository_id": "", "repository_owner_id": ""}
        )
        self.assertEqual(
            result["identity_after"], {"repository_id": "7001", "repository_owner_id": "8001"}
        )
        self.assertEqual(result["inventory_record_id"], inventory.record_id)
        self.assertEqual(result["inventory_digest"], inventory.inventory_digest)
        self.assertRegex(result["profile_record_sha256_before"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(result["applied"])
        self.assertEqual(self.store.read_product_profile_record(_PRODUCT), original)

    async def test_apply_records_inventory_identity_and_reads_back(self) -> None:
        original = _profile()
        self.store.write_product_profile_record(original)
        self.store.write_repository_inventory_record(_inventory())
        plan_sha256 = await self._reviewed_plan_sha256()

        response = await self._post(
            _payload(mode="apply", reviewed_plan_sha256=plan_sha256),
            idempotency_key="repository-identity-apply",
        )
        replay = await self._post(
            _payload(mode="apply", reviewed_plan_sha256=plan_sha256),
            idempotency_key="repository-identity-apply",
        )

        self.assertEqual(response.status_code, 202, response.json())
        result = response.json()["result"]
        self.assertTrue(result["applied"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["read_back_matches"])
        stored = self.store.read_product_profile_record(_PRODUCT)
        self.assertEqual((stored.repository_id, stored.repository_owner_id), ("7001", "8001"))
        self.assertEqual(stored.source, "service:product-repository-identity")
        self.assertEqual(result["profile_updated_at_after"], stored.updated_at)
        self.assertEqual(stored.lanes, original.lanes)
        self.assertEqual(stored.repository, original.repository)
        self.assertEqual(replay.status_code, 202)
        self.assertTrue(replay.json()["replayed"])

    async def test_apply_reports_unchanged_when_identity_already_matches(self) -> None:
        original = _profile(repository_id="7001", repository_owner_id="8001")
        self.store.write_product_profile_record(original)
        self.store.write_repository_inventory_record(_inventory())
        plan_sha256 = await self._reviewed_plan_sha256()

        response = await self._post(
            _payload(mode="apply", reviewed_plan_sha256=plan_sha256),
            idempotency_key="repository-identity-unchanged",
        )

        self.assertEqual(response.status_code, 202, response.json())
        result = response.json()["result"]
        self.assertEqual(result["operation"], "unchanged")
        self.assertFalse(result["changed"])
        self.assertTrue(result["read_back_matches"])
        self.assertEqual(self.store.read_product_profile_record(_PRODUCT), original)

    async def test_retry_after_lost_receipt_reports_unchanged(self) -> None:
        self.store.write_product_profile_record(_profile())
        self.store.write_repository_inventory_record(_inventory())
        plan_sha256 = await self._reviewed_plan_sha256()
        first = await self._post(
            _payload(mode="apply", reviewed_plan_sha256=plan_sha256),
            idempotency_key="repository-identity-first",
        )
        self.assertEqual(first.status_code, 202, first.json())
        written = self.store.read_product_profile_record(_PRODUCT)

        retry = await self._post(
            _payload(mode="apply", reviewed_plan_sha256=plan_sha256),
            idempotency_key="repository-identity-retry",
        )

        self.assertEqual(retry.status_code, 202, retry.json())
        self.assertNotIn("replayed", retry.json())
        self.assertEqual(retry.json()["result"]["operation"], "unchanged")
        self.assertTrue(retry.json()["result"]["read_back_matches"])
        self.assertEqual(self.store.read_product_profile_record(_PRODUCT), written)

    async def test_apply_refuses_stale_reviewed_plan(self) -> None:
        original = _profile()
        self.store.write_product_profile_record(original)
        self.store.write_repository_inventory_record(_inventory())
        plan_sha256 = await self._reviewed_plan_sha256()
        self.store.write_product_profile_record(
            original.model_copy(update={"display_name": "Renamed while under review"})
        )

        response = await self._post(
            _payload(mode="apply", reviewed_plan_sha256=plan_sha256),
            idempotency_key="repository-identity-stale",
        )

        self._assert_refused(response, status_code=409, code="stale")
        self.assertEqual(self.store.read_product_profile_record(_PRODUCT).repository_id, "")

    async def test_refuses_missing_profile(self) -> None:
        self.store.write_repository_inventory_record(_inventory())

        response = await self._post(_payload())

        self._assert_refused(response, status_code=404, code="not_found")

    async def test_refuses_profile_without_usable_repository(self) -> None:
        self.store.write_product_profile_record(_profile(repository="sellyouroutboard"))
        self.store.write_repository_inventory_record(_inventory())

        response = await self._post(_payload())

        self._assert_refused(
            response, status_code=409, code="repository_identity_profile_repository_missing"
        )

    async def test_refuses_without_tracked_inventory(self) -> None:
        self.store.write_product_profile_record(_profile())
        tracked = _inventory()
        self.store.write_repository_inventory_record(tracked)
        self.store.write_repository_inventory_record(
            _inventory(
                inventory_state="retired",
                inventory_revision=2,
                supersedes_record_id=tracked.record_id,
            )
        )

        response = await self._post(_payload())

        self._assert_refused(
            response, status_code=409, code="repository_identity_inventory_missing"
        )

    async def test_refuses_when_two_repositories_are_tracked_under_one_name(self) -> None:
        self.store.write_product_profile_record(_profile())
        self.store.write_repository_inventory_record(_inventory())
        self.store.write_repository_inventory_record(_inventory(repository_id="7002"))

        response = await self._post(_payload())

        self._assert_refused(
            response, status_code=409, code="repository_identity_inventory_ambiguous"
        )

    async def test_refuses_identity_already_recorded_by_another_product(self) -> None:
        self.store.write_product_profile_record(_profile())
        self.store.write_product_profile_record(
            _profile(product="other-site", repository_id="7001", repository_owner_id="8001")
        )
        self.store.write_repository_inventory_record(_inventory())

        response = await self._post(_payload())

        self._assert_refused(
            response, status_code=409, code="repository_identity_claimed_by_other_product"
        )

    async def test_never_overwrites_a_different_recorded_identity(self) -> None:
        original = _profile(repository_id="6001", repository_owner_id="8001")
        self.store.write_product_profile_record(original)
        self.store.write_repository_inventory_record(_inventory())

        response = await self._post(_payload())

        self._assert_refused(response, status_code=409, code="repository_identity_conflict")
        self.assertEqual(self.store.read_product_profile_record(_PRODUCT), original)

    async def test_requires_product_scoped_profile_write_grant(self) -> None:
        self.store.write_product_profile_record(_profile(product="other-site"))
        self.store.write_repository_inventory_record(_inventory())

        response = await self._post({**_payload(), "product": "other-site"})

        self._assert_refused(response, status_code=403, code="authorization_denied")

    async def test_apply_requires_idempotency_key(self) -> None:
        self.store.write_product_profile_record(_profile())
        self.store.write_repository_inventory_record(_inventory())
        plan_sha256 = await self._reviewed_plan_sha256()

        response = await self._post(_payload(mode="apply", reviewed_plan_sha256=plan_sha256))

        self._assert_refused(response, status_code=400, code="idempotency_key_required")

    async def test_validation_error_does_not_echo_input(self) -> None:
        response = await self._post({**_payload(), "unexpected": "pasted-secret-material"})

        self._assert_refused(response, status_code=400, code="invalid_request")
        self.assertNotIn("pasted-secret-material", response.text)


if __name__ == "__main__":
    unittest.main()
