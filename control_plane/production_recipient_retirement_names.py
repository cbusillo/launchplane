from collections.abc import Iterator, Iterable
from contextlib import contextmanager
from typing import Any
import re


def require_available_name(
    payloads: Iterable[dict[str, Any]],
    name: str,
    target_id: str = "",
    *,
    creation: bool = False,
    generated_prefix: str = "",
) -> None:
    for payload in payloads:
        execution = payload.get("execution", {})
        if not execution.get("retirement_started_at"):
            continue
        manifest = execution["retirement_manifest"]
        normalized = name.strip().replace(" ", "-").lower()
        if (
            creation
            and generated_prefix
            and manifest["project"].startswith(generated_prefix)
            or (
                normalized
                and (
                    normalized == manifest["project"]
                    or creation
                    and re.fullmatch(re.escape(normalized) + r"-[a-z0-9]{6}", manifest["project"])
                    is not None
                )
            )
            or target_id
            and target_id == manifest["target"]
        ):
            raise ValueError(
                "The original retirement reserves this identity or namespace; creation can use an independent explicit app_name."
            )


def require_unretired_provider_name(store: object, name: str, target_id: str = "") -> None:
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(store, PostgresRecordStore):
        return
    require_available_name(store.list_production_recipient_payloads(), name, target_id)


@contextmanager
def provider_name_guard(store: object, name: str, *, generated_prefix: str = "") -> Iterator[None]:
    from control_plane.storage.postgres import PostgresRecordStore

    if not (name or generated_prefix) or not isinstance(store, PostgresRecordStore):
        yield
        return
    with store._session_factory() as session:
        store._lock_landing_authority(session, "retiring-provider-names")
        require_available_name(
            store.list_production_recipient_payloads(),
            name,
            creation=True,
            generated_prefix=generated_prefix,
        )
        yield
        session.commit()
