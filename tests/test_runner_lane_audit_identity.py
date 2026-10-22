from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
import unittest

from control_plane.contracts.runner_lane_registration import (
    RunnerLaneLifecycleOperation,
    RunnerLaneRegistrationAuditIdentityConflict,
    RunnerLaneRegistrationAuditRecord,
    RunnerLaneRegistrationAuditStatus,
    RunnerLaneRegistrationPolicy,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.runner_lane_relabel import deliver_terminal_audit, execute_relabel
from tests.http_app_test_support import (
    _post_runner_lane_registration_audit_evidence,
    _runner_lane_registration_audit_write_identity,
    _runner_lane_registration_audit_write_policy,
)
from tests.support.auth import _StubVerifier
from tests.support.runner_audits import lifecycle_audit
from tests.support.stores import sqlite_database_url
from tests.test_runner_lane_relabel import Transport


StoreKind = Literal["filesystem", "sqlite"]
STORE_KINDS: tuple[StoreKind, ...] = ("filesystem", "sqlite")
OPERATIONS: tuple[RunnerLaneLifecycleOperation, ...] = ("register", "retire", "relabel")


@contextmanager
def audit_store(
    kind: StoreKind, root: Path
) -> Iterator[FilesystemRecordStore | PostgresRecordStore]:
    if kind == "filesystem":
        yield FilesystemRecordStore(state_dir=root / "state")
    else:
        store = PostgresRecordStore(database_url=sqlite_database_url(root / "audit.sqlite"))
        try:
            store.ensure_schema()
            yield store
        finally:
            store.close()


def envelope(record: RunnerLaneRegistrationAuditRecord) -> dict[str, object]:
    return {"schema_version": 1, "product": "launchplane", "audit": record.model_dump(mode="json")}


class RunnerAuditIdentityTests(unittest.TestCase):
    def test_same_key_cannot_change_lifecycle_operation(self) -> None:
        for kind in STORE_KINDS:
            for original_operation in OPERATIONS:
                for incoming_operation in OPERATIONS:
                    if incoming_operation == original_operation:
                        continue
                    with self.subTest(
                        kind=kind, original=original_operation, incoming=incoming_operation
                    ):
                        with (
                            TemporaryDirectory() as directory,
                            audit_store(kind, Path(directory)) as store,
                        ):
                            original = lifecycle_audit(operation=original_operation)
                            store.write_runner_lane_registration_audit_record(original)
                            with self.assertRaises(RunnerLaneRegistrationAuditIdentityConflict):
                                store.write_runner_lane_registration_audit_record(
                                    lifecycle_audit(operation=incoming_operation)
                                )
                            self.assertEqual(
                                store.list_runner_lane_registration_audit_records(), (original,)
                            )

    def test_same_operation_cannot_change_any_original_request_field(self) -> None:
        changes: dict[str, object] = {
            "schema_version": 2,
            "repository": "example/other",
            "host_name": "build-2",
            "lane_name": "general-2",
            "registration_root": "/srv/other",
            "labels": ("general", "different"),
            "active_run_ids": (123,),
            "target_worker_active": True,
            "mutate": False,
            "expected_runner_id": 8,
            "expected_custom_labels": ("different",),
        }
        for kind in STORE_KINDS:
            for field, value in changes.items():
                with self.subTest(kind=kind, field=field):
                    with (
                        TemporaryDirectory() as directory,
                        audit_store(kind, Path(directory)) as store,
                    ):
                        original = lifecycle_audit(operation="relabel")
                        store.write_runner_lane_registration_audit_record(original)
                        with self.assertRaises(RunnerLaneRegistrationAuditIdentityConflict):
                            store.write_runner_lane_registration_audit_record(
                                lifecycle_audit(operation="relabel", request_changes={field: value})
                            )
                        self.assertEqual(
                            store.list_runner_lane_registration_audit_records(), (original,)
                        )
                        self.assertEqual(
                            store.list_runner_lane_registration_audit_records(
                                repository=original.request.repository,
                                host_name=original.request.host_name,
                                status=original.status,
                            ),
                            (original,),
                        )

    def test_original_request_allows_terminal_delivery_replay_and_mutable_evidence(self) -> None:
        terminal_statuses: tuple[RunnerLaneRegistrationAuditStatus, ...] = ("completed", "failed")
        for kind in STORE_KINDS:
            for operation in OPERATIONS:
                for status in terminal_statuses:
                    for terminal_first in (False, True):
                        with self.subTest(
                            kind=kind,
                            operation=operation,
                            status=status,
                            terminal_first=terminal_first,
                        ):
                            with (
                                TemporaryDirectory() as directory,
                                audit_store(kind, Path(directory)) as store,
                            ):
                                planned = lifecycle_audit(operation=operation)
                                terminal = lifecycle_audit(operation=operation, status=status)
                                if not terminal_first:
                                    store.write_runner_lane_registration_audit_record(planned)
                                store.write_runner_lane_registration_audit_record(terminal)
                                store.write_runner_lane_registration_audit_record(terminal)
                                updated = terminal.model_copy(
                                    update={"message": "retained original operation evidence"}
                                )
                                store.write_runner_lane_registration_audit_record(updated)
                                self.assertEqual(
                                    store.list_runner_lane_registration_audit_records(), (updated,)
                                )

    def test_normalized_request_is_the_identity(self) -> None:
        for kind in STORE_KINDS:
            with (
                self.subTest(kind=kind),
                TemporaryDirectory() as directory,
                audit_store(kind, Path(directory)) as store,
            ):
                original = lifecycle_audit()
                equivalent = lifecycle_audit(
                    status="completed",
                    request_changes={
                        "repository": " Example/Control ",
                        "host_name": "BUILD-1",
                        "lane_name": "GENERAL-1",
                        "labels": ("CANDIDATE", "general", "general"),
                        "registration_root": "/srv/runners/",
                    },
                )
                self.assertEqual(original.request, equivalent.request)
                store.write_runner_lane_registration_audit_record(original)
                store.write_runner_lane_registration_audit_record(equivalent)
                self.assertEqual(store.list_runner_lane_registration_audit_records(), (equivalent,))

    def test_filesystem_import_cannot_reassign_database_audit(self) -> None:
        with TemporaryDirectory() as directory, audit_store("sqlite", Path(directory)) as store:
            assert isinstance(store, PostgresRecordStore)
            source = FilesystemRecordStore(state_dir=Path(directory) / "source")
            original = lifecycle_audit()
            store.write_runner_lane_registration_audit_record(original)
            source.write_runner_lane_registration_audit_record(lifecycle_audit(operation="retire"))
            with self.assertRaises(RunnerLaneRegistrationAuditIdentityConflict):
                store.import_core_records_from_filesystem(source)
            self.assertEqual(store.list_runner_lane_registration_audit_records(), (original,))

    def test_retained_relabel_delivery_updates_original_audit_without_replaying_provider_write(
        self,
    ) -> None:
        for kind in STORE_KINDS:
            with (
                self.subTest(kind=kind),
                TemporaryDirectory() as directory,
                audit_store(kind, Path(directory)) as store,
            ):
                planned = lifecycle_audit(operation="relabel")
                transport = Transport()

                def persist(
                    record: RunnerLaneRegistrationAuditRecord, _key: str
                ) -> dict[str, object]:
                    store.write_runner_lane_registration_audit_record(record)
                    return {
                        "status": "accepted",
                        "records": {
                            "runner_lane_registration_audit_record_key": record.audit_record_key
                        },
                    }

                def interrupted_delivery(
                    record: RunnerLaneRegistrationAuditRecord, key: str
                ) -> dict[str, object]:
                    if record.status != "planned":
                        raise TimeoutError("terminal upload interrupted")
                    return persist(record, key)

                result = execute_relabel(
                    request=planned.request,
                    policy=RunnerLaneRegistrationPolicy(
                        allowed_repositories=(planned.request.repository,),
                        approved_hosts=(planned.request.host_name,),
                        allowed_registration_roots=(planned.request.registration_root,),
                    ),
                    transport=transport,
                    inventory_reader=lambda: planned.pre_inventory,
                    audit_poster=interrupted_delivery,
                    protected_lane_names=(),
                    protected_labels=(),
                )
                self.assertEqual(result["status"], "audit_delivery_pending")
                self.assertEqual(sum(call[0] == "PUT" for call in transport.calls), 1)
                calls_before_recovery = list(transport.calls)
                for _ in range(2):
                    receipt = deliver_terminal_audit(result, persist)
                    self.assertEqual(receipt["status"], "audit_delivered")
                terminal = RunnerLaneRegistrationAuditRecord.model_validate(
                    result["terminal_audit"]
                )
                self.assertEqual(terminal.request, planned.request)
                self.assertEqual(store.list_runner_lane_registration_audit_records(), (terminal,))
                self.assertEqual(transport.calls, calls_before_recovery)


class RunnerAuditHttpIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_or_absent_http_keys_cannot_reassign_audit(self) -> None:
        incoming_records = (
            lifecycle_audit(operation="retire"),
            lifecycle_audit(operation="relabel"),
            lifecycle_audit(request_changes={"labels": ("general", "different")}),
        )
        for kind in STORE_KINDS:
            for incoming in incoming_records:
                for incoming_key in ("different-upload-key", ""):
                    with self.subTest(
                        kind=kind, operation=incoming.request.operation, incoming_key=incoming_key
                    ):
                        with (
                            TemporaryDirectory() as directory,
                            audit_store(kind, Path(directory)) as store,
                        ):
                            original = lifecycle_audit()
                            app = create_launchplane_fastapi_app(
                                verifier=_StubVerifier(
                                    _runner_lane_registration_audit_write_identity()
                                ),
                                authz_policy=_runner_lane_registration_audit_write_policy(),
                                record_store_factory=lambda: store,
                                control_plane_root_path=Path(directory),
                            )
                            accepted = await _post_runner_lane_registration_audit_evidence(
                                app,
                                envelope(original),
                                idempotency_key="original-planned",
                            )
                            conflict = await _post_runner_lane_registration_audit_evidence(
                                app,
                                envelope(incoming),
                                idempotency_key=incoming_key,
                            )
                            self.assertEqual(accepted.status_code, 202)
                            self.assertEqual(conflict.status_code, 409)
                            self.assertNotIn("audit", conflict.json())
                            self.assertEqual(
                                store.list_runner_lane_registration_audit_records(), (original,)
                            )
                            terminal = lifecycle_audit(status="completed")
                            recovered = await _post_runner_lane_registration_audit_evidence(
                                app,
                                envelope(terminal),
                                idempotency_key=incoming_key,
                            )
                            self.assertEqual(recovered.status_code, 202)
                            replay = await _post_runner_lane_registration_audit_evidence(
                                app,
                                envelope(original),
                                idempotency_key="original-planned",
                            )
                            self.assertTrue(replay.json()["replayed"])
                            self.assertEqual(
                                store.list_runner_lane_registration_audit_records(), (terminal,)
                            )
