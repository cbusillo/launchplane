import unittest

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
    build_every_code_work_request_id,
    claim_every_code_work_request,
    close_every_code_work_request_for_issue,
    close_every_code_work_request_for_pull_request,
    requeue_every_code_work_request,
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
                    fencing_token=claimed_record.fencing_token,
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
                fencing_token=claimed_record.fencing_token,
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
                fencing_token=claimed_record.fencing_token,
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

    def test_issue_close_marks_running_request_done(self) -> None:
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
                fencing_token=claimed_record.fencing_token,
                updated_at="2026-05-05T22:03:00Z",
            ),
        )

        done_record = close_every_code_work_request_for_issue(
            running_record,
            closed_at="2026-05-05T22:05:00Z",
            reason="completed",
        )

        self.assertIsNotNone(done_record)
        assert done_record is not None
        self.assertEqual(done_record.state, "done")
        self.assertEqual(done_record.finished_at, "2026-05-05T22:05:00Z")
        self.assertEqual(done_record.error_message, "")
        self.assertEqual(
            done_record.result_summary,
            "Source issue closed (completed): https://github.com/cbusillo/code/issues/123",
        )
        self.assertIsNone(
            close_every_code_work_request_for_issue(
                done_record,
                closed_at="2026-05-05T22:06:00Z",
                reason="completed",
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
                fencing_token=claimed_record.fencing_token,
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

    def test_pr_close_marks_matching_queued_request_done(self) -> None:
        done_record = close_every_code_work_request_for_pull_request(
            _queued_record(),
            pr_url="https://github.com/cbusillo/code/pull/99",
            merged=True,
            closed_at="2026-05-05T22:05:00Z",
        )

        self.assertIsNotNone(done_record)
        assert done_record is not None
        self.assertEqual(done_record.state, "done")
        self.assertEqual(done_record.claimed_at, "2026-05-05T22:05:00Z")
        self.assertEqual(done_record.claimed_by_host, "github-pull-request-close")
        self.assertEqual(done_record.started_at, "2026-05-05T22:05:00Z")
        self.assertEqual(done_record.finished_at, "2026-05-05T22:05:00Z")
        self.assertEqual(done_record.result_pr_url, "https://github.com/cbusillo/code/pull/99")
        self.assertEqual(
            done_record.result_summary,
            "Linked pull request merged: https://github.com/cbusillo/code/pull/99",
        )

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
                fencing_token=claimed_record.fencing_token,
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

    def test_pr_close_enriches_matching_terminal_request_without_changing_execution_outcome(
        self,
    ) -> None:
        claimed_record = claim_every_code_work_request(
            _queued_record(),
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed_record is not None
        pr_url = "https://github.com/cbusillo/code/pull/99"
        for state, merged in (
            ("done", True),
            ("done", False),
            ("blocked", True),
            ("blocked", False),
        ):
            with self.subTest(state=state, merged=merged):
                error_message = "Worker stopped." if state == "blocked" else ""
                terminal_record = apply_every_code_work_request_status(
                    claimed_record,
                    EveryCodeWorkRequestStatusUpdate(
                        state=state,  # type: ignore[arg-type]
                        host="Chris-Studio",
                        fencing_token=claimed_record.fencing_token,
                        updated_at="2026-05-05T22:03:00Z",
                        result_pr_url=pr_url,
                        result_summary="Opened PR.",
                        error_message=error_message,
                    ),
                )

                closed_record = close_every_code_work_request_for_pull_request(
                    terminal_record,
                    pr_url=pr_url,
                    merged=merged,
                    closed_at="2026-05-05T22:05:00Z",
                )

                self.assertIsNotNone(closed_record)
                assert closed_record is not None
                self.assertEqual(closed_record.state, state)
                self.assertEqual(closed_record.finished_at, terminal_record.finished_at)
                self.assertEqual(closed_record.error_message, terminal_record.error_message)
                self.assertEqual(closed_record.fencing_token, terminal_record.fencing_token)
                self.assertEqual(closed_record.attempt, terminal_record.attempt)
                self.assertEqual(closed_record.updated_at, "2026-05-05T22:05:00Z")
                expected_disposition = "merged" if merged else "closed without merge"
                self.assertEqual(
                    closed_record.result_summary,
                    f"Linked pull request {expected_disposition}: {pr_url}\nOpened PR.",
                )

    def test_pr_close_terminal_enrichment_keeps_newer_updated_at(self) -> None:
        terminal_record = _queued_record().model_copy(
            update={
                "state": "done",
                "claimed_at": "2026-05-05T22:01:00Z",
                "claimed_by_host": "Chris-Studio",
                "fencing_token": 4,
                "attempt": 4,
                "started_at": "2026-05-05T22:02:00Z",
                "finished_at": "2026-05-05T22:03:00Z",
                "updated_at": "2026-05-05T22:07:00Z",
                "result_pr_url": "https://github.com/cbusillo/code/pull/99",
                "result_summary": "Opened PR.",
            }
        )

        closed_record = close_every_code_work_request_for_pull_request(
            terminal_record,
            pr_url="https://github.com/cbusillo/code/pull/99",
            merged=True,
            closed_at="2026-05-05T22:02:30Z",
        )

        self.assertIsNotNone(closed_record)
        assert closed_record is not None
        self.assertEqual(closed_record.updated_at, "2026-05-05T22:07:00Z")
        self.assertEqual(
            closed_record.result_summary,
            "Linked pull request merged: https://github.com/cbusillo/code/pull/99\nOpened PR.",
        )

    def test_pr_close_does_not_enrich_terminal_request_without_exact_stored_pr(self) -> None:
        terminal_record = _queued_record().model_copy(
            update={
                "state": "done",
                "claimed_at": "2026-05-05T22:01:00Z",
                "claimed_by_host": "Chris-Studio",
                "started_at": "2026-05-05T22:02:00Z",
                "finished_at": "2026-05-05T22:03:00Z",
            }
        )
        for stored_pr_url in ("", "https://github.com/cbusillo/code/pull/88"):
            with self.subTest(stored_pr_url=stored_pr_url):
                self.assertIsNone(
                    close_every_code_work_request_for_pull_request(
                        terminal_record.model_copy(update={"result_pr_url": stored_pr_url}),
                        pr_url="https://github.com/cbusillo/code/pull/99",
                        merged=True,
                        closed_at="2026-05-05T22:05:00Z",
                    )
                )

    def test_pr_close_duplicate_terminal_closure_is_noop_for_both_dispositions(self) -> None:
        pr_url = "https://github.com/cbusillo/code/pull/99"
        terminal_record = _queued_record().model_copy(
            update={
                "state": "done",
                "claimed_at": "2026-05-05T22:01:00Z",
                "claimed_by_host": "Chris-Studio",
                "started_at": "2026-05-05T22:02:00Z",
                "finished_at": "2026-05-05T22:03:00Z",
                "result_pr_url": pr_url,
            }
        )
        for summary in (
            f"Linked pull request merged: {pr_url}",
            f"Linked pull request closed without merge: {pr_url}",
        ):
            with self.subTest(summary=summary):
                self.assertIsNone(
                    close_every_code_work_request_for_pull_request(
                        terminal_record.model_copy(update={"result_summary": summary}),
                        pr_url=pr_url,
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

    def test_requeue_terminal_request_clears_worker_state(self) -> None:
        claimed_record = claim_every_code_work_request(
            _queued_record(),
            host="Chris-Studio",
            claimed_at="2026-05-05T22:01:00Z",
        )
        assert claimed_record is not None
        blocked_record = apply_every_code_work_request_status(
            claimed_record,
            EveryCodeWorkRequestStatusUpdate(
                state="blocked",
                host="Chris-Studio",
                fencing_token=claimed_record.fencing_token,
                updated_at="2026-05-05T22:03:00Z",
                result_pr_url="https://github.com/cbusillo/code/pull/99",
                result_summary="Old session went stale.",
                error_message="Old session went stale.",
            ),
        )

        requeued_record = requeue_every_code_work_request(
            blocked_record,
            queued_at="2026-05-05T22:07:00Z",
            trigger_actor="cbusillo",
        )

        self.assertEqual(requeued_record.state, "queued")
        self.assertEqual(requeued_record.trigger_actor, "cbusillo")
        self.assertEqual(requeued_record.queued_at, "2026-05-05T22:07:00Z")
        self.assertEqual(requeued_record.updated_at, "2026-05-05T22:07:00Z")
        self.assertEqual(requeued_record.claimed_at, "")
        self.assertEqual(requeued_record.claimed_by_host, "")
        self.assertEqual(requeued_record.started_at, "")
        self.assertEqual(requeued_record.finished_at, "")
        self.assertEqual(requeued_record.result_pr_url, "")
        self.assertEqual(requeued_record.result_summary, "")
        self.assertEqual(requeued_record.error_message, "")

    def test_requeue_requires_terminal_request(self) -> None:
        with self.assertRaises(ValueError):
            requeue_every_code_work_request(
                _queued_record(),
                queued_at="2026-05-05T22:07:00Z",
            )


if __name__ == "__main__":
    unittest.main()
