"""Dokploy's dotenv storage grammar, before its project/service interpolation.

Dokploy parses environment text with npm dotenv. In particular, quotes are
delimiters rather than shell escapes, and only double quotes expand \\n/\\r.
Keep this grammar aligned with that consumer, not with a symmetric private
encoding that the provider never reads.

Consumer reference: https://github.com/motdotla/dotenv/blob/v16.4.5/lib/main.js
"""

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


class DokployEnvironmentEncodingError(ValueError):
    """The provider cannot consume an environment value without changing it."""


def parse_dokploy_env_text(raw_env_text: str) -> dict[str, str]:
    normalized = raw_env_text.replace("\r\n", "\n").replace("\r", "\n")
    values: dict[str, str] = {}
    for match in _LINE.finditer(normalized):
        if match[1] == "__proto__":
            continue
        value = (match[2] or "").strip(_WHITESPACE)
        quote = value[:1]
        if quote in {"'", '"', "`"} and value[-1:] == quote and len(value) >= 2:
            value = value[1:-1]
        if quote == '"':
            value = value.replace("\\n", "\n").replace("\\r", "\r")
        values[match[1]] = value
    return values


def serialize_dokploy_env_text(env_map: dict[str, str]) -> str:
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
            if parse_dokploy_env_text(assignment) == {key: value}:
                rendered.append(assignment)
                break
        else:
            # Never include a value in an error: these mappings include secrets.
            raise DokployEnvironmentEncodingError("Dokploy cannot represent runtime inputs.")
    result = "\n".join(rendered)
    if parse_dokploy_env_text(result) != env_map:
        raise DokployEnvironmentEncodingError("Dokploy cannot represent runtime inputs.")
    return result
