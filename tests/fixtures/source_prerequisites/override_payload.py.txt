import base64
import binascii
import json
from collections.abc import Collection, Mapping
from typing import NamedTuple

ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY = "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64"
FALSE_VALUES = {"", "0", "false", "no", "off"}
TRUE_VALUES = {"1", "true", "yes", "on"}


class OverrideValue(NamedTuple):
    source: str
    literal_value: object
    environment_variable: str


class OverridePayloadError(ValueError):
    pass


def _normalize_config_param_value(raw_value: str) -> str:
    value = raw_value.strip()
    lowered = value.lower()
    if lowered in FALSE_VALUES:
        return "False"
    if lowered in TRUE_VALUES:
        return "True"
    return value


def _parse_boolean(raw_value: str | None, *, default: bool) -> bool:
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in FALSE_VALUES:
        return False
    if value in TRUE_VALUES:
        return True
    return default


def _normalize_scalar_override_value(raw_value: object) -> str:
    if isinstance(raw_value, bool):
        return "True" if raw_value else "False"
    return _normalize_config_param_value(str(raw_value))


def decode_payload(encoded: str) -> Mapping[str, object] | None:
    encoded_payload = encoded.strip()
    if not encoded_payload:
        return None
    try:
        decoded_payload = base64.b64decode(encoded_payload, validate=True)
        payload = json.loads(decoded_payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as error:
        raise OverridePayloadError("Invalid Odoo instance override payload.") from error
    if not isinstance(payload, dict):
        raise OverridePayloadError("Odoo instance override payload must decode to an object.")
    return payload


def _payload_items(payload: Mapping[str, object] | None, key: str) -> tuple[Mapping[str, object], ...]:
    if payload is None:
        return ()
    raw_items = payload.get(key)
    if raw_items is None:
        return ()
    if not isinstance(raw_items, list):
        raise OverridePayloadError(f"Odoo instance override payload field '{key}' must be a list.")
    items: list[Mapping[str, object]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise OverridePayloadError(f"Odoo instance override payload field '{key}' must contain objects.")
        items.append(raw_item)
    return tuple(items)


def _validate_value_payload(*, raw_value: object, override_name: str) -> OverrideValue:
    if not isinstance(raw_value, dict):
        raise OverridePayloadError(f"Odoo override '{override_name}' has an invalid value payload.")
    source = str(raw_value.get("source") or "").strip()
    if source == "literal":
        if "value" not in raw_value:
            raise OverridePayloadError(f"Odoo override '{override_name}' is missing a literal value.")
        return OverrideValue(source, raw_value.get("value"), "")
    if source == "secret_binding":
        environment_variable = str(raw_value.get("environment_variable") or "").strip()
        if not environment_variable:
            raise OverridePayloadError(f"Secret-backed Odoo override '{override_name}' is missing its runtime environment variable.")
        return OverrideValue(source, None, environment_variable)
    raise OverridePayloadError(f"Odoo override '{override_name}' has unsupported source '{source}'.")


def _payload_value_for_setting(*, raw_value: object, override_name: str, environment: Mapping[str, str]) -> str:
    source, literal_value, environment_variable = _validate_value_payload(raw_value=raw_value, override_name=override_name)
    if source == "literal":
        return _normalize_scalar_override_value(literal_value)
    if environment_variable not in environment:
        raise OverridePayloadError(
            f"Secret-backed Odoo override '{override_name}' is missing environment variable {environment_variable}."
        )
    return environment.get(environment_variable, "")


def resolve_config_parameters(
    payload: Mapping[str, object] | None,
    environment: Mapping[str, str],
    *,
    keys: Collection[str] | None = None,
) -> dict[str, str]:
    selected: set[str] | None = None
    if keys is not None:
        if isinstance(keys, (str, bytes, bytearray, memoryview)) or not isinstance(keys, Collection):
            raise OverridePayloadError("Selected Odoo config parameter keys must be a collection of strings.")
        selected = set()
        for selected_key in keys:
            if not isinstance(selected_key, str) or not selected_key.strip():
                raise OverridePayloadError("Selected Odoo config parameter keys must be nonempty strings.")
            selected.add(selected_key.strip().lower())
    overrides: dict[str, str] = {}
    for item in _payload_items(payload, "config_parameters"):
        key = str(item.get("key") or "").strip().lower()
        if not key:
            raise OverridePayloadError("Odoo config parameter override payload entries require key.")
        if selected is not None and key not in selected:
            _validate_value_payload(raw_value=item.get("value"), override_name=key)
            continue
        overrides[key] = _payload_value_for_setting(raw_value=item.get("value"), override_name=key, environment=environment)
    return overrides
