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
        ):
            with self.subTest(value=value):
                values = {"APP": value, "AFTER": "next"}
                rendered = api.serialize_dokploy_env_text(values)
                self.assertEqual(api.parse_dokploy_env_text(rendered), values)

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
