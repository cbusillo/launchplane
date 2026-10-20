import unittest

from control_plane.dokploy import api


class DokployEnvironmentTests(unittest.TestCase):
    def test_reads_provider_dotenv_comments_quotes_and_multiline_values(self) -> None:
        raw = (
            "export COMMENT=first#discarded\n"
            "QUOTED='first#kept'\n"
            'DOUBLE="first\\nsecond\\rthird"\n'
            "LITERAL='first\\nsecond'\n"
            "MULTILINE=`first\nsecond`\n"
            "SPACED=  value  \n"
            "EMPTY=\n"
            "COLON: value\n"
            "REPEATED=old\nREPEATED=new\n"
        )
        self.assertEqual(
            api.parse_dokploy_env_text(raw),
            {
                "COMMENT": "first",
                "QUOTED": "first#kept",
                "DOUBLE": "first\nsecond\rthird",
                "LITERAL": "first\\nsecond",
                "MULTILINE": "first\nsecond",
                "SPACED": "value",
                "EMPTY": "",
                "COLON": "value",
                "REPEATED": "new",
            },
        )

    def test_serializes_literal_values_for_the_provider_grammar(self) -> None:
        for value in (
            "first#second",
            '"first"',
            "first\nsecond",
            " first ",
            "first\\nsecond",
            "first's#second",
            "first's`#second",
            "first\rsecond",
            "$NAME ${NAME} $$ $(command)",
            "trailing\\",
            "'#trailing\\",
            "x\u2028'y'",
            "x\u2029'y'",
            "'a' x'\u2028y",
        ):
            with self.subTest(value=value):
                values = {"APP": value, "AFTER": "next"}
                rendered = api.serialize_dokploy_env_text(values)
                self.assertEqual(api.parse_dokploy_env_text(rendered), values)

    def test_reads_quotes_at_ecmascript_line_boundaries(self) -> None:
        for separator in ("\u2028", "\u2029"):
            with self.subTest(separator=separator):
                self.assertEqual(
                    api.parse_dokploy_env_text(f"APP=x{separator}'y'"),
                    {"APP": f"x{separator}y"},
                )

    def test_refuses_values_that_the_provider_cannot_consume_intact(self) -> None:
        for key, value in (("APP", "'\"`#\nvalue"), ("APP", "nul\x00value"), ("A\nB", "value")):
            with self.subTest(key=key):
                with self.assertRaises(api.DokployEnvironmentEncodingError) as error:
                    api.serialize_dokploy_env_text({key: value})
                self.assertNotIn(value, str(error.exception))

    def test_override_preserves_actual_provider_only_values(self) -> None:
        rendered = api.render_dokploy_env_text_with_overrides(
            "KEEP='first#second\nthird'\nREMOVE=old\nCHANGE=old",
            updates={"CHANGE": '"new"'},
            removals=("REMOVE",),
        )
        self.assertEqual(
            api.parse_dokploy_env_text(rendered),
            {"KEEP": "first#second\nthird", "CHANGE": '"new"'},
        )

    def test_compose_readback_merge_and_retirement_use_literal_values(self) -> None:
        values = {"KEEP": '"quoted" $NAME\nnext', "REMOVE": "old"}
        stored = api.serialize_dokploy_env_text(values, target_type="compose")
        self.assertEqual(api.parse_dokploy_env_text(stored), values)
        updated = api.render_dokploy_env_text_with_overrides(
            stored, updates={"NEW": "new"}, removals=("REMOVE",), target_type="compose"
        )
        self.assertEqual(
            api.parse_dokploy_env_text(updated), {"KEEP": values["KEEP"], "NEW": "new"}
        )
        self.assertEqual(
            api.serialize_dokploy_env_text(
                api.parse_dokploy_env_text(updated), target_type="compose"
            ),
            updated,
        )

    def test_compose_refuses_invalid_container_values_before_provider_writes(self) -> None:
        from unittest.mock import patch

        for value in ("nul\x00value",):
            with self.subTest(value=value), patch.object(api, "dokploy_request") as request:
                with self.assertRaises(api.DokployEnvironmentEncodingError) as error:
                    api.update_dokploy_target_env(
                        host="https://provider.invalid",
                        token="synthetic",
                        target_type="compose",
                        target_id="synthetic",
                        target_payload={"env": ""},
                        env_text=api.serialize_dokploy_env_text({"APP": value}),
                    )
                request.assert_not_called()
                self.assertNotIn(value, str(error.exception))

    def test_compose_encoding_does_not_hide_platform_credentials(self) -> None:
        from unittest.mock import patch

        import click

        stored = api.serialize_dokploy_env_text(
            {"DOKPLOY_TOKEN": '"synthetic token"'}, target_type="compose"
        )
        with (
            patch.object(api, "dokploy_request") as request,
            self.assertRaises(click.ClickException),
        ):
            api.update_dokploy_target_env(
                host="https://provider.invalid",
                token="synthetic",
                target_type="compose",
                target_id="synthetic",
                target_payload={"env": ""},
                env_text=stored,
            )
        request.assert_not_called()

    def test_provider_edits_and_header_normalization_remain_readable_and_reconcilable(self) -> None:
        original = {"KEEP": '"literal" $NAME\nnext'}
        wire = api.serialize_dokploy_env_text(original, target_type="compose")
        for edited, additions in (
            ("# provider comment\n" + wire.replace("\n", "\r\n"), {}),
            (wire + '\nNEW="hello world"\nARRAY=["a"]', {"NEW": "hello world", "ARRAY": '["a"]'}),
            (
                api.render_dokploy_env_text_with_overrides(wire, updates={"KEEP": "hello world"}),
                {"KEEP": "hello world"},
            ),
        ):
            with self.subTest(additions=additions):
                values = {**original, **additions}
                self.assertEqual(api.parse_dokploy_env_text(edited), values)
                rewritten = api.render_dokploy_env_text_with_overrides(
                    edited, updates={"NEXT": "next"}, target_type="compose"
                )
                self.assertEqual(api.parse_dokploy_env_text(rewritten), {**values, "NEXT": "next"})

    def test_format_comment_inside_a_value_is_not_metadata(self) -> None:
        from control_plane.dokploy.environment import compose_environment_is_encoded

        encoded = api.serialize_dokploy_env_text({"INPUT": "value"}, target_type="compose")
        header = encoded.splitlines()[0]
        legacy = api.serialize_dokploy_env_text({"APP": "line\n" + header + "\nnext"})
        self.assertFalse(compose_environment_is_encoded(legacy))
        self.assertEqual(api.parse_dokploy_env_text(legacy), {"APP": "line\n" + header + "\nnext"})
