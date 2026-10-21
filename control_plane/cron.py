"""The supported UTC cron grammar for managed application schedules."""


def _field_matches(field: str, value: int, *, minimum: int, maximum: int) -> bool:
    if "," in field:
        parts = field.split(",")
        if any(not part for part in parts):
            raise ValueError(f"Invalid cron list: {field!r}")
        # Validate every member even when an earlier member matches.
        matches = tuple(
            _field_matches(part, value, minimum=minimum, maximum=maximum) for part in parts
        )
        return any(matches)
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        if not 1 <= step <= maximum - minimum + 1:
            raise ValueError(f"Invalid cron step: {field!r}")
        return (value - minimum) % step == 0
    if not field.isdecimal():
        raise ValueError(f"Unsupported cron field: {field!r}")
    expected = int(field)
    if not minimum <= expected <= maximum:
        raise ValueError(f"Cron field is outside its range: {field!r}")
    return value == expected


def validate_cron_expression(expression: str) -> None:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Managed cron expressions must have five fields.")
    for field, minimum, maximum in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 6), strict=True):
        _field_matches(field, minimum, minimum=minimum, maximum=maximum)
