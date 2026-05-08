from __future__ import annotations

import unittest

from control_plane.contracts.every_code_preview_gate_record import (
    EveryCodePreviewGateRecord,
    build_every_code_preview_gate_id,
)
from control_plane.contracts.preview_readiness_read_model import (
    build_preview_readiness_read_model,
)


def _gate(
    *,
    status: str = "pending",
    head_sha: str = "abcdef1234567890",
    pr_number: int = 86,
    blocked_reason: str = "",
    pending_reason: str = "",
    check_summary: str = "",
) -> EveryCodePreviewGateRecord:
    return EveryCodePreviewGateRecord(
        gate_id=build_every_code_preview_gate_id(
            repository="cbusillo/sellyouroutboard",
            pr_number=pr_number,
            head_sha=head_sha,
        ),
        request_id="every-code-cbusillo-sellyouroutboard-123-test",
        repository="cbusillo/sellyouroutboard",
        issue_number=123,
        issue_url="https://github.com/cbusillo/sellyouroutboard/issues/123",
        issue_author="Mbanks89",
        pr_number=pr_number,
        pr_url=f"https://github.com/cbusillo/sellyouroutboard/pull/{pr_number}",
        head_sha=head_sha,
        status=status,  # type: ignore[arg-type]
        created_at="2026-05-06T20:00:00Z",
        updated_at="2026-05-06T20:01:00Z",
        ready_at="2026-05-06T20:02:00Z" if status == "ready" else "",
        labeled_at="2026-05-06T20:03:00Z" if status == "labeled" else "",
        blocked_at="2026-05-06T20:04:00Z" if status == "blocked" else "",
        cancelled_at="2026-05-06T20:05:00Z" if status == "cancelled" else "",
        last_checked_at="2026-05-06T20:01:00Z",
        pending_reason=pending_reason,
        blocked_reason=blocked_reason,
        check_summary=check_summary,
    )


class _Store:
    def __init__(self, gates: tuple[EveryCodePreviewGateRecord, ...]) -> None:
        self.gates = gates

    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]:
        gates = tuple(
            gate
            for gate in self.gates
            if (not request_id or gate.request_id == request_id)
            and (not repository or gate.repository == repository)
            and (pr_number is None or gate.pr_number == pr_number)
            and (not status or gate.status == status)
        )
        if offset:
            gates = gates[offset:]
        if limit is not None:
            gates = gates[:limit]
        return gates


class PreviewReadinessReadModelTests(unittest.TestCase):
    def test_preview_readiness_maps_gate_statuses_to_agent_statuses(self) -> None:
        read_model = build_preview_readiness_read_model(
            generated_at="2026-05-08T18:35:00Z",
            record_store=_Store(
                (
                    _gate(status="pending", pending_reason="Waiting on static_checks."),
                    _gate(status="ready", pr_number=87),
                    _gate(status="labeled", pr_number=88),
                    _gate(
                        status="blocked",
                        pr_number=89,
                        blocked_reason="static_checks failed.",
                    ),
                    _gate(
                        status="cancelled",
                        pr_number=90,
                        blocked_reason="Source issue closed.",
                    ),
                )
            ),
        )

        by_pr = {item.pr_number: item for item in read_model.items}
        self.assertEqual(by_pr[86].readiness_status, "waiting_on_checks")
        self.assertEqual(by_pr[86].freshness_status, "recorded")
        self.assertFalse(by_pr[86].safe_to_request_preview)
        self.assertEqual(by_pr[87].readiness_status, "ready")
        self.assertTrue(by_pr[87].safe_to_request_preview)
        self.assertEqual(by_pr[88].readiness_status, "ready")
        self.assertEqual(by_pr[89].readiness_status, "needs_attention")
        self.assertTrue(by_pr[89].needs_operator_attention)
        self.assertEqual(by_pr[90].readiness_status, "cancelled")
        self.assertEqual(by_pr[90].terminal_at, "2026-05-06T20:05:00Z")

    def test_preview_readiness_supports_repo_pr_and_status_filters(self) -> None:
        read_model = build_preview_readiness_read_model(
            generated_at="2026-05-08T18:35:00Z",
            record_store=_Store((_gate(status="blocked", blocked_reason="Timed out."),)),
            repository="cbusillo/sellyouroutboard",
            pr_number=86,
            status="blocked",
        )

        self.assertEqual(read_model.repository, "cbusillo/sellyouroutboard")
        self.assertEqual(read_model.pr_number, 86)
        self.assertEqual(read_model.status_filter, "blocked")
        self.assertEqual(len(read_model.items), 1)
        self.assertEqual(read_model.items[0].detail, "Timed out.")

    def test_invalid_status_filter_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "status filter is invalid"):
            build_preview_readiness_read_model(
                generated_at="2026-05-08T18:35:00Z",
                record_store=_Store(()),
                status="secret",
            )


if __name__ == "__main__":
    unittest.main()
