"""Conservative host ownership for Traefik's boolean matcher expressions."""

import json
import re


def host_rule_may_match(rule: str, host: str) -> bool:
    """Exclude a foreign rule only when its boolean expression proves it false.

    Other matchers, including Go regular expressions, are unknown. In particular
    a foreign Host on one side of OR does not make a Path matcher safe to ignore.
    Unsupported syntax refuses route admission instead of guessing.
    """
    offset = 0

    def whitespace() -> None:
        nonlocal offset
        while offset < len(rule) and rule[offset].isspace():
            offset += 1

    def token(value: str) -> bool:
        nonlocal offset
        whitespace()
        if rule.startswith(value, offset):
            offset += len(value)
            return True
        return False

    def string() -> str:
        nonlocal offset
        whitespace()
        start = offset
        if offset >= len(rule) or rule[offset] not in {'"', "`"}:
            raise ValueError("Unsupported routing matcher argument.")
        quote = rule[offset]
        offset += 1
        while offset < len(rule):
            char = rule[offset]
            offset += 1
            if char == quote:
                return (
                    rule[start + 1 : offset - 1]
                    if quote == "`"
                    else str(json.loads(rule[start:offset]))
                )
            if char == "\\" and quote == '"':
                offset += 1
        raise ValueError("Unterminated routing matcher argument.")

    def primary() -> bool | None:
        nonlocal offset
        if token("!"):
            result = primary()
            return None if result is None else not result
        if token("("):
            result = expression()
            if not token(")"):
                raise ValueError("Unbalanced routing rule.")
            return result
        whitespace()
        name = re.match(r"[A-Za-z][A-Za-z0-9]*", rule[offset:])
        if name is None:
            raise ValueError("Unsupported routing expression.")
        offset += len(name[0])
        if not token("("):
            raise ValueError("Unsupported routing matcher.")
        arguments = [string()]
        while token(","):
            arguments.append(string())
        if not token(")"):
            raise ValueError("Unsupported routing matcher arguments.")
        if name[0] == "Host":
            # Traefik's Host matcher uses a case-insensitive DNS hostname.
            normalized = {value.casefold().rstrip(".") for value in arguments}
            if host.casefold().rstrip(".") in normalized:
                return True
            if any(not re.fullmatch(r"[a-zA-Z0-9.-]+", value) for value in arguments):
                return None
            return False
        return None

    def conjunction() -> bool | None:
        value = primary()
        while token("&&"):
            other = primary()
            value = (
                False
                if value is False or other is False
                else True
                if value is True and other is True
                else None
            )
        return value

    def expression() -> bool | None:
        value = conjunction()
        while token("||"):
            other = conjunction()
            value = (
                True
                if value is True or other is True
                else False
                if value is False and other is False
                else None
            )
        return value

    if not rule or len(rule) > 8192:
        raise ValueError("Routing expression is unavailable or exceeds its bound.")
    try:
        result = expression()
    except (RecursionError, json.JSONDecodeError) as error:
        raise ValueError("Routing expression cannot prove lane ownership.") from error
    whitespace()
    if offset != len(rule):
        raise ValueError("Routing expression has unsupported trailing input.")
    return result is not False
