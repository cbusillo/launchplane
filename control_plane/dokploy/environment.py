"""Dokploy's dotenv storage grammar, before its project/service interpolation.

Dokploy parses environment text with npm dotenv. In particular, quotes are
delimiters rather than shell escapes, and only double quotes expand \\n/\\r.
Keep this grammar aligned with that consumer, not with a symmetric private
encoding that the provider never reads.

Consumer reference: https://github.com/motdotla/dotenv/blob/v16.4.5/lib/main.js
"""

import json
import re
from collections.abc import Iterable


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
_COMPOSE_LITERAL_HEADER = "# launchplane:compose-dotenv-v2"
_COMPOSE_CHARACTERS = {"DOLLAR": "$", "QUOTE": "'", "SLASH": "\\", "RETURN": "\r"}
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


def _legacy_compose_literal(value: str) -> str:
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


def _compose_encoded_keys(raw_env_text: str, header: str = _COMPOSE_HEADER) -> set[str] | None:
    normalized = raw_env_text.replace("\r\n", "\n").replace("\r", "\n")
    spans = [match.span(2) for match in _LINE.finditer(normalized)]
    encoded_keys: set[str] | None = None
    offset = 0
    for line in normalized.splitlines(keepends=True):
        comment = line.strip()
        # A lookalike comment inside a multiline value is data, not metadata.
        if (comment == header or comment.startswith(header + " ")) and not any(
            start <= offset < end for start, end in spans
        ):
            encoded_keys = (encoded_keys or set()).union(comment[len(header) :].split())
        offset += len(line)
    return encoded_keys


def compose_environment_is_encoded(raw_env_text: str) -> bool:
    return any(
        _compose_encoded_keys(raw_env_text, header) is not None
        for header in (_COMPOSE_HEADER, _COMPOSE_LITERAL_HEADER)
    )


def _compose_value(value: str, names: dict[str, str]) -> str:
    return "".join(
        "${" + names[character] + "}" if character in _COMPOSE_CHARACTERS.values() else character
        for character in value
    )


def _literal_provider_environment(
    raw_env_text: str, values: dict[str, str]
) -> tuple[dict[str, str], set[str]]:
    metadata = _compose_encoded_keys(raw_env_text, _COMPOSE_LITERAL_HEADER)
    if metadata is None:
        return values, set()
    bindings: dict[str, str] = {}
    for token in metadata:
        if "=" not in token:
            continue
        role, name = token.split("=", 1)
        if role not in _COMPOSE_CHARACTERS or values.get(name) != _COMPOSE_CHARACTERS[role]:
            return values, set()
        bindings[name] = _COMPOSE_CHARACTERS[role]
    names = {character: name for name, character in bindings.items()}

    def restore_character(match: re.Match[str]) -> str:
        return bindings.get(match.group(1) or "", match.group() or "")

    decoded_keys: set[str] = set()
    for key in (metadata & values.keys()) - bindings.keys():
        literal = re.sub(
            r"\$\{([^{}]+)}",
            restore_character,
            values[key],
        )
        try:
            if _compose_value(literal, names) != values[key]:
                continue
        except KeyError:
            continue
        values[key] = literal
        decoded_keys.add(key)
    for name in bindings:
        values.pop(name)
    return values, decoded_keys


def _legacy_provider_environment(raw_env_text: str) -> tuple[dict[str, str], set[str]]:
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
            if _legacy_compose_literal(literal) != wire_value:
                continue
        except (ValueError, TypeError):
            continue
        values[key] = literal
        decoded_keys.add(key)
    return values, decoded_keys


def _provider_environment(raw_env_text: str) -> tuple[dict[str, str], set[str]]:
    values, _ = _legacy_provider_environment(raw_env_text)
    return _literal_provider_environment(raw_env_text, values)


def parse_dokploy_env_text(raw_env_text: str) -> dict[str, str]:
    return _provider_environment(raw_env_text)[0]


def require_unique_compose_inputs(raw_env_text: str, keys: Iterable[str]) -> dict[str, str]:
    selected = set(keys)
    normalized = raw_env_text.replace("\r\n", "\n").replace("\r", "\n")
    counts = [match[1] for match in _LINE.finditer(normalized)]
    values = parse_dokploy_env_text(raw_env_text)
    if any(
        counts.count(key) != 1 or not values.get(key) for key in selected
    ) or compose_unqualified_keys(raw_env_text, selected):
        raise ValueError("Compose resource inputs are missing, duplicated or unqualified.")
    return {key: values[key] for key in selected}


def compose_unqualified_keys(raw_env_text: str, keys: Iterable[str]) -> list[str]:
    """Names whose legacy transport cannot establish literal Compose delivery.

    Plain legacy values need no additional quoting. Missing values are handled
    by value verification; this reports transport uncertainty, never values.
    """
    values, decoded_keys = _provider_environment(raw_env_text)
    return sorted(
        key
        for key in set(keys) & values.keys() - decoded_keys
        if not _COMPOSE_PLAIN.fullmatch(values[key])
    )


def compose_literal_keys(raw_env_text: str) -> set[str]:
    """Keys with validated literal transport, for a writer's readback contract."""
    return _provider_environment(raw_env_text)[1]


def serialize_dokploy_env_text(
    env_map: dict[str, str],
    *,
    target_type: str = "application",
    compose_type: str = "docker-compose",
    current_env_text: str | None = None,
    literal_keys: Iterable[str] = (),
) -> str:
    requested_keys = set(literal_keys)
    if requested_keys - env_map.keys():
        raise DokployEnvironmentEncodingError("Literal qualification requires managed key names.")
    if target_type != "compose" or compose_type == "stack":
        if requested_keys:
            raise DokployEnvironmentEncodingError(
                "Literal qualification requires a Compose target."
            )
        return _serialize_dotenv(env_map)
    previous, legacy_keys = _legacy_provider_environment(current_env_text or "")
    previous, encoded_keys = _literal_provider_environment(current_env_text or "", previous)
    previous_wire = _parse_dotenv(current_env_text or "")
    encoded: dict[str, str] = {}
    encoded_literal_keys: set[str] = set()
    preserved_legacy_keys: set[str] = set()
    for key, value in env_map.items():
        if (
            current_env_text is not None
            and key not in encoded_keys | requested_keys
            and previous.get(key) == value
        ):
            # A routine image/startup write must not reinterpret an untouched
            # legacy password/reference against an already-initialized database.
            encoded[key] = previous_wire[key]
            if key in legacy_keys:
                preserved_legacy_keys.add(key)
        else:
            encoded[key] = value
            encoded_literal_keys.add(key)
    names: dict[str, str] = {}
    metadata = set(encoded_literal_keys)
    for role, character in _COMPOSE_CHARACTERS.items():
        if not any(character in encoded[key] for key in encoded_literal_keys):
            continue
        base = "LAUNCHPLANE_DOTENV_" + role
        name = base
        suffix = 0
        while name in env_map or any("${" + name in value for value in env_map.values()):
            suffix += 1
            name = f"{base}_{suffix}"
        names[character] = name
        metadata.add(role + "=" + name)
    escaped = {
        key: _compose_value(value, names) if key in encoded_literal_keys else value
        for key, value in encoded.items()
    }
    rendered = _serialize_dotenv(
        {**{name: character for character, name in names.items()}, **escaped}
    )
    headers = [_COMPOSE_LITERAL_HEADER + " " + " ".join(sorted(metadata))]
    if preserved_legacy_keys:
        headers.append(_COMPOSE_HEADER + " " + " ".join(sorted(preserved_legacy_keys)))
    return "\n".join([*headers, rendered])


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
