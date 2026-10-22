import json
import unittest
from email.message import Message
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from io import BytesIO
from pprint import pformat
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import requests

from control_plane.dokploy import api


def _cookie_representations() -> tuple[str, ...]:
    cookie = Cookie(
        0,
        "sid",
        'made-up-jar]>"\\tail',
        None,
        False,
        "example.invalid",
        False,
        False,
        "/",
        True,
        False,
        None,
        True,
        None,
        None,
        {},
    )
    jar = CookieJar()
    jar.set_cookie(cookie)
    request_jar = requests.cookies.RequestsCookieJar()
    request_jar.set_cookie(cookie)
    simple = SimpleCookie()
    simple["sid"] = "made-up-simple"
    simple["apostrophe"] = "'made-up-'tail"
    simple["punctuation"] = 'made-up->]"tail'
    headers = Message()
    headers["Set-Cookie"] = "sid=made-up-pair"
    return (
        repr(headers.items()),
        """['Cookie', `sid="made-up-one's"`, 'X-Request-Id', 'req-123']""",
        repr([(b"Set-Cookie", b"sid=made-up-bytes"), (b"X-Request-Id", b"req-123")]),
        json.dumps({"rawHeaders": ["Set-Cookie", "sid=made-up-raw", "X-Request-Id", "req-123"]}),
        "rawHeaders: [\n  'Set-Cookie',\n  'sid=made-up-raw',\n  'X-Request-Id', 'req-123'\n]",
        *(
            representation(value)
            for value in (
                cookie,
                jar,
                request_jar,
                simple,
                simple["sid"],
                simple["apostrophe"],
                simple["punctuation"],
            )
            for representation in (str, repr)
        ),
        *(
            f"{key}: sid=made-up-alias"
            for key in ("HTTP_COOKIE", "cookies", "Cookie2", "Set-Cookie2")
        ),
        *(
            json.dumps({key: ["sid=made-up-alias", "other=made-up-second"], "status": 502})
            for key in ("HTTP_COOKIE", "cookies", "Cookie2", "Set-Cookie2")
        ),
        'Cookie: "sid=made-up-prefix"; other=made-up-tail',
        "Cookie: 'sid=made-up-prefix'; other=made-up-tail",
        'Set-Cookie: "sid=made-up-prefix", other=made-up-tail',
        'Cookie: "sid=made-up-prefix" other=made-up-tail',
    )


class DokployCookieLogTests(unittest.TestCase):
    def test_undecodable_quoted_representations_redact_cookies(self) -> None:
        for representation in _cookie_representations():
            for invalid_json in (r"\x1b", "\t"):
                with self.subTest(representation=representation, invalid_json=invalid_json):
                    body = json.dumps(f"{representation} {invalid_json}")
                    if invalid_json == "\t":
                        body = body.replace(r"\t", "\t")
                    else:
                        body = body.replace(r"\\x1b", r"\x1b")
                    with self.assertRaises(json.JSONDecodeError):
                        json.loads(body)
                    output = api.redact_dokploy_log_line(f"status=502\n{body}\nretry scheduled")
                    self.assertNotIn("made-up-", output)
                    self.assertIn("status=502", output)
                    self.assertIn("retry scheduled", output)

    def test_tagged_cookie_containers_redact_their_bodies(self) -> None:
        for representation in (
            "cookies: [Object: null prototype] { sid: 'made-up-node' }, status: 502",
            "cookies: [Object: null prototype] {\n  sid: 'made-up-node'\n}, status: 502",
        ):
            with self.subTest(representation=representation):
                output = api.redact_dokploy_log_line(representation)
                self.assertNotIn("made-up-", output)
                self.assertIn("status: 502", output)

    def test_quotes_inside_redacted_values_cannot_hide_later_cookie_keys(self) -> None:
        text = "{ cookie: 'sid=made-up-one\"tail', 'set-cookie': \"x=made-up-two's\", status: 502 }"
        output = api.redact_dokploy_log_line(text)
        self.assertNotIn("made-up-", output)
        self.assertIn("status: 502", output)

    def test_representations_redact_whole_bodies_and_batches(self) -> None:
        for representation in _cookie_representations():
            body = f"status=502\n{representation}\nretry scheduled"
            for text in (body, json.dumps({"message": body})):
                with self.subTest(representation=representation, text=text):
                    output = api.redact_dokploy_log_line(text)
                    self.assertNotIn("made-up-", output)
                    self.assertIn("status=502", output)
                    self.assertIn("retry scheduled", output)
            payloads: list[api.JsonValue] = [
                body,
                list(body.splitlines()),
                [{"message": line} for line in body.splitlines()],
            ]
            for payload in payloads:
                with self.subTest(representation=representation, payload=payload):
                    output = "\n".join(api.normalize_dokploy_log_payload(payload))
                    self.assertNotIn("made-up-", output)
                    self.assertIn("status=502", output)
                    self.assertIn("retry scheduled", output)

    def test_representation_redaction_preserves_neighboring_structured_diagnostics(self) -> None:
        for representation in (
            "[('Set-Cookie', 'sid=made-up-pair'), ('X-Request-Id', 'req-123')]",
            """['Cookie', `sid="made-up-one's"`, 'X-Request-Id', 'req-123']""",
            '["Cookie", "sid=made-up-raw", "X-Request-Id", "req-123"]',
            "<Cookie sid=made-up-jar for example.invalid/>",
            "Cookie(version=0, name='sid', value='made-up-jar')",
            "<SimpleCookie: sid='made-up-simple'>",
            "{'cookies': {'sid': 'made-up-map'}, 'request': 'req-123'}",
        ):
            with self.subTest(representation=representation):
                output = api.redact_dokploy_log_line(f"{representation}, status=502")
                self.assertNotIn("made-up-", output)
                self.assertIn("status=502", output)
                if "req-123" in representation:
                    self.assertIn("req-123", output)

    def test_representation_redaction_runs_on_provider_errors(self) -> None:
        for representation in _cookie_representations():
            for error in (
                HTTPError(
                    "https://provider.example/api/test",
                    502,
                    "failed",
                    Message(),
                    BytesIO(f"status=502\n{representation}\nretry scheduled".encode()),
                ),
                URLError(f"status=502\n{representation}\nretry scheduled"),
            ):
                with self.subTest(representation=representation, error=type(error).__name__):
                    with patch("control_plane.dokploy.api.urlopen", side_effect=error):
                        with self.assertRaises(api.DokployRequestFailed) as raised:
                            api.dokploy_request(
                                host="https://provider.example",
                                token="test-token",
                                path="/api/test",
                            )
                    output = raised.exception.format_message()
                    self.assertNotIn("made-up-", output)
                    self.assertIn("status=502", output)
                    self.assertIn("retry scheduled", output)

    def test_representation_redaction_runs_before_provider_log_tail(self) -> None:
        for representation in _cookie_representations():
            for reader, target in (
                (api.fetch_dokploy_application_logs, {"application_id": "test-app"}),
                (api.fetch_dokploy_compose_logs, {"compose_id": "test-compose"}),
                (api.fetch_dokploy_deployment_logs, {"deployment_id": "test-deployment"}),
            ):
                with self.subTest(representation=representation, reader=reader.__name__):
                    with patch(
                        "control_plane.dokploy.api.dokploy_request",
                        return_value={"logs": representation.splitlines() + ["status=502"]},
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
                    self.assertIn("status=502", output)

    def test_truncated_representations_fail_closed(self) -> None:
        for representation in (
            "[('Set-Cookie', 'sid=made-up-pair",
            '["Cookie", "sid=made-up-raw',
            "<Cookie sid=made-up-jar",
            "Cookie(version=0, name='sid', value='made-up-jar",
            "<SimpleCookie: sid='made-up-simple",
            "{'cookies': {'sid': 'made-up-map",
        ):
            with self.subTest(representation=representation):
                text = f"status=502\n{representation}\nmade-up-continuation"
                output = api.redact_dokploy_log_line(text)
                self.assertNotIn("made-up-", output)
                self.assertIn("status=502", output)

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

    def test_value_start_and_continuation_rules_share_transport_prefixes_and_indentation(
        self,
    ) -> None:
        for prefix in ("", "web-1 | ", "2026-10-22T01:00:00Z "):
            for lines in (
                ["Cookie:", '"sid=made-up-one"', "status=502"],
                ["Cookie:", "sid=made-up-one", "status=502"],
                ["Cookie:", '["sid=made-up-one",', '"other=made-up-two"]', "status=502"],
                ["Cookie:", "- sid=made-up-one", "- other=made-up-two", "status=502"],
                ["Cookie:", "  - sid=made-up-one", "  - other=made-up-two", "status=502"],
                ["Cookie: sid=made-up-one;", "  other=made-up-two", "status=502"],
                ["  Cookie: sid=made-up-one", "  X-Request-Id: req-123", "status=502"],
            ):
                with self.subTest(prefix=prefix, lines=lines):
                    output = "\n".join(
                        api.normalize_dokploy_log_payload([prefix + line for line in lines])
                    )
                    self.assertNotIn("made-up-", output)
                    self.assertIn(prefix + "status=502", output)
                    if "  X-Request-Id: req-123" in lines:
                        self.assertIn(prefix + "  X-Request-Id: req-123", output)

    def test_generic_whole_error_bodies_keep_multiline_secret_protection(self) -> None:
        for body in ('{"API_TOKEN":\n"made-up-token"}', "TOKEN=\nmade-up-token"):
            with self.subTest(body=body):
                self.assertNotIn("made-up-", api.redact_dokploy_log_line(body))

    def test_truncated_encoded_error_retains_later_log_lines(self) -> None:
        body = '{"message":"{\\"set-cookie\\":[\\"sid=made-up-one\\"],\\"detail\\":\\"abc'
        output = api.redact_dokploy_log_line(body + "\nretry scheduled\ndeploy finished")
        self.assertNotIn("made-up-", output)
        self.assertIn("retry scheduled\ndeploy finished", output)

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
                redacted = "\n".join(api.normalize_dokploy_log_payload(text))
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
        for value in (
            "sid=made-up-one; " + "other=made-up-two; " * 4,
            ("sid=made-up-one", "other=made-up-two"),
        ):
            with self.subTest(value=value):
                text = pformat({"Cookie": value, "status": 502}, width=45)
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
