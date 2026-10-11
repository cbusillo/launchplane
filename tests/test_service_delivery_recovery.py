from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from typing import Any, Callable
import unittest
from unittest.mock import patch

from control_plane.github_delivery_configuration import (
    DeliveryGitHubAppConfigurationRequest,
    apply_delivery_github_configuration,
)
from control_plane.service_github_delivery_controls import (
    apply_service_token_retirement,
    ServiceTokenRetirementRequest,
    ServiceTokenRetirementResponse,
)
from control_plane.service_delivery_recovery import (
    ServiceDeliveryApplyReceiptResponse,
    ServiceDeliveryApplyReceiptRequest,
    read_service_delivery_apply_receipt,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from tests.support.stores import sqlite_database_url
from tests.test_service_github_delivery_controls import seed_metadata, retirement_request


OriginalRequest = DeliveryGitHubAppConfigurationRequest | ServiceTokenRetirementRequest
ApplyResult = dict[str, object] | ServiceTokenRetirementResponse
Executor = Callable[..., ApplyResult]


class ServiceDeliveryRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "state.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        seed_metadata(self.store)

    def cases(self) -> tuple[tuple[OriginalRequest, Executor], ...]:
        return (
            (
                DeliveryGitHubAppConfigurationRequest(
                    app_id=78, integration="existing-delivery", reason="Fixture selection."
                ),
                apply_delivery_github_configuration,
            ),
            (retirement_request(), apply_service_token_retirement),
        )

    def prepare(self, request: OriginalRequest, execute: Executor) -> OriginalRequest:
        preview = execute(
            store=self.store,
            request=request,
            actor="github:42",
            trace_id="plan",
            idempotency_key="",
        )
        digest = preview["plan_digest"] if isinstance(preview, dict) else preview.plan_digest
        return request.model_copy(
            update={
                "mode": "apply",
                "expected_plan_digest": digest,
                **({"director_confirmed": True} if hasattr(request, "secret_ids") else {}),
            }
        )

    def recover(
        self, request: OriginalRequest, actor: str = "github:42", key: str = "original-key"
    ) -> ServiceDeliveryApplyReceiptResponse:
        return read_service_delivery_apply_receipt(
            store=self.store,
            request=ServiceDeliveryApplyReceiptRequest(
                original_request=request, idempotency_key=key
            ),
            actor=actor,
            trace_id="read",
        )

    def test_missing_and_other_actor_receipts_do_not_settle_or_mutate(self) -> None:
        for request, execute in self.cases():
            with self.subTest(operation=execute.__name__):
                apply = self.prepare(request, execute)
                before = (
                    self.store.list_runtime_environment_records(),
                    self.store.list_secret_records(),
                    self.store.list_secret_bindings(),
                )
                with patch.object(
                    self.store,
                    "write_product_authority_bundle",
                    side_effect=AssertionError("Receipt read must be inert"),
                ):
                    self.assertEqual(self.recover(apply).state, "unknown")
                self.assertEqual(
                    before,
                    (
                        self.store.list_runtime_environment_records(),
                        self.store.list_secret_records(),
                        self.store.list_secret_bindings(),
                    ),
                )
                execute(
                    store=self.store,
                    request=apply,
                    actor="github:42",
                    trace_id="completed",
                    idempotency_key="original-key",
                )
                self.assertEqual(self.recover(apply).original_trace_id, "completed")
                self.assertEqual(self.recover(apply, actor="github:43").state, "unknown")
                self.assertEqual(self.recover(apply, key="fresh-key").state, "unknown")
                with self.assertRaises(ValueError):
                    self.recover(apply.model_copy(update={"reason": "Different request"}))

    def test_duplicate_that_misses_early_lookup_replays_committed_receipt(self) -> None:
        for request, execute in self.cases():
            with self.subTest(operation=execute.__name__):
                apply = self.prepare(request, execute)
                completed = execute(
                    store=self.store,
                    request=apply,
                    actor="github:42",
                    trace_id="original",
                    idempotency_key="original-key",
                )
                read = self.store.read_idempotency_record
                count = 0

                def miss_once(**kwargs: Any) -> LaunchplaneIdempotencyRecord | None:
                    nonlocal count
                    count += 1
                    return None if count == 1 else read(**kwargs)

                with (
                    patch.object(self.store, "read_idempotency_record", side_effect=miss_once),
                    patch.object(
                        self.store,
                        "write_product_authority_bundle",
                        side_effect=AssertionError("No second mutation"),
                    ),
                ):
                    replay = execute(
                        store=self.store,
                        request=apply,
                        actor="github:42",
                        trace_id="duplicate",
                        idempotency_key="original-key",
                    )
                self.assertEqual(replay, completed)

    def test_concurrent_duplicates_commit_once_and_return_one_receipt(self) -> None:
        for request, execute in self.cases():
            with self.subTest(operation=execute.__name__):
                self.assert_concurrent_replay(request, execute)

    def test_concurrent_unchanged_selection_replays_unique_receipt(self) -> None:
        self.assert_concurrent_replay(
            DeliveryGitHubAppConfigurationRequest(
                app_id=76, integration="existing-delivery", reason="Confirm the existing selection."
            ),
            apply_delivery_github_configuration,
        )

    def assert_concurrent_replay(self, request: OriginalRequest, execute: Executor) -> None:
        apply = self.prepare(request, execute)
        barrier = Barrier(2)
        write = self.store.write_product_authority_bundle

        def simultaneous(bundle: ProductAuthorityBundle) -> None:
            barrier.wait(timeout=30)
            write(bundle)

        def run(index: int) -> ApplyResult:
            return execute(
                store=self.store,
                request=apply,
                actor="github:42",
                trace_id=f"concurrent-{index}",
                idempotency_key="original-key",
            )

        with (
            patch.object(self.store, "write_product_authority_bundle", side_effect=simultaneous),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(run, index) for index in range(2)]
            results = [future.result(timeout=60) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.recover(apply).state, "completed")

    def test_corrupt_completion_is_not_accepted(self) -> None:
        request, execute = self.cases()[0]
        apply = self.prepare(request, execute)
        execute(
            store=self.store,
            request=apply,
            actor="github:42",
            trace_id="original",
            idempotency_key="original-key",
        )
        receipt = self.store.read_idempotency_record(
            scope="github-delivery-configuration:github:42",
            route_path="/v1/service/github-delivery/configuration",
            idempotency_key="original-key",
        )
        assert receipt is not None
        for update in (
            {"response_status_code": 409},
            {"response_payload": {**receipt.response_payload, "actor": "github:43"}},
            {"response_payload": {**receipt.response_payload, "trace_id": "different"}},
        ):
            with patch.object(
                self.store,
                "read_idempotency_record",
                return_value=receipt.model_copy(update=update),
            ):
                with self.assertRaises(ValueError):
                    self.recover(apply)
