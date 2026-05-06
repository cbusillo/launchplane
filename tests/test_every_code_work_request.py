import unittest

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
    build_every_code_work_request_id,
    claim_every_code_work_request,
    close_every_code_work_request_for_pull_request,
)


def _queued_record() -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-code-123-test",
        source="manual",
        state="queued",
        repository="cbusillo/code",
        issue_number=123,
        issue_url="https://github.com/cbusillo/code/issues/123",
        trigger_label="every-code",
        queued_at="2026-05-05T22:00:00Z",
        updated_at="2026-05-05T22:00:00Z",
    )


class EveryCodeWorkRequestRecordTests(unittest.TestCase):
    def test_build_id_is_deterministic(self) -> None:
        first = build_every_code_work_request_id(
            repository="cbusillo/code",
            issue_number=123,
            trigger_label="every-code",
        )
        second = build_every_code_work_request_id(
            repository="CBUSILLO/CODE",
            issue_number=123,
            trigger_label="EVERY-CODE",
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("every-code-cbusillo-code-123-"))

    def test_claim_requires_queued_state(self) -> None:
        queued_record = _queued_record()
        claimed_record = claim_every_code_work_request(
            queued_record,
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )

        self.assertIsNotNone(claimed_record)
        assert claimed_record is not None
        self.assertEqual(claimed_record.state, "claimed")
        self.assertEqual(claimed_record.claimed_by_host, "Chris-Studio")
        self.assertIsNone(
            claim_every_code_work_request(
                claimed_record,
                host="Other-Host",
                claimed_at="2026-05-05T22:02:00Z",
            )
        )

    def test_status_update_enforces_claim_host(self) -> None:
        claimed_record = claim_every_code_work_request(
            _queued_record(),
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed_record is not None

        with self.assertRaises(ValueError):
            apply_every_code_work_request_status(
                claimed_record,
                EveryCodeWorkRequestStatusUpdate(
                    state="running",
                    host="Other-Host",
                    updated_at="2026-05-05T22:02:00Z",
                ),
            )

    def test_done_status_sets_started_and_finished_when_worker_finishes_directly(self) -> None:
        claimed_record = claim_every_code_work_request(
            _queued_record(),
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed_record is not None

        done_record = apply_every_code_work_request_status(
            claimed_record,
            EveryCodeWorkRequestStatusUpdate(
                state="done",
                host="Chris-Studio",
                updated_at="2026-05-05T22:03:00Z",
                result_pr_url="https://github.com/cbusillo/code/pull/99",
            ),
        )

        self.assertEqual(done_record.state, "done")
        self.assertEqual(done_record.started_at, "2026-05-05T22:03:00Z")
        self.assertEqual(done_record.finished_at, "2026-05-05T22:03:00Z")
        self.assertEqual(done_record.result_pr_url, "https://github.com/cbusillo/code/pull/99")

    def test_pr_close_marks_matching_running_request_done(self) -> None:
        claimed_record = claim_every_code_work_request(
            _queued_record(),
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed_record is not None
        running_record = apply_every_code_work_request_status(
            claimed_record,
            EveryCodeWorkRequestStatusUpdate(
                state="running",
                host="Chris-Studio",
                updated_at="2026-05-05T22:03:00Z",
                result_pr_url="https://github.com/cbusillo/code/pull/99",
            ),
        )

        done_record = close_every_code_work_request_for_pull_request(
            running_record,
            pr_url="https://github.com/cbusillo/code/pull/99",
            merged=True,
            closed_at="2026-05-05T22:05:00Z",
        )

        self.assertIsNotNone(done_record)
        assert done_record is not None
        self.assertEqual(done_record.state, "done")
        self.assertEqual(done_record.finished_at, "2026-05-05T22:05:00Z")
        self.assertEqual(done_record.error_message, "")
        self.assertIsNone(
            close_every_code_work_request_for_pull_request(
                done_record,
                pr_url="https://github.com/cbusillo/code/pull/99",
                merged=True,
                closed_at="2026-05-05T22:06:00Z",
            )
        )

    def test_pr_close_marks_running_request_done_without_stored_pr_url(self) -> None:
        claimed_record = claim_every_code_work_request(
            _queued_record(),
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed_record is not None
        running_record = apply_every_code_work_request_status(
            claimed_record,
            EveryCodeWorkRequestStatusUpdate(
                state="running",
                host="Chris-Studio",
                updated_at="2026-05-05T22:03:00Z",
            ),
        )

        done_record = close_every_code_work_request_for_pull_request(
            running_record,
            pr_url="https://github.com/cbusillo/code/pull/99",
            merged=True,
            closed_at="2026-05-05T22:05:00Z",
        )

        self.assertIsNotNone(done_record)
        assert done_record is not None
        self.assertEqual(done_record.state, "done")
        self.assertEqual(done_record.result_pr_url, "https://github.com/cbusillo/code/pull/99")

    def test_pr_close_does_not_match_different_stored_pr_url(self) -> None:
        claimed_record = claim_every_code_work_request(
            _queued_record(),
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed_record is not None
        running_record = apply_every_code_work_request_status(
            claimed_record,
            EveryCodeWorkRequestStatusUpdate(
                state="running",
                host="Chris-Studio",
                updated_at="2026-05-05T22:03:00Z",
                result_pr_url="https://github.com/cbusillo/code/pull/88",
            ),
        )

        self.assertIsNone(
            close_every_code_work_request_for_pull_request(
                running_record,
                pr_url="https://github.com/cbusillo/code/pull/99",
                merged=True,
                closed_at="2026-05-05T22:05:00Z",
            )
        )

    def test_blocked_requires_error_message(self) -> None:
        with self.assertRaises(ValueError):
            EveryCodeWorkRequestRecord(
                request_id="every-code-cbusillo-code-123-test",
                source="manual",
                state="blocked",
                repository="cbusillo/code",
                issue_number=123,
                issue_url="https://github.com/cbusillo/code/issues/123",
                trigger_label="every-code",
                queued_at="2026-05-05T22:00:00Z",
                updated_at="2026-05-05T22:03:00Z",
                claimed_at="2026-05-05T22:01:00Z",
                claimed_by_host="Chris-Studio",
                finished_at="2026-05-05T22:03:00Z",
            )


if __name__ == "__main__":
    unittest.main()
