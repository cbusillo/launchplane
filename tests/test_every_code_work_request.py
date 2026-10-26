import unittest

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
)


class EveryCodeWorkRequestRecordTests(unittest.TestCase):
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
