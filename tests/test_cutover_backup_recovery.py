from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch
import json
import io
import os
from contextlib import redirect_stdout

from control_plane.contracts.odoo_cutover import OdooCutoverRecord
from control_plane.dokploy.cutover_host import BoundOdooCutoverHost, BACKUP_MANIFEST_PROGRAM
from control_plane.dokploy.post_deploy import DEFAULT_ODOO_BACKUP_ROOT
from control_plane.odoo_cutover_plan import cutover_record_id
from tests.support.prepared_public_fixture import WebsiteFixture
from tests.test_odoo_cutover import release_plan
from tests.test_odoo_prod_backup_gate import (
    _passing_backup_capture_evidence,
    _passing_verification_evidence,
)


class CutoverBackupRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fixture = WebsiteFixture(Path(temporary.name))
        self.addCleanup(fixture.engine.dispose)
        self.plan = release_plan(fixture)
        self.backup_id = cutover_record_id(self.plan) + "-online"
        self.binding = {
            "record_id": self.backup_id,
            "nonce": "f" * 64,
            "verification_nonce": "e" * 64,
        }
        self.bindings: dict[str, Any] = {}
        self.manifest: dict[str, Any] = {"state": "absent"}
        self.capture = {
            **_passing_backup_capture_evidence(),
            "backup_record_id": self.backup_id,
            "database_name": self.plan.old.binding.database,
            "backup_nonce": self.binding["nonce"],
            "recovery_point_at": datetime.now(UTC).isoformat(),
        }

        def authority(key: str, desired: dict[str, Any] | None = None) -> dict[str, Any] | None:
            if desired is not None:
                self.bindings[key] = desired
            return self.bindings.get(key)

        def read(argv: tuple[str, ...], stdin: str | None) -> str:
            return json.dumps(self.manifest) if argv[1] == "exec" else str(self.capture["image_id"])

        self.transport = Mock()
        self.transport.read.side_effect = read
        records: dict[str, Any] = {}
        store = Mock()
        store.list_promotion_records.return_value = (
            SimpleNamespace(
                record_id="promotion-cutover-" + cutover_record_id(self.plan),
                backup_gate=SimpleNamespace(
                    evidence={"infrastructure_backup_record_id": "independent"}
                ),
            ),
        )
        store.write_backup_gate_record.side_effect = lambda record: records.update(
            {record.record_id: record}
        )
        store.read_backup_gate_record.side_effect = lambda record_id: records[record_id]
        record = OdooCutoverRecord(
            record_id=cutover_record_id(self.plan),
            plan=self.plan,
            step="backing_up",
            began_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.host = cast(
            BoundOdooCutoverHost,
            SimpleNamespace(
                guard=Mock(),
                plan=self.plan,
                record=lambda: record,
                store=store,
                state=SimpleNamespace(authority=authority),
                transport=self.transport,
                runtime=SimpleNamespace(
                    backup_root=DEFAULT_ODOO_BACKUP_ROOT,
                    authority_directory=temporary.name,
                    restore_image="postgres@sha256:" + "a" * 64,
                    operation_timeout_seconds=1800,
                    filestore_path="/volumes/data/filestore",
                ),
                runner=lambda slot: "runner",
                target=lambda slot, writer: SimpleNamespace(container="serving"),
                payloads={self.plan.old.slot_id: {"appName": "original"}},
            ),
        )
        self.enterContext(
            patch("control_plane.dokploy.cutover_host.require_production_promotion_backup")
        )
        self.enterContext(
            patch(
                "control_plane.dokploy.cutover_host.secrets.token_hex",
                side_effect=[self.binding["nonce"], self.binding["verification_nonce"]],
            )
        )

        self.enterContext(
            patch(
                "control_plane.dokploy.cutover_host.verify_disposable_database_restore",
                return_value={},
            )
        )
        self.extract = self.enterContext(
            patch(
                "control_plane.dokploy.cutover_host.post_deploy.extract_odoo_backup_gate_result",
                return_value=self.capture,
            )
        )
        verified = {
            **_passing_verification_evidence(),
            "verification_nonce": self.binding["verification_nonce"],
            "backup_record_id": self.backup_id,
            "database_name": self.plan.old.binding.database,
        }
        self.enterContext(
            patch(
                "control_plane.dokploy.cutover_host.post_deploy.extract_odoo_backup_verification_result",
                return_value=verified,
            )
        )

    def test_absent_capture_cancellation_atomically_fences_late_original_dispatch(self) -> None:
        for had_intent in (False, True):
            with self.subTest(had_intent=had_intent), TemporaryDirectory() as temporary:
                self.bindings.clear()
                if had_intent:
                    self.bindings["backup"] = self.binding
                self.host.runtime.backup_root = str(Path(temporary).resolve())

                def native(argv: tuple[str, ...], stdin: str | None) -> str:
                    output = io.StringIO()
                    with patch("sys.stdin", io.StringIO(stdin or "")), redirect_stdout(output):
                        exec(BACKUP_MANIFEST_PROGRAM, {})
                    return output.getvalue()

                self.transport.side_effect = native
                with patch(
                    "control_plane.dokploy.cutover_host.secrets.token_hex",
                    side_effect=[self.binding["nonce"], self.binding["verification_nonce"]],
                ):
                    BoundOdooCutoverHost.cancel_backup_restore(self.host, self.plan)
                binding = self.bindings["backup"]
                directory = (
                    Path(self.host.runtime.backup_root)
                    / self.plan.old.binding.database
                    / self.backup_id
                )
                claim = directory.with_name(directory.name + ".attempt.json")
                with self.assertRaises(FileExistsError):
                    os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self.assertEqual(
                    json.loads(claim.read_text()),
                    {"record_id": self.backup_id, "nonce": binding["nonce"], "cancelled": True},
                )
                BoundOdooCutoverHost.cancel_backup_restore(self.host, self.plan)

    def test_missing_legacy_or_inherited_lock_never_proves_capture_death(self) -> None:
        for inherited in (False, True):
            with self.subTest(inherited=inherited), TemporaryDirectory() as temporary:
                directory = Path(temporary).resolve() / "capture"
                directory.mkdir()
                (directory / "partial").write_bytes(b"inert unfinished capture")
                binding = {"record_id": self.backup_id, "nonce": self.binding["nonce"]}
                claim = directory.with_name(directory.name + ".attempt.json")
                claim.write_text(
                    json.dumps({**binding, **({"lock_inherited": True} if inherited else {})})
                )
                output = io.StringIO()
                with (
                    patch(
                        "sys.stdin",
                        io.StringIO(
                            json.dumps({"directory": str(directory), "cancel_absent": binding})
                        ),
                    ),
                    redirect_stdout(output),
                ):
                    exec(BACKUP_MANIFEST_PROGRAM, {})
                self.assertEqual(json.loads(output.getvalue()), {"state": "uncertain"})

    def test_denial_before_capture_dispatch_keeps_the_same_intent_replayable(self) -> None:
        self.transport.side_effect = [
            RuntimeError("original guard denied before send"),
            "capture",
            "verification",
        ]
        with self.assertRaises(RuntimeError):
            BoundOdooCutoverHost.backup(self.host, self.plan)
        self.extract.assert_not_called()
        self.assertEqual(self.bindings["backup"], self.binding)
        recovered = BoundOdooCutoverHost.backup(self.host, self.plan)
        self.assertEqual(recovered.logical_record_id, self.backup_id)
        self.assertEqual(self.bindings["backup"], self.binding)
        self.assertEqual(self.extract.call_count, 1)

    def test_failed_archive_verification_cancels_before_becoming_abortable(self) -> None:
        from control_plane.odoo_cutover_recovery import BackupVerificationRejected

        failed = {
            **_passing_verification_evidence(),
            "verification_status": "fail",
            "failure_code": "artifact_hash_mismatch",
            "verification_nonce": self.binding["verification_nonce"],
            "backup_record_id": self.backup_id,
            "database_name": self.plan.old.binding.database,
        }
        with (
            patch(
                "control_plane.dokploy.cutover_host.post_deploy.extract_odoo_backup_verification_result",
                return_value=failed,
            ),
            patch.object(self.host, "cancel_backup_restore", create=True) as cancel,
            self.assertRaises(BackupVerificationRejected),
        ):
            BoundOdooCutoverHost.backup(self.host, self.plan)
        cancel.assert_called_once_with(self.plan)

    def test_uncertain_or_foreign_capture_is_never_dispatched_again(self) -> None:
        for state in ("uncertain", "foreign-nonce", "missing-intent"):
            with self.subTest(state=state):
                self.bindings.clear()
                if state != "missing-intent":
                    self.bindings["backup"] = self.binding
                self.manifest = (
                    {"state": "uncertain"}
                    if state == "uncertain"
                    else {
                        "state": "captured",
                        "claim": {"record_id": self.backup_id, "nonce": self.binding["nonce"]},
                        "manifest": {
                            **self.capture,
                            "backup_nonce": "foreign"
                            if state == "foreign-nonce"
                            else self.binding["nonce"],
                        },
                    }
                )
                with self.assertRaises(ValueError):
                    BoundOdooCutoverHost.backup(self.host, self.plan)
                self.transport.assert_not_called()

    def test_only_matching_terminal_capture_failure_permits_abort(self) -> None:
        from control_plane.odoo_cutover_recovery import BackupVerificationRejected

        self.bindings["backup"] = self.binding
        self.manifest = {
            "state": "failed",
            "claim": {"record_id": self.backup_id, "nonce": self.binding["nonce"]},
        }
        with self.assertRaises(BackupVerificationRejected):
            BoundOdooCutoverHost.backup(self.host, self.plan)
        self.transport.assert_not_called()
        self.manifest["claim"]["nonce"] = "foreign"
        with self.assertRaises(ValueError) as caught:
            BoundOdooCutoverHost.backup(self.host, self.plan)
        self.assertNotIsInstance(caught.exception, BackupVerificationRejected)
        self.transport.assert_not_called()
