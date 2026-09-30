import unittest
from typing import Any
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import ProductReconcileTarget
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _asgi_get, _product_profile_read_policy
from tests.support.auth import StubVerifier, identity
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url

_COMMIT = "cdd8f4a0d68be3575389fdffbcd6ef138ca13cc9"
_DIGEST = "sha256:" + "d5da36c3" * 8
# Shapes a secret can take in text the reconciler saves from GitHub, providers and builds.
_SECRETS = (
    "hunter2-db-password",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "eyJhbGciOiJIUzI1NiJ9.payload.signaturevalue",
    "Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5",
    "s3cr3t-in-url",
)
_ROUTE = "/v1/product-profiles/{product}/reconcile-requests"


def _write_request(store: PostgresRecordStore, *, product: str, number: int) -> None:
    store.request_product_reconcile(
        ProductReconcileTarget(product=product, target_kind="preview", pull_request_number=number),
        "2026-09-30T18:48:00Z",
    )
    claimed = store.claim_next_product_reconcile_request("worker", 60)
    assert claimed is not None
    store.complete_product_reconcile_request(
        claimed.target_key,
        "worker",
        "failed",
        {
            "target": "preview",
            "action": "apply",
            "head_sha": _COMMIT,
            "desired_image_digest": _DIGEST,
            "omitted_integration_credential_keys": ["ODOO_SMTP_PASSWORD"],
            "detail": f"ODOO_SMTP_PASSWORD={_SECRETS[0]} with token {_SECRETS[1]}",
            "provider": {"response": f"Authorization: Bearer {_SECRETS[2]}", "raw": _SECRETS[3]},
        },
        f"Provider rejected https://admin:{_SECRETS[4]}@provider.example/api for {_COMMIT}",
    )


async def _read_requests(*, product: str, read_product: str) -> tuple[int, str, dict[str, Any]]:
    with TemporaryDirectory() as temporary_directory:
        store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(temporary_directory) / "lp.sqlite3")
        )
        store.ensure_schema()
        for name in ("sellyouroutboard", "verireel"):
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(product_profile_payload(name))
            )
            _write_request(store, product=name, number=111)
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=_product_profile_read_policy(product=read_product),
            record_store_factory=lambda: store,
        )
        response = await _asgi_get(
            app,
            _ROUTE.format(product=product),
            headers={"Authorization": "Bearer valid-token"},
        )
    return response.status_code, response.text, response.json()


class ProductReconcileRequestsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_products_decisions_without_secret_values(self) -> None:
        status, text, payload = await _read_requests(
            product="sellyouroutboard", read_product="sellyouroutboard"
        )

        self.assertEqual(status, 200, text)
        for secret in _SECRETS:
            self.assertNotIn(secret, text)
        (request,) = payload["requests"]
        self.assertEqual(request["target_key"], "sellyouroutboard:preview:111")
        self.assertEqual(request["state"], "failed")
        plan = request["last_plan"]
        self.assertEqual(plan["head_sha"], _COMMIT)
        self.assertEqual(plan["desired_image_digest"], _DIGEST)
        self.assertEqual(plan["omitted_integration_credential_keys"], ["ODOO_SMTP_PASSWORD"])
        self.assertIn("ODOO_SMTP_PASSWORD=[redacted]", plan["detail"])
        self.assertIn("[redacted-url]", request["last_error"])

    async def test_refuses_a_caller_without_read_access_to_the_product(self) -> None:
        status, text, _ = await _read_requests(product="verireel", read_product="sellyouroutboard")

        self.assertEqual(status, 403, text)
        for secret in _SECRETS:
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
