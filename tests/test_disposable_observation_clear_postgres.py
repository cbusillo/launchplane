"""Reviewed disposable-observation clears exclude concurrent authority changes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import threading
import time
from typing import Any
import unittest
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_postgres_integration import _head_postgres_database


class RealPostgresDisposableObservationClearTests(unittest.TestCase):
    def test_reviewed_clear_keeps_target_and_preview_writers_excluded_through_commit(self) -> None:
        from control_plane import disposable_lanes
        from tests.test_director_bounded_writes import DirectorBoundedWritesTests
        from tests.support.disposable_lanes import record_safety

        for change in ("target-id", "preview-insert"):
            with self.subTest(change=change), _head_postgres_database() as url:
                fixture = DirectorBoundedWritesTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                marker = "reviewed-clear-" + change
                writer_url = make_url(url).update_query_dict({"application_name": marker})
                with (
                    closing(PostgresRecordStore(database_url=url)) as first,
                    closing(
                        PostgresRecordStore(
                            database_url=writer_url.render_as_string(hide_password=False)
                        )
                    ) as second,
                ):
                    profile, preview = fixture.profile, fixture.preview
                    instance = profile.preview.slug_template.format(number=preview.anchor_pr_number)
                    scope = dict(
                        product=profile.product, context=preview.context, instance=instance
                    )
                    first.write_product_profile_record(profile)
                    first.write_preview_record(preview)
                    target = DokployTargetIdRecord(
                        context=preview.context,
                        instance=instance,
                        target_id="reviewed-clear-original",
                        updated_at=profile.updated_at,
                    )
                    first.write_dokploy_target_id_record(target)
                    record_safety(first, **scope, preview=preview, usage="in_use")
                    reviewed = disposable_lanes.resolve_disposable_observation_binding(
                        first, **scope, preview_id=preview.preview_id
                    )
                    snapshot_read = threading.Event()
                    release_clear = threading.Event()
                    at_write = threading.Event()
                    resolve = disposable_lanes.resolve_disposable_observation_binding

                    def pause_current_binding(*args: Any, **kwargs: Any) -> Any:
                        result = resolve(*args, **kwargs)
                        if args[0] is first:
                            snapshot_read.set()
                            if not release_clear.wait(10):
                                raise RuntimeError("Fixture did not release checked clear")
                        return result

                    def write() -> None:
                        at_write.set()
                        if change == "target-id":
                            second.write_dokploy_target_id_record(
                                target.model_copy(update={"target_id": "reviewed-clear-successor"})
                            )
                        else:
                            # Same context and PR slug, new ID: exact scope becomes ambiguous.
                            second.write_preview_record(
                                preview.model_copy(
                                    update={"preview_id": preview.preview_id + "-duplicate"}
                                )
                            )
                        # Successful writer has passed the exclusion held through deletion.
                        self.assertIsNone(second.read_disposable_lane_safety(**scope))

                    with (
                        patch.object(
                            disposable_lanes,
                            "resolve_disposable_observation_binding",
                            side_effect=pause_current_binding,
                        ),
                        ThreadPoolExecutor(max_workers=2) as pool,
                    ):
                        clearing = pool.submit(
                            first.clear_reviewed_disposable_lane_safety,
                            **scope,
                            preview_id=preview.preview_id,
                            expected_binding_sha256=reviewed.binding_sha256,
                            expected_observation_sha256=disposable_lanes.disposable_lane_observation_sha256(
                                first.read_disposable_lane_safety(**scope)
                            ),
                        )
                        try:
                            if not snapshot_read.wait(10):
                                clearing.result(timeout=1)
                                self.fail("Checked clear did not resolve its held current binding")
                            writer = pool.submit(write)
                            self.assertTrue(at_write.wait(10))
                            waiting = False
                            deadline = time.monotonic() + 5
                            while not writer.done() and time.monotonic() < deadline:
                                with first._engine.connect() as observer:
                                    waiting = bool(
                                        observer.scalar(
                                            text(
                                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                                "WHERE datname=current_database() "
                                                "AND application_name=:marker AND wait_event_type='Lock')"
                                            ),
                                            {"marker": marker},
                                        )
                                    )
                                if waiting:
                                    break
                                time.sleep(0.01)
                            self.assertTrue(
                                waiting, "Writer did not reach a real database lock wait"
                            )
                            self.assertFalse(writer.done())
                        finally:
                            release_clear.set()
                        self.assertEqual(clearing.result(timeout=10), reviewed.binding_sha256)
                        writer.result(timeout=10)
                    self.assertIsNone(first.read_disposable_lane_safety(**scope))
                    if change == "target-id":
                        self.assertEqual(
                            first.read_dokploy_target_id_record(
                                context_name=preview.context, instance_name=instance
                            ).target_id,
                            "reviewed-clear-successor",
                        )
                    else:
                        with self.assertRaises(disposable_lanes.DisposableLaneSafetyUnknown):
                            disposable_lanes.resolve_disposable_observation_binding(
                                first, **scope, preview_id=preview.preview_id
                            )

    def test_busy_authority_times_out_without_clearing_the_reviewed_report(self) -> None:
        from control_plane import disposable_lanes
        from tests.test_director_bounded_writes import DirectorBoundedWritesTests
        from tests.support.disposable_lanes import record_safety

        fixture = DirectorBoundedWritesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with (
            _head_postgres_database() as url,
            closing(PostgresRecordStore(database_url=url)) as first,
            closing(PostgresRecordStore(database_url=url)) as second,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            first.write_product_profile_record(fixture.profile)
            first.write_preview_record(fixture.preview)
            scope = dict(
                product=fixture.profile.product,
                context=fixture.preview.context,
                instance=fixture.profile.preview.slug_template.format(
                    number=fixture.preview.anchor_pr_number
                ),
            )
            record_safety(first, **scope, preview=fixture.preview, usage="in_use")
            reviewed = disposable_lanes.resolve_disposable_observation_binding(
                first, **scope, preview_id=fixture.preview.preview_id
            )
            protected = first.read_disposable_lane_safety(**scope)
            with first.serialize_disposable_testing_resources():
                clearing = pool.submit(
                    second.clear_reviewed_disposable_lane_safety,
                    **scope,
                    preview_id=fixture.preview.preview_id,
                    expected_binding_sha256=reviewed.binding_sha256,
                    expected_observation_sha256=disposable_lanes.disposable_lane_observation_sha256(
                        protected
                    ),
                )
                with self.assertRaises(SQLAlchemyError):
                    clearing.result(timeout=15)
                self.assertEqual(first.read_disposable_lane_safety(**scope), protected)
            self.assertEqual(second.read_disposable_lane_safety(**scope), protected)
