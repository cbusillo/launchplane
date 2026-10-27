from copy import deepcopy

import sqlalchemy as sa

from control_plane.preview_settings import validate_preview_scope

from control_plane.storage.site_setting_migration import _RUNTIME_ENVIRONMENTS

_PROFILES = sa.table(
    "launchplane_product_profiles",
    sa.column("product", sa.String),
    sa.column("updated_at", sa.String),
    sa.column("payload", sa.JSON),
)


def migrate_preview_settings(connection: sa.Connection, *, recorded_at: str) -> None:
    profiles = connection.execute(sa.select(_PROFILES).with_for_update()).mappings().all()
    destinations: set[str] = set()
    for row in profiles:
        payload = deepcopy(row["payload"])
        preview = payload.get("preview") or {}
        values = preview.get("override_env") or {}
        if not values:
            continue
        raw_context = str(preview.get("context") or "")
        context = raw_context.strip().lower()
        if context != raw_context:
            raise ValueError("Preview settings migration requires canonical contexts.")
        if not context or context in destinations:
            raise ValueError("Preview settings migration requires distinct configured contexts.")
        destinations.add(context)
        owners = {
            other["product"]
            for other in profiles
            if context
            in {
                str(candidate or "").strip().lower()
                for candidate in {
                    *(lane.get("context") for lane in other["payload"].get("lanes", [])),
                    (other["payload"].get("preview") or {}).get("context"),
                    *other["payload"].get("historical_contexts", []),
                }
            }
        }
        if owners != {row["product"]}:
            raise ValueError("Preview settings migration requires exclusive product ownership.")
        where = (
            _RUNTIME_ENVIRONMENTS.c.scope == "instance",
            _RUNTIME_ENVIRONMENTS.c.context == context,
            _RUNTIME_ENVIRONMENTS.c.instance == "preview",
        )
        existing = connection.execute(
            sa.select(_RUNTIME_ENVIRONMENTS.c.payload).where(*where).with_for_update()
        ).scalar_one_or_none()
        env = dict((existing or {}).get("env") or {})
        conflicts = {key for key in values if key in env and env[key] != values[key]}
        retired = set((existing or {}).get("retired_provider_keys") or ())
        if conflicts or retired.intersection(values):
            raise ValueError("Preview settings conflict with existing managed preview settings.")
        env.update(values)
        validate_preview_scope(env, context=context)
        updated = {
            **(existing or {}),
            "schema_version": (existing or {}).get("schema_version", 1),
            "scope": "instance",
            "context": context,
            "instance": "preview",
            "env": env,
            "updated_at": recorded_at,
            "source_label": "migration:preview-settings",
        }
        if existing is None:
            connection.execute(
                sa.insert(_RUNTIME_ENVIRONMENTS).values(
                    scope="instance",
                    context=context,
                    instance="preview",
                    updated_at=recorded_at,
                    payload=updated,
                )
            )
        else:
            connection.execute(
                sa.update(_RUNTIME_ENVIRONMENTS)
                .where(*where)
                .values(
                    updated_at=recorded_at,
                    payload=updated,
                )
            )
        preview.pop("override_env", None)
        expected = payload.setdefault("expected_config", {})
        requirements = expected.setdefault("runtime_environment_keys", [])
        declared = {
            (item.get("context", ""), item.get("instance", ""), item["key"])
            for item in requirements
        }
        for key in values:
            if (context, "preview", key) not in declared:
                requirements.append({"context": context, "instance": "preview", "key": key})
        payload["updated_at"] = recorded_at
        connection.execute(
            sa.update(_PROFILES)
            .where(_PROFILES.c.product == row["product"])
            .values(
                payload=payload,
                updated_at=recorded_at,
            )
        )
