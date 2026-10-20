"""Dokploy's dotenv storage grammar, before its project/service interpolation.

Dokploy parses environment text with npm dotenv. In particular, quotes are
delimiters rather than shell escapes, and only double quotes expand \\n/\\r.
Keep this grammar aligned with that consumer, not with a symmetric private
encoding that the provider never reads.

Consumer reference: https://github.com/motdotla/dotenv/blob/v16.4.5/lib/main.js
"""

import json
import re


# ECMAScript whitespace (also used by String.trim), not Python's broader \s.
_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_SPACE = f"[{_WHITESPACE}]"
_LINE = re.compile(
    rf"(?:\A|(?<=[\n\u2028\u2029])){_SPACE}*(?:export{_SPACE}+)?([A-Za-z0-9_.-]+)"
    rf"(?:{_SPACE}*={_SPACE}*?|:{_SPACE}+?)"
    rf"({_SPACE}*'(?:\\'|[^'])*'|{_SPACE}*\"(?:\\\"|[^\"])*\"|"
    rf"{_SPACE}*`(?:\\`|[^`])*`|[^#\r\n]+)?"
    rf"{_SPACE}*(?:#[^\n\u2028\u2029]*)?(?=\Z|[\n\u2028\u2029])",
)
_QUOTED = re.compile(r"(?:\A|(?<=[\n\u2028\u2029]))(['\"`])([\s\S]*)\1(?=\Z|[\n\u2028\u2029])")


class DokployEnvironmentEncodingError(ValueError):
    """The provider cannot consume an environment value without changing it."""


_COMPOSE_HEADER = "# launchplane:compose-dotenv-v1\n"
_COMPOSE_PLAIN = re.compile(r"[A-Za-z0-9_./:@%+=,-]*\Z")


def _parse_dotenv(raw_env_text: str) -> dict[str, str]:
    normalized = raw_env_text.replace("\r\n", "\n").replace("\r", "\n")
    values: dict[str, str] = {}
    for match in _LINE.finditer(normalized):
        if match[1] == "__proto__":
            continue
        value = (match[2] or "").strip(_WHITESPACE)
        quote = value[:1]
        value = _QUOTED.sub(r"\2", value)
        if quote == '"':
            value = value.replace("\\n", "\n").replace("\\r", "\r")
        values[match[1]] = value
    return values


def parse_dokploy_env_text(raw_env_text: str) -> dict[str, str]:
    values = _parse_dotenv(raw_env_text)
    if raw_env_text.startswith(_COMPOSE_HEADER):
        try:
            decoded: dict[str, str] = {}
            for key, value in values.items():
                if _COMPOSE_PLAIN.fullmatch(value):
                    decoded[key] = value
                else:
                    literal = json.loads(value)
                    if not isinstance(literal, str):
                        raise ValueError
                    decoded[key] = literal.replace("$$", "$")
            return decoded
        except (ValueError, TypeError):
            raise DokployEnvironmentEncodingError(
                "Invalid Compose environment encoding; reconcile from managed runtime inputs."
            ) from None
    return values


def serialize_dokploy_env_text(
    env_map: dict[str, str],
    *,
    target_type: str = "application",
    compose_type: str = "docker-compose",
) -> str:
    if target_type == "compose" and compose_type != "stack":
        return _serialize_compose_environment(env_map)
    return _serialize_dotenv(env_map)


def _serialize_dotenv(env_map: dict[str, str]) -> str:
    rendered: list[str] = []
    for key, value in env_map.items():
        # NUL cannot be a container environment value. Dotenv itself accepts it.
        if "\x00" in value:
            raise DokployEnvironmentEncodingError("Dokploy cannot represent runtime inputs.")
        candidates = (
            value,
            f"'{value}'",
            f"`{value}`",
            '"' + value.replace("\r", "\\r") + '"',
        )
        for candidate in candidates:
            assignment = f"{key}={candidate}"
            if _parse_dotenv(assignment) == {key: value}:
                rendered.append(assignment)
                break
        else:
            # Never include a value in an error: these mappings include secrets.
            raise DokployEnvironmentEncodingError("Dokploy cannot represent runtime inputs.")
    result = "\n".join(rendered)
    if _parse_dotenv(result) != env_map:
        raise DokployEnvironmentEncodingError("Dokploy cannot represent runtime inputs.")
    return result


def _serialize_compose_environment(env_map: dict[str, str]) -> str:
    # Dokploy strips its dotenv quotes and writes KEY=value into .env. Keep a
    # second set of quotes for that consumer. The header distinguishes wire
    # quoting from literal application values on readback and on later merges.
    if any(re.search(r"\$\{\{[^\n\r\u2028\u2029]*?\}\}", value) for value in env_map.values()):
        raise DokployEnvironmentEncodingError(
            "Compose cannot preserve provider substitutions as literal inputs; "
            "literal substitution expressions require a different deployment provider."
        )
    try:
        encoded: dict[str, str] = {}
        for key, value in env_map.items():
            if _COMPOSE_PLAIN.fullmatch(value):
                encoded[key] = value
                continue
            encoded[key] = json.dumps(value.replace("$", "$$"), ensure_ascii=False)
        # Compose's quoted dotenv supports these escapes, not JSON's arbitrary
        # control-character Unicode escapes. NUL is invalid for containers too.
        if any(
            any(ord(char) < 32 and char not in "\n\r\t\b\f" for char in value)
            for value in env_map.values()
        ):
            raise DokployEnvironmentEncodingError
        return (_COMPOSE_HEADER if encoded != env_map else "") + _serialize_dotenv(encoded)
    except DokployEnvironmentEncodingError:
        raise DokployEnvironmentEncodingError(
            "Compose cannot represent these literal runtime inputs; "
            "use an application target for this lane. No values were changed."
        ) from None
