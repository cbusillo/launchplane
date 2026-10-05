import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.product_profile_mutation_receipt import profile_mutation_receipt
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_routes.mutation_support import idempotency_scope
from tests.http_app_test_support import _product_profile_read_policy
from tests.support.auth import StubVerifier, identity
from tests.support.http import request
from tests.test_http_app_product_owner import _PRODUCT, _post_owner, _profile, _store, _workflow_app


class ProfileMutationReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_committed_save_can_be_read_without_the_request_and_never_writes(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), _profile())
            applied = await _post_owner(
                _workflow_app(store),
                {"mode": "apply", "github_login": "site-owner", "reason": "Set the Client."},
                idempotency_key="lost-save",
            )
            self.assertEqual(applied.status_code, 202)
            record = store.read_idempotency_record(
                scope=idempotency_scope(identity()),
                route_path="/v1/product-profiles/{product}/owner",
                idempotency_key="lost-save",
            )
            assert record is not None
            for changes in (
                {"response_status_code": 400},
                {"response_payload": {**record.response_payload, "result": {"applied": False}}},
            ):
                receipt = profile_mutation_receipt(
                    record=record.model_copy(update=changes),
                    trace_id="read",
                    product=_PRODUCT,
                    field="owner",
                    idempotency_key="lost-save",
                )
                self.assertEqual(receipt.state, "unresolved")
                self.assertEqual(receipt.original_trace_id, "")
            before = store.read_product_profile_record(_PRODUCT)
            app = _workflow_app(store, policy=_product_profile_read_policy(product=_PRODUCT))
            response = await request(
                app,
                "GET",
                f"/v1/product-profiles/{_PRODUCT}/mutation-receipts/owner?operation_key=lost-save",
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["state"], "completed")
            self.assertEqual(response.json()["original_trace_id"], applied.json()["trace_id"])
            self.assertEqual(response.json()["idempotency_key"], "lost-save")
            self.assertNotIn("result", response.json())
            self.assertEqual(store.read_product_profile_record(_PRODUCT), before)
            for product, field, key in (
                (_PRODUCT, "image-repository", "lost-save"),
                (_PRODUCT, "owner", "missing"),
                ("another-product", "owner", "lost-save"),
            ):
                read_app = _workflow_app(
                    store, policy=_product_profile_read_policy(product=product)
                )
                response = await request(
                    read_app,
                    "GET",
                    f"/v1/product-profiles/{product}/mutation-receipts/{field}?operation_key={key}",
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertEqual(response.json()["state"], "unresolved")
                self.assertEqual(response.json()["original_trace_id"], "")
            other = create_launchplane_fastapi_app(
                verifier=StubVerifier(
                    identity(
                        workflow_ref="every/verireel/.github/workflows/other.yml@refs/heads/main"
                    )
                ),
                authz_policy=_product_profile_read_policy(product=_PRODUCT).model_copy(
                    update={
                        "github_actions": tuple(
                            rule.model_copy(
                                update={
                                    "workflow_refs": (
                                        "every/verireel/.github/workflows/other.yml@refs/heads/main",
                                    )
                                }
                            )
                            for rule in _product_profile_read_policy(
                                product=_PRODUCT
                            ).github_actions
                        )
                    }
                ),
                record_store_factory=lambda: store,
            )
            response = await request(
                other,
                "GET",
                f"/v1/product-profiles/{_PRODUCT}/mutation-receipts/owner?operation_key=lost-save",
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertEqual(response.json()["state"], "unresolved")
            denied = await request(
                app,
                "GET",
                "/v1/product-profiles/unauthorized/mutation-receipts/owner?operation_key=lost-save",
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertEqual(denied.status_code, 403)
            store.close()

    async def test_running_reservation_never_settles_a_missing_draft(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), _profile())
            reservation = store.reserve_mutation(
                scope=idempotency_scope(identity()),
                route_path="/v1/product-profiles/{product}/owner",
                idempotency_key="running",
                request_fingerprint="request",
                lease_owner="worker",
            )
            record = reservation.record
            assert record is not None
            app = _workflow_app(store, policy=_product_profile_read_policy(product=_PRODUCT))
            response = await request(
                app,
                "GET",
                f"/v1/product-profiles/{_PRODUCT}/mutation-receipts/owner?operation_key=running",
                headers={"Authorization": "Bearer valid-token"},
            )
            self.assertEqual(response.json()["state"], "unresolved")
            self.assertEqual(
                store.read_idempotency_record(
                    scope=record.scope,
                    route_path=record.route_path,
                    idempotency_key=record.idempotency_key,
                ),
                record,
            )
            store.close()
