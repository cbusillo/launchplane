from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from sqlalchemy import delete, event, Select
from sqlalchemy.sql.elements import BindParameter

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin
from control_plane.provider_hold_attention import (
    PROVIDER_HOLD_PAGE_LIMIT,
    parse_provider_hold_cursor,
    provider_hold_cursor,
)
from control_plane.storage.postgres import (
    ORIGINAL_MUTATION_INTENT_ROUTE,
    LaunchplaneIdempotencyRow,
    PostgresRecordStore,
)


def reserve_attention_hold(store: PostgresRecordStore, key: str) -> LaunchplaneIdempotencyRecord:
    origin = ProviderTargetOrigin(
        product="retired-product",
        context="deleted-context",
        instance="prod",
        provider_id="fixture-provider",
        target_type="compose",
        target_id=key,
    )
    with patch.object(store, "_database_mutation_timestamp", return_value="2026-10-01T00:00:00Z"):
        return store.reserve_mutation(
            scope="PRIVATE-CALLER-SCOPE",
            route_path="/private-operation",
            idempotency_key=key,
            request_fingerprint="PRIVATE-FINGERPRINT",
            lease_owner="fixture",
            lease_seconds=60,
            reconciliation_key="PRIVATE-INTENT",
            provider_target_key=origin.target_key(),
            provider_target_origin=origin,
        ).record


class ProviderHoldAttentionTests(unittest.TestCase):
    def database_url(self) -> str:
        directory = self.enterContext(TemporaryDirectory())
        return f"sqlite+pysqlite:///{Path(directory) / 'page.db'}"

    def setUp(self) -> None:
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(self.store.close)
        self.store.ensure_schema()

    def test_keyset_pages_bound_projection_and_keep_inactive_original_scope(self) -> None:
        rows = [reserve_attention_hold(self.store, f"row-{index}") for index in range(7)]
        for index, key in enumerate(("unrelated:key", rows[0].provider_target_key + ":child")):
            self.store.reserve_mutation(
                scope="fixture",
                route_path="/fixture",
                idempotency_key=f"other-{index}",
                request_fingerprint="fixture",
                lease_owner="fixture",
                lease_seconds=60,
                reconciliation_key="fixture",
                provider_target_key=key,
            )
        seen: list[str | None] = []
        cursor = None
        while True:
            page = self.store.read_provider_hold_attention_page(limit=2, cursor=cursor)
            seen.extend(item.operation_reference for item in page.items)
            self.assertEqual(page.completeness, "partial")
            self.assertTrue(
                all(
                    item.original_scope and item.original_scope.product == "retired-product"
                    for item in page.items
                )
            )
            if not page.has_more:
                break
            self.assertIsNotNone(page.next_cursor)
            self.assertNotEqual(page.next_cursor, cursor)
            cursor = page.next_cursor
        self.assertEqual(len(seen), len(rows))
        self.assertEqual(len(set(seen)), len(rows))
        last = max(rows, key=lambda row: row.record_id)
        empty = self.store.read_provider_hold_attention_page(
            limit=2, cursor=provider_hold_cursor(last.record_id)
        )
        self.assertEqual(empty.items, ())
        self.assertEqual(empty.completeness, "partial")

    def test_read_is_sql_bounded_and_does_not_call_bulk_inventory_or_catalog(self) -> None:
        for index in range(5):
            reserve_attention_hold(self.store, f"sql-{index}")
        membership_bounds: list[int | None] = []

        def capture(
            _connection: object,
            _cursor: object,
            _statement: str,
            _parameters: Any,
            context: Any,
            _many: bool,
        ) -> None:
            compiled = context.compiled
            if compiled is None or not isinstance(compiled.statement, Select):
                return
            statement = compiled.statement
            if not any(
                column.get("entity") is LaunchplaneIdempotencyRow
                for column in statement.column_descriptions
            ):
                return
            cap = statement._limit_clause
            if cap is None:
                cap = statement._fetch_clause
                if (statement._fetch_clause_options or {}).get("percent"):
                    cap = None
            bind_name = compiled.bind_names.get(cap) if isinstance(cap, BindParameter) else None
            value = context.compiled_parameters[0].get(bind_name) if bind_name else None
            membership_bounds.append(value if type(value) is int else None)

        event.listen(self.store._engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.store._engine, "before_cursor_execute", capture)
        with (
            patch.object(
                self.store, "list_provider_target_holds", side_effect=AssertionError("bulk scan")
            ),
            patch.object(
                self.store,
                "list_product_profile_records",
                side_effect=AssertionError("catalog scan"),
            ),
        ):
            limit = 1
            page = self.store.read_provider_hold_attention_page(limit=limit)
        self.assertTrue(page.has_more)
        self.assertTrue(membership_bounds)
        self.assertEqual(membership_bounds[0], limit + 1)
        with patch.object(
            self.store, "_read_provider_hold_record", wraps=self.store._read_provider_hold_record
        ) as read:
            self.store.read_provider_hold_attention_page(limit=1)
            baseline_work = read.call_count
        for index in range(20):
            reserve_attention_hold(self.store, f"larger-{index}")
        with patch.object(
            self.store, "_read_provider_hold_record", wraps=self.store._read_provider_hold_record
        ) as read:
            larger = self.store.read_provider_hold_attention_page(limit=1)
        self.assertTrue(larger.has_more)
        self.assertGreater(baseline_work, 0)
        self.assertEqual(read.call_count, baseline_work)

    def test_public_page_omits_private_data_and_retains_unknown_and_corrupt_origin(self) -> None:
        rows = [
            reserve_attention_hold(self.store, name) for name in ("PRIVATE-KEY-A", "PRIVATE-KEY-B")
        ]
        with self.store._session_factory.begin() as session:
            session.execute(
                delete(LaunchplaneIdempotencyRow).where(
                    LaunchplaneIdempotencyRow.route_path == ORIGINAL_MUTATION_INTENT_ROUTE,
                    LaunchplaneIdempotencyRow.idempotency_key == rows[0].record_id,
                )
            )
            receipt = (
                session.query(LaunchplaneIdempotencyRow)
                .filter_by(
                    route_path=ORIGINAL_MUTATION_INTENT_ROUTE,
                    idempotency_key=rows[1].record_id,
                )
                .one()
            )
            receipt.payload = {
                **receipt.payload,
                "response_payload": {
                    **receipt.payload["response_payload"],
                    "provider_target_origin": {"private": "PRIVATE-CAPSULE"},
                },
            }
        page = self.store.read_provider_hold_attention_page()
        self.assertEqual({item.attribution for item in page.items}, {"unavailable", "corrupt"})
        self.assertTrue(
            all(
                item.original_scope is None and item.operation_reference is None
                for item in page.items
            )
        )
        public = page.model_dump_json()
        for private in (
            "PRIVATE-",
            "provider_target_key",
            "fingerprint",
            "source_sha256",
            "original_intent",
            "lease_owner",
        ):
            self.assertNotIn(private, public)

    def test_indexed_payload_contradiction_is_unavailable_never_empty(self) -> None:
        row = reserve_attention_hold(self.store, "drift")
        with self.store._session_factory.begin() as session:
            source = session.get(LaunchplaneIdempotencyRow, row.record_id)
            assert source is not None
            source.payload = {**source.payload, "provider_target_key": "unrelated:key"}
        with self.assertRaises(ValueError):
            self.store.read_provider_hold_attention_page()

    def test_completed_operation_disappears_without_hiding_another(self) -> None:
        first, second = [
            reserve_attention_hold(self.store, name) for name in ("complete", "retained")
        ]
        self.store.write_idempotency_record(
            first.model_copy(
                update={
                    "state": "completed",
                    "response_status_code": 500,
                    "response_trace_id": "failed",
                    "recorded_at": "2026-10-01T00:01:00Z",
                }
            )
        )
        page = self.store.read_provider_hold_attention_page()
        self.assertEqual(len(page.items), 1)
        self.assertIn(second.record_id, page.items[0].operation_reference or "")
        self.assertEqual(page.completeness, "single_page")

    def test_cursor_and_limit_are_validated_before_queries(self) -> None:
        with patch.object(self.store, "_session_factory", side_effect=AssertionError("data read")):
            for cursor in ("", "private", "v1:mutation-reservation-" + "f" * 65):
                with self.assertRaises(ValueError):
                    self.store.read_provider_hold_attention_page(cursor=cursor)
            for limit in (0, PROVIDER_HOLD_PAGE_LIMIT + 1):
                with self.assertRaises(ValueError):
                    self.store.read_provider_hold_attention_page(limit=limit)
        self.assertEqual(parse_provider_hold_cursor(None), "")
