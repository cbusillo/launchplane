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


_COMPOSE_HEADER = "# launchplane:compose-dotenv-v1"
_COMPOSE_PLAIN = re.compile(r"[A-Za-z0-9_./:@%+=,-]*\Z")
_COMPOSE_ESCAPES = re.compile(r"\\\\|\\0([0-7]{3})")


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


def _compose_literal(value: str) -> str:
    if "\x00" in value:
        raise DokployEnvironmentEncodingError(
            "NUL is not a container environment value; remove NUL from this runtime input."
        )
    if _COMPOSE_PLAIN.fullmatch(value):
        return value
    # Compose supports octal escapes. Hide apostrophes from the first dotenv
    # delimiter and dollars from Dokploy substitutions; Compose expands the
    # doubled dollars to literals. No product-side decoder is needed.
    return (
        '"'
        + "".join(
            r"\0047"
            if char == "'"
            else r"\0044\0044"
            if char == "$"
            else "\\0" + format(ord(char), "03o")
            if ord(char) < 32 and char not in "\n\r\t\b\f"
            else json.dumps(char, ensure_ascii=False)[1:-1]
            for char in value
        )
        + '"'
    )


def _compose_encoded_keys(raw_env_text: str) -> set[str] | None:
    normalized = raw_env_text.replace("\r\n", "\n").replace("\r", "\n")
    spans = [(match.start(), match.end()) for match in _LINE.finditer(normalized)]
    encoded_keys: set[str] | None = None
    offset = 0
    for line in normalized.splitlines(keepends=True):
        comment = line.strip()
        # A lookalike comment inside a multiline value is data, not metadata.
        if (comment == _COMPOSE_HEADER or comment.startswith(_COMPOSE_HEADER + " ")) and not any(
            start <= offset < end for start, end in spans
        ):
            if encoded_keys is None:
                encoded_keys = set()
            encoded_keys.update(comment[len(_COMPOSE_HEADER) :].split())
        offset += len(line)
    return encoded_keys


def compose_environment_is_encoded(raw_env_text: str) -> bool:
    return _compose_encoded_keys(raw_env_text) is not None


def _provider_environment(raw_env_text: str) -> tuple[dict[str, str], set[str]]:
    values = _parse_dotenv(raw_env_text)
    encoded_keys = _compose_encoded_keys(raw_env_text) or set()
    decoded_keys: set[str] = set()
    for key in encoded_keys & values.keys():
        wire_value = values[key]
        if _COMPOSE_PLAIN.fullmatch(wire_value):
            decoded_keys.add(key)
            continue
        try:
            json_value = _COMPOSE_ESCAPES.sub(
                lambda match: (
                    json.dumps(chr(int(match[1], 8)))[1:-1] if match[1] is not None else match[0]
                ),
                wire_value,
            )
            literal = json.loads(json_value)
            if not isinstance(literal, str):
                continue
            literal = literal.replace("$$", "$")
            if _compose_literal(literal) != wire_value:
                continue
        except (ValueError, TypeError):
            continue
        values[key] = literal
        decoded_keys.add(key)
    return values, decoded_keys


def parse_dokploy_env_text(raw_env_text: str) -> dict[str, str]:
    return _provider_environment(raw_env_text)[0]


def serialize_dokploy_env_text(
    env_map: dict[str, str],
    *,
    target_type: str = "application",
    compose_type: str = "docker-compose",
    current_env_text: str | None = None,
) -> str:
    if target_type != "compose" or compose_type == "stack":
        return _serialize_dotenv(env_map)
    previous, encoded_keys = _provider_environment(current_env_text or "")
    previous_wire = _parse_dotenv(current_env_text or "")
    encoded: dict[str, str] = {}
    literal_keys: set[str] = set()
    for key, value in env_map.items():
        if current_env_text is not None and key not in encoded_keys and previous.get(key) == value:
            # A routine image/startup write must not reinterpret an untouched
            # legacy password/reference against an already-initialized database.
            encoded[key] = previous_wire[key]
        else:
            encoded[key] = _compose_literal(value)
            literal_keys.add(key)
    rendered = _serialize_dotenv(encoded)
    return _COMPOSE_HEADER + " " + " ".join(sorted(literal_keys)) + "\n" + rendered


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
