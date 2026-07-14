from collections.abc import Callable

import click


DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
DIRECT_DB_MUTATION_MESSAGE = (
    "Direct local DB mutation is restricted after the Launchplane service boundary. "
    "Use the deployed service route or operator workflow for shared/production changes, "
    "or pass --allow-direct-db-mutation only for explicit local/bootstrap repair."
)


def direct_db_mutation_acknowledgement_option(
    function: Callable[..., object],
) -> Callable[..., object]:
    return click.option(
        "--allow-direct-db-mutation",
        is_flag=True,
        default=False,
        help="Acknowledge direct local DB mutation for explicit local/bootstrap repair.",
    )(function)


def require_direct_db_mutation_acknowledgement(
    allow_direct_db_mutation: bool,
    *,
    message: str = DIRECT_DB_MUTATION_MESSAGE,
) -> None:
    if not allow_direct_db_mutation:
        raise click.ClickException(message)
