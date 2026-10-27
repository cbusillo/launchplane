from collections.abc import Callable


def public_error_reference(
    value: object, project: Callable[[object], str], *, default: str = ""
) -> str:
    try:
        return project(value)
    except ValueError:
        return default
