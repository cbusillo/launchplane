from __future__ import annotations

import unittest

from control_plane.contracts.every_code_summary_read_model import (
    build_every_code_summary_read_model,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord


def _record(**overrides: object) -> EveryCodeWorkRequestRecord:
    payload: dict[str, object] = {
        "request_id": "every-code-cbusillo-launchplane-190",
        "source": "github_issue_label",
        "state": "queued",
        "repository": "cbusillo/launchplane",
        "issue_number": 190,
        "issue_url": "https://github.com/cbusillo/launchplane/issues/190",
        "issue_title": "Build What To Work On Next cockpit",
        "trigger_label": "every-code",
        "trigger_actor": "cbusillo",
        "github_delivery_id": "delivery-190",
        "queued_at": "2026-05-06T02:00:00Z",
        "updated_at": "2026-05-06T02:00:00Z",
    }
    payload.update(overrides)
    return EveryCodeWorkRequestRecord.model_validate(payload)


class _Store:
    def __init__(self, records: tuple[EveryCodeWorkRequestRecord, ...]) -> None:
        self.records = records

    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]:
        records = tuple(
            record
            for record in self.records
            if (not state or record.state == state)
            and (not repository or record.repository == repository)
        )
        if offset:
            records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return records


class EveryCodeSummaryReadModelTests(unittest.TestCase):
    def test_summary_projects_compact_agent_safe_fields(self) -> None:
        read_model = build_every_code_summary_read_model(
            generated_at="2026-05-08T18:25:00Z",
            record_store=_Store(
                (
                    _record(),
                    _record(
                        request_id="every-code-cbusillo-launchplane-191",
                        state="blocked",
                        issue_number=191,
                        issue_url="https://github.com/cbusillo/launchplane/issues/191",
                        claimed_at="2026-05-06T02:01:00Z",
                        claimed_by_host="Chris-Studio.local",
                        started_at="2026-05-06T02:02:00Z",
                        finished_at="2026-05-06T02:08:00Z",
                        updated_at="2026-05-06T02:08:00Z",
                        error_message="/Users/chris/private checkout failed",
                    ),
                    _record(
                        request_id="every-code-cbusillo-launchplane-192",
                        state="done",
                        issue_number=192,
                        issue_url="https://github.com/cbusillo/launchplane/issues/192",
                        claimed_at="2026-05-06T02:01:00Z",
                        claimed_by_host="local-mac",
                        started_at="2026-05-06T02:02:00Z",
                        finished_at="2026-05-06T02:08:00Z",
                        updated_at="2026-05-06T02:08:00Z",
                        result_pr_url="https://github.com/cbusillo/launchplane/pull/200",
                        result_summary="PR opened with focused implementation.",
                    ),
                )
            ),
        )

        summaries = {summary.issue_number: summary for summary in read_model.summaries}
        self.assertEqual(summaries[190].summary_status, "active")
        self.assertFalse(summaries[190].safe_to_rerun)
        self.assertEqual(summaries[191].summary_status, "stuck")
        self.assertTrue(summaries[191].safe_to_rerun)
        self.assertEqual(summaries[191].claimed_by_host, "claimed_local_worker")
        self.assertEqual(summaries[191].provenance.source_record_id, summaries[191].request_id)
        self.assertIn("work_request_record", {entry.code for entry in summaries[191].evidence})
        self.assertNotIn("private", summaries[191].model_dump_json())
        self.assertEqual(summaries[192].summary_status, "complete")
        self.assertEqual(summaries[192].result_pr_url, "https://github.com/cbusillo/launchplane/pull/200")
        self.assertEqual(summaries[192].result_summary, "PR opened with focused implementation.")

    def test_summary_supports_repo_issue_and_state_filters(self) -> None:
        read_model = build_every_code_summary_read_model(
            generated_at="2026-05-08T18:25:00Z",
            record_store=_Store(
                (
                    _record(repository="cbusillo/launchplane", issue_number=190),
                    _record(
                        request_id="every-code-cbusillo-code-12",
                        repository="cbusillo/code",
                        issue_number=12,
                        issue_url="https://github.com/cbusillo/code/issues/12",
                        state="done",
                        claimed_at="2026-05-06T02:01:00Z",
                        claimed_by_host="local-mac",
                        started_at="2026-05-06T02:02:00Z",
                        finished_at="2026-05-06T02:08:00Z",
                        updated_at="2026-05-06T02:08:00Z",
                    ),
                )
            ),
            repository="cbusillo/code",
            issue_number=12,
            state="done",
        )

        self.assertEqual(read_model.repository, "cbusillo/code")
        self.assertEqual(read_model.issue_number, 12)
        self.assertEqual(read_model.state_filter, "done")
        self.assertEqual(len(read_model.summaries), 1)
        self.assertEqual(read_model.summaries[0].repository, "cbusillo/code")
        self.assertEqual(read_model.summaries[0].issue_number, 12)

    def test_invalid_state_filter_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "state filter is invalid"):
            build_every_code_summary_read_model(
                generated_at="2026-05-08T18:25:00Z",
                record_store=_Store(()),
                state="secret",
            )


if __name__ == "__main__":
    unittest.main()
