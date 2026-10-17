from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli_runner_relabel import runner_lane_relabel_executor

from control_plane.contracts.runner_lane_inventory import (
    RunnerLaneInventory,
    build_runner_lane_inventory,
    RunnerLaneRecord,
)
from control_plane.contracts.runner_lane_registration import (
    RunnerLaneRegistrationAuditRecord,
    RunnerLaneRegistrationPolicy,
    RunnerLaneRegistrationRequest,
)
from control_plane.workflows.runner_lane_relabel import execute_relabel


def inventory(
    *, name: str = "general-1", runner_id: int = 7, busy: bool = False
) -> RunnerLaneInventory:
    return build_runner_lane_inventory(
        repository="example/control",
        observed_at="2026-10-16T23:00:00Z",
        lanes=(
            RunnerLaneRecord(
                repository="example/control",
                name=name,
                github_id=runner_id,
                status="online",
                busy=busy,
                labels=("self-hosted", "general"),
                observed_at="2026-10-16T23:00:00Z",
            ),
        ),
    )


def labels(
    *custom: str, automatic: tuple[str, ...] = ("self-hosted", "linux")
) -> dict[str, object]:
    return {
        "labels": [
            *({"name": name, "type": "custom"} for name in custom),
            *({"name": name, "type": "read-only"} for name in automatic),
        ]
    }


class Transport:
    def __init__(self, *, unknown_write: bool = False, wrong_after: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.unknown_write = unknown_write
        self.wrong_after = wrong_after
        self.written = False

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        self.calls.append((method, path, body))
        if method == "PUT":
            self.written = True
            if self.unknown_write:
                raise TimeoutError("response lost after provider accepted write")
            return labels("general", "candidate")
        if self.written:
            return labels("foreign" if self.wrong_after else "candidate", "general")
        return labels("general")


class RunnerRelabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = RunnerLaneRegistrationRequest(
            operation="relabel",
            repository="example/control",
            host_name="build-1",
            lane_name="general-1",
            registration_root="/srv/runners",
            labels=("general", "candidate"),
            expected_runner_id=7,
            expected_custom_labels=("general",),
            audit_record_key="runner-label/test/apply",
            mutate=True,
        )
        self.policy = RunnerLaneRegistrationPolicy(
            allowed_repositories=("example/control",),
            approved_hosts=("build-1",),
            allowed_registration_roots=("/srv/runners",),
        )
        self.transport = Transport()
        self.audits: list[RunnerLaneRegistrationAuditRecord] = []

    def audit(self, record: RunnerLaneRegistrationAuditRecord, _key: str) -> dict[str, object]:
        self.audits.append(record)
        return {
            "status": "accepted",
            "records": {"runner_lane_registration_audit_record_key": self.request.audit_record_key},
        }

    def run_change(self, *, protected_lane_names: tuple[str, ...] = ()) -> dict[str, object]:
        return execute_relabel(
            request=self.request,
            policy=self.policy,
            transport=self.transport,
            inventory_reader=inventory,
            audit_poster=self.audit,
            protected_lane_names=protected_lane_names,
        )

    def test_replaces_only_custom_labels_after_audit_and_fresh_review(self) -> None:
        reads = 0

        def read() -> RunnerLaneInventory:
            nonlocal reads
            reads += 1
            if reads == 2:
                self.assertEqual([record.status for record in self.audits], ["planned"])
                self.assertFalse(self.transport.written)
            return inventory()

        result = execute_relabel(
            request=self.request,
            policy=self.policy,
            transport=self.transport,
            inventory_reader=read,
            audit_poster=self.audit,
        )
        self.assertEqual(result["status"], "completed")
        writes = [call for call in self.transport.calls if call[0] != "GET"]
        self.assertEqual(
            writes,
            [
                (
                    "PUT",
                    "/repos/example/control/actions/runners/7/labels",
                    {"labels": ["candidate", "general"]},
                )
            ],
        )
        self.assertEqual(self.audits[-1].status, "completed")
        self.assertEqual(result["before_custom_labels"], ("general",))
        self.assertEqual(result["automatic_labels"], ("linux", "self-hosted"))

    def test_dry_run_never_writes_labels(self) -> None:
        self.request = self.request.model_copy(update={"mutate": False})
        result = self.run_change()
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(self.transport.written)

    def test_stale_or_foreign_or_busy_identity_refuses_before_write(self) -> None:
        readers: tuple[Callable[[], RunnerLaneInventory], ...] = (
            lambda: inventory(runner_id=8),
            lambda: inventory(name="other"),
            lambda: inventory(busy=True),
        )
        for reader in readers:
            with self.subTest(reader=reader), self.assertRaises(ValueError):
                execute_relabel(
                    request=self.request,
                    policy=self.policy,
                    transport=self.transport,
                    inventory_reader=reader,
                    audit_poster=self.audit,
                )
        self.assertFalse(self.transport.written)

    def test_changed_labels_and_automatic_labels_refuse(self) -> None:
        for changes in ({"expected_custom_labels": ("old",)}, {"labels": ("linux",)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                execute_relabel(
                    request=self.request.model_copy(update=changes),
                    policy=self.policy,
                    transport=self.transport,
                    inventory_reader=inventory,
                    audit_poster=self.audit,
                )
        self.assertFalse(self.transport.written)

    def test_failed_audit_or_changed_review_during_audit_never_writes(self) -> None:
        def fail_audit(_record: RunnerLaneRegistrationAuditRecord, _key: str) -> dict[str, object]:
            raise TimeoutError()

        with self.assertRaises(TimeoutError):
            execute_relabel(
                request=self.request,
                policy=self.policy,
                transport=self.transport,
                inventory_reader=inventory,
                audit_poster=fail_audit,
            )
        reads = 0

        def changed() -> RunnerLaneInventory:
            nonlocal reads
            reads += 1
            return inventory(runner_id=7 if reads == 1 else 8)

        with self.assertRaises(ValueError):
            execute_relabel(
                request=self.request,
                policy=self.policy,
                transport=self.transport,
                inventory_reader=changed,
                audit_poster=self.audit,
            )
        self.assertFalse(self.transport.written)

    def test_uncertain_write_is_not_retried_and_is_audited(self) -> None:
        for transport in (Transport(unknown_write=True), Transport(wrong_after=True)):
            with self.subTest(transport=transport):
                result = execute_relabel(
                    request=self.request,
                    policy=self.policy,
                    transport=transport,
                    inventory_reader=inventory,
                    audit_poster=self.audit,
                )
                self.assertEqual(result["status"], "mutation_reconciliation_required")
                self.assertEqual(sum(call[0] == "PUT" for call in transport.calls), 1)
                self.assertEqual(self.audits[-1].status, "failed")

    def test_terminal_audit_failure_retains_packet_and_actual_outcome(self) -> None:
        def audit(record: RunnerLaneRegistrationAuditRecord, _key: str) -> dict[str, object]:
            if record.status != "planned":
                raise TimeoutError()
            return {
                "status": "accepted",
                "records": {
                    "runner_lane_registration_audit_record_key": self.request.audit_record_key
                },
            }

        result = execute_relabel(
            request=self.request,
            policy=self.policy,
            transport=self.transport,
            inventory_reader=inventory,
            audit_poster=audit,
        )
        self.assertEqual(result["status"], "audit_delivery_pending")
        self.assertEqual(result["mutation_status"], "completed")
        self.assertIsInstance(result["terminal_audit"], dict)

    def test_operations_lane_is_preserved(self) -> None:
        with self.assertRaises(ValueError):
            self.run_change(protected_lane_names=("general-1",))
        self.assertFalse(self.transport.written)

    def test_duplicate_custom_labels_are_rejected_in_review_request(self) -> None:
        with self.assertRaises(ValueError):
            RunnerLaneRegistrationRequest.model_validate(
                {
                    **self.request.model_dump(),
                    "labels": ["general", "GENERAL"],
                }
            )

    def test_cli_dry_run_and_service_audit_requirement(self) -> None:
        with TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.json"
            args = [
                "--request-file",
                str(request_path),
                "--allowed-repository",
                self.request.repository,
                "--approved-host",
                self.request.host_name,
                "--allowed-registration-root",
                self.request.registration_root,
                "--execution-lane",
                "operations",
                "--service-user",
                "runner-user",
            ]
            request_path.write_text(self.request.model_dump_json())
            with patch(
                "control_plane.cli_runner_relabel.UrllibMergeTrainGitHubTransport"
            ) as create:
                result = CliRunner().invoke(runner_lane_relabel_executor, [*args, "--mutate"])
                self.assertNotEqual(result.exit_code, 0)
                create.assert_not_called()
            request_path.write_text(
                self.request.model_copy(update={"mutate": False}).model_dump_json()
            )
            with (
                patch(
                    "control_plane.cli_runner_relabel.UrllibMergeTrainGitHubTransport",
                    return_value=self.transport,
                ),
                patch("control_plane.cli_runner_relabel.GitHubRunnerLaneInventoryReader") as reader,
            ):
                reader.return_value.read_runner_lane_inventory.return_value = inventory()
                result = CliRunner().invoke(
                    runner_lane_relabel_executor,
                    args,
                    env={"GITHUB_TOKEN": "fixture-token"},
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertFalse(self.transport.written)
