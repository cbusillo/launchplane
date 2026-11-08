import logging

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane import secrets
from control_plane.contracts.runtime_environment_record import ScalarValue
from control_plane.service_auth import BearerIdentityConfig
from control_plane.storage.postgres import PostgresRecordStore

_LOGGER = logging.getLogger(__name__)
AGENT_BEARER_SOURCE_KEY = "LAUNCHPLANE_AGENT_BEARER_SOURCE"
_RECORD_READ_ERRORS = (ValueError, OSError, click.ClickException, SQLAlchemyError)
_AGENT_KEYS = (
    ("local_operator", "LAUNCHPLANE_LOCAL_OPERATOR", "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN"),
    ("terminal_agent", "LAUNCHPLANE_TERMINAL_AGENT", "LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN"),
)
AGENT_SELECTOR_KEYS = frozenset(
    f"{prefix}_{suffix}"
    for _identity, prefix, _token in _AGENT_KEYS
    for suffix in ("SUBJECT", "TOKEN_LABEL")
)
AGENT_BEARER_ENVIRONMENT_KEYS = {
    f"{identity}_{field}": key
    for identity, prefix, token_key in _AGENT_KEYS
    for field, key in (
        ("token", token_key),
        ("subject", f"{prefix}_SUBJECT"),
        ("token_label", f"{prefix}_TOKEN_LABEL"),
    )
}


def load_service_agent_identities(
    *, store: PostgresRecordStore, bootstrap: BearerIdentityConfig
) -> BearerIdentityConfig:
    try:
        records = store.list_runtime_environment_records(
            scope="context", context_name="launchplane"
        )
        values = records[0].env if records else {}
        if values.get(AGENT_BEARER_SOURCE_KEY) in (None, "bootstrap"):
            return bootstrap
        if values[AGENT_BEARER_SOURCE_KEY] != "records":
            raise ValueError("Invalid service identity source")
    except _RECORD_READ_ERRORS:
        _LOGGER.warning("Service identity selectors unavailable; Director-agent auth disabled.")
        return bootstrap.model_copy(update={"local_operator_token": "", "terminal_agent_token": ""})
    updates: dict[str, str] = {}
    for identity, prefix, token_key in _AGENT_KEYS:
        subject_key, label_key = f"{prefix}_SUBJECT", f"{prefix}_TOKEN_LABEL"
        subject = _selector(values.get(subject_key))
        label = _selector(values.get(label_key))
        token = ""
        if subject and label:
            try:
                token = secrets.resolve_context_secret_value_from_store(
                    record_store=store,
                    integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                    context_name="launchplane",
                    binding_key=token_key,
                )
            except _RECORD_READ_ERRORS:
                _LOGGER.warning(
                    "Service identity secret unavailable; Director-agent auth disabled."
                )
        updates.update(
            {
                f"{identity}_token": token,
                f"{identity}_subject": subject,
                f"{identity}_token_label": label,
            }
        )
    return bootstrap.model_copy(update=updates)


def _selector(value: ScalarValue | None) -> str:
    return value.strip() if isinstance(value, str) else ""
