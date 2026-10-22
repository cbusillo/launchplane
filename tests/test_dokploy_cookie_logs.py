import json
import unittest
from pprint import pformat
from unittest.mock import patch

from control_plane.dokploy import api


class DokployCookieLogTests(unittest.TestCase):
    def test_cookie_containers_and_concatenated_strings_keep_adjacent_diagnostics(self) -> None:
        for value in (
            "[\n  'sid=made-up-one; Path=/; HttpOnly',\n  'other=made-up-two; Secure'\n]",
            '[\n  "sid=made-up-one]",\n  {"nested": "made-up-two"}\n]',
            "'sid=made-up-one; ' +\n  'other=made-up-two'",
            '"sid=made-up-one; "\n  + "other=made-up-two"',
            "'sid=made-up-one;\nother=made-up-two'",
        ):
            for key in ("Cookie", "set-cookie"):
                with self.subTest(value=value, key=key):
                    text = f"request failed {{'{key}': {value}, 'status': 502}}\nretry scheduled"
                    redacted = api.redact_dokploy_log_line(text)
                    self.assertNotIn("made-up-", redacted)
                    self.assertIn("'status': 502", redacted)
                    self.assertIn("retry scheduled", redacted)

    def test_folded_headers_and_yaml_cookie_lists_redact_indented_continuations(self) -> None:
        for text in (
            "Set-Cookie:\n  - sid=made-up-one\n  - other=made-up-two\nstatus=502",
            "Cookie: sid=made-up-one;\n  other=made-up-two\nstatus=502",
        ):
            with self.subTest(text=text):
                redacted = api.redact_dokploy_log_line(text)
                self.assertNotIn("made-up-", redacted)
                self.assertIn("status=502", redacted)

    def test_prefixed_quoted_cookie_continuations_redact_transport_prefixes(self) -> None:
        for prefix in ("web-1 | ", "2026-10-22T01:00:00Z "):
            for operator in ("+", ""):
                with self.subTest(prefix=prefix, operator=operator):
                    text = (
                        f"{prefix}Cookie: 'sid=made-up-one' {operator}\n"
                        f"{prefix}  'other=made-up-two'\n{prefix}status=502"
                    )
                    redacted = api.redact_dokploy_log_line(text)
                    self.assertNotIn("made-up-", redacted)
                    self.assertIn("status=502", redacted)

    def test_cookie_keys_in_encoded_json_errors_redact_without_losing_diagnostics(self) -> None:
        text = json.dumps(
            {"message": json.dumps({"headers": {"set-cookie": ["sid=made-up-one"]}, "status": 502})}
        )
        for body in (text, json.dumps({"error": text}), text[:-10]):
            with self.subTest(body=body):
                redacted = api.redact_dokploy_log_line(body)
                self.assertNotIn("made-up-", redacted)
                self.assertIn("message", redacted)
        self.assertIn("502", api.redact_dokploy_log_line(text))

    def test_incomplete_keys_and_new_quoted_fields_preserve_diagnostics(self) -> None:
        for text in (
            "Missing cookie:\nretry scheduled",
            "Missing TOKEN:\nretry scheduled",
            "Missing Bearer\nretry scheduled",
            'Cookie: "sid=made-up-one"\n"status": 502',
        ):
            with self.subTest(text=text):
                redacted = api.redact_dokploy_log_line(text)
                self.assertNotIn("made-up-", redacted)
                self.assertIn(text.splitlines()[-1], redacted)

    def test_truncated_cookie_containers_and_strings_fail_closed(self) -> None:
        for value in (
            "[\n  'sid=made-up-one',\n  'other=made-up-two",
            "'sid=made-up-one;\nother=made-up-two",
            "'sid=made-up-one' +\n  'other=made-up-two",
        ):
            with self.subTest(value=value):
                redacted = api.redact_dokploy_log_line(f"status=502\nCookie: {value}")
                self.assertNotIn("made-up-", redacted)
                self.assertIn("status=502", redacted)

    def test_python_pretty_printed_cookie_strings_redact_implicit_continuations(self) -> None:
        text = pformat(
            {"Cookie": "sid=made-up-one; " + "other=made-up-two; " * 4, "status": 502},
            width=45,
        )
        redacted = api.redact_dokploy_log_line(text)
        self.assertNotIn("made-up-", redacted)
        self.assertIn("'status': 502", redacted)

    def test_batch_normalization_shares_cookie_context_across_payload_shapes(self) -> None:
        raw_lines = [
            "headers: { 'set-cookie': [",
            "  'sid=made-up-one',",
            "  'other=made-up-two'",
            "] }, status=502",
            "retry scheduled",
        ]
        payloads: list[api.JsonValue] = [
            "\n".join(raw_lines),
            list(raw_lines),
            [{"message": line} for line in raw_lines],
            {"logs": list(raw_lines)},
            {"lines": [{"line": line} for line in raw_lines]},
            {"output": "\r\n".join(raw_lines)},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                output = "\n".join(api.normalize_dokploy_log_payload(payload))
                self.assertNotIn("made-up-", output)
                self.assertIn("status=502", output)
                self.assertIn("retry scheduled", output)

    def test_runtime_and_deployment_readers_redact_before_tail(self) -> None:
        for reader, target in (
            (api.fetch_dokploy_application_logs, {"application_id": "test-app"}),
            (api.fetch_dokploy_compose_logs, {"compose_id": "test-compose"}),
            (api.fetch_dokploy_deployment_logs, {"deployment_id": "test-deployment"}),
        ):
            with self.subTest(reader=reader.__name__):
                with patch(
                    "control_plane.dokploy.api.dokploy_request",
                    return_value={
                        "logs": [
                            "Cookie: [",
                            "'sid=made-up-one',",
                            "'other=made-up-two']",
                            "failed",
                        ]
                    },
                ):
                    output = "\n".join(
                        reader(
                            host="https://provider.example",
                            token="test-token",
                            line_count=2,
                            **target,
                        )
                    )
                self.assertNotIn("made-up-", output)
                self.assertIn("failed", output)
