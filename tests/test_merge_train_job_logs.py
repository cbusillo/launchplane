"""The recovery reader never forwards credentials into signed log downloads."""

from email.message import Message
from io import BytesIO
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from control_plane.merge_train_job_logs import MAX_JOB_LOG_BYTES, read_job_log


class JobLogReadTests(unittest.TestCase):
    def test_signed_storage_request_drops_auth_and_is_bounded(self) -> None:
        headers = Message()
        headers["Location"] = "https://results.blob.core.windows.net/job/log?signature=fixture"
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b"log evidence"
        opener = Mock()
        opener.open.side_effect = [
            HTTPError("https://api.github.com/job/logs", 302, "Found", headers, BytesIO()),
            response,
        ]
        request = Request(
            "https://api.github.com/job/logs", headers={"Authorization": "Bearer fixture"}
        )
        with patch("control_plane.merge_train_job_logs.build_opener", return_value=opener):
            self.assertEqual(read_job_log(request), "log evidence")
        storage_request = opener.open.call_args_list[1].args[0]
        self.assertIsNone(storage_request.get_header("Authorization"))
        response.read.assert_called_once_with(MAX_JOB_LOG_BYTES + 1)
        response.read.return_value = b"x" * (MAX_JOB_LOG_BYTES + 1)
        opener.open.side_effect = [
            HTTPError(request.full_url, 302, "Found", headers, BytesIO()),
            response,
        ]
        with patch("control_plane.merge_train_job_logs.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                read_job_log(request)

    def test_non_storage_or_non_https_redirect_is_refused_before_download(self) -> None:
        for location in (
            "http://results.blob.core.windows.net/log",
            "https://attacker.test/log",
            "https://user:pass@results.blob.core.windows.net/log",
        ):
            headers = Message()
            headers["Location"] = location
            opener = Mock()
            opener.open.side_effect = HTTPError(
                "https://api.github.com/job/logs", 302, "Found", headers, BytesIO()
            )
            with (
                self.subTest(location=location),
                patch("control_plane.merge_train_job_logs.build_opener", return_value=opener),
            ):
                with self.assertRaises(ValueError):
                    read_job_log(Request("https://api.github.com/job/logs"))
                self.assertEqual(opener.open.call_count, 1)
