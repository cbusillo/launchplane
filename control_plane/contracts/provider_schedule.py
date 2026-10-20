"""Value-free local schedule transport failures."""

import click

SCHEDULE_MATERIALIZATION_TOO_LARGE = "schedule_materialization_too_large"
SCHEDULE_MATERIALIZATION_DESCRIPTION = (
    "The generated schedule exceeds the safe materialization size; "
    "deliver a compact host program before retrying the original operation."
)


class ScheduleMaterializationTooLarge(click.ClickException):
    """Local validation stopped this schedule before provider dispatch."""

    code = SCHEDULE_MATERIALIZATION_TOO_LARGE

    def __init__(self) -> None:
        super().__init__(SCHEDULE_MATERIALIZATION_DESCRIPTION)
