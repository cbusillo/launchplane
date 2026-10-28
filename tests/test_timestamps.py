from datetime import UTC, datetime
import unittest
from unittest.mock import MagicMock, patch

from control_plane.timestamps import utc_now_timestamp


class UtcNowTimestampTests(unittest.TestCase):
    @patch("control_plane.timestamps.datetime", wraps=datetime)
    def test_formats_utc_at_whole_second_precision(self, datetime_mock: MagicMock) -> None:
        datetime_mock.now.return_value = datetime(2026, 10, 28, 4, 5, 6, 789012, tzinfo=UTC)

        self.assertEqual(utc_now_timestamp(), "2026-10-28T04:05:06Z")
        datetime_mock.now.assert_called_with(UTC)


if __name__ == "__main__":
    unittest.main()
