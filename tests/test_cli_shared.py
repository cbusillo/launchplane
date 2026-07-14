import unittest

import click
from click.testing import CliRunner

from control_plane.cli_shared import DATABASE_URL_ENV_KEYS
from control_plane.cli_shared import DIRECT_DB_MUTATION_MESSAGE
from control_plane.cli_shared import direct_db_mutation_acknowledgement_option
from control_plane.cli_shared import require_direct_db_mutation_acknowledgement


class CliSharedTests(unittest.TestCase):
    def test_database_url_environment_keys_preserve_bootstrap_wiring(self) -> None:
        self.assertEqual(DATABASE_URL_ENV_KEYS, ("LAUNCHPLANE_DATABASE_URL",))

    def test_direct_db_acknowledgement_option_preserves_flag_default_and_help(self) -> None:
        @click.command()
        @direct_db_mutation_acknowledgement_option
        def command(allow_direct_db_mutation: bool) -> None:
            click.echo(str(allow_direct_db_mutation))

        runner = CliRunner()

        default_result = runner.invoke(command)
        allowed_result = runner.invoke(command, ["--allow-direct-db-mutation"])
        help_result = runner.invoke(command, ["--help"])

        self.assertEqual(default_result.exit_code, 0, default_result.output)
        self.assertEqual(default_result.output, "False\n")
        self.assertEqual(allowed_result.exit_code, 0, allowed_result.output)
        self.assertEqual(allowed_result.output, "True\n")
        self.assertEqual(help_result.exit_code, 0, help_result.output)
        normalized_help = " ".join(help_result.output.split())
        self.assertIn(
            "Acknowledge direct local DB mutation for explicit local/bootstrap repair.",
            normalized_help,
        )

    def test_direct_db_acknowledgement_requires_common_message(self) -> None:
        with self.assertRaises(click.ClickException) as raised:
            require_direct_db_mutation_acknowledgement(False)

        self.assertEqual(str(raised.exception), DIRECT_DB_MUTATION_MESSAGE)
        require_direct_db_mutation_acknowledgement(True)

    def test_direct_db_acknowledgement_accepts_explicit_message_override(self) -> None:
        message = "Direct local DB mutation is restricted for secret writes."

        with self.assertRaises(click.ClickException) as raised:
            require_direct_db_mutation_acknowledgement(False, message=message)

        self.assertEqual(str(raised.exception), message)
