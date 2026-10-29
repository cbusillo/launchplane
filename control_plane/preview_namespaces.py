from collections.abc import Iterable

from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


POSTGRES_PREVIEW_TRANSPORTS = frozenset({"postgres", "verireel-postgres"})


def effective_preview_app_name_prefix(*, profile: LaunchplaneProductProfileRecord) -> str:
    return profile.preview.app_name_prefix.strip() or f"{profile.product}-preview"


def _namespace_prefixes(
    profile: LaunchplaneProductProfileRecord, retained_contexts: set[str] | None = None
) -> dict[str, str]:
    if profile.driver_id not in {"generic-web", "verireel"} or not profile.preview.context:
        return {}
    if (
        retained_contexts is not None
        and not profile.preview.enabled
        and profile.preview.context not in retained_contexts
    ):
        return {}
    leading_slug = profile.preview.slug_template.strip().partition("{number}")[0]
    namespaces = {
        "application": f"{effective_preview_app_name_prefix(profile=profile)}-{leading_slug}"
    }
    if profile.preview.transport_extension in POSTGRES_PREVIEW_TRANSPORTS:
        namespaces["database"] = (
            f"{profile.preview.transport_database_prefix}{leading_slug.lower().replace('-', '_')}"
        )
    return namespaces


def require_unique_preview_namespaces(
    profile: LaunchplaneProductProfileRecord,
    profiles: Iterable[LaunchplaneProductProfileRecord],
    *,
    retained_contexts: set[str] | None = None,
) -> None:
    namespaces = _namespace_prefixes(profile)
    for other in profiles:
        if other.product == profile.product:
            continue
        for kind, other_prefix in _namespace_prefixes(other, retained_contexts).items():
            prefix = namespaces.get(kind)
            if prefix is not None and (
                prefix.startswith(other_prefix) or other_prefix.startswith(prefix)
            ):
                raise ValueError(
                    "Preview namespaces overlap another product; choose distinct application/database prefixes and slug templates."
                )


def _resource_declaration(profile: LaunchplaneProductProfileRecord) -> dict[str, object]:
    preview = profile.preview
    template = next(
        (lane for lane in profile.lanes if lane.instance == preview.template_instance), None
    )
    return {
        "repository": profile.repository,
        "driver": "generic-web" if profile.driver_id == "verireel" else profile.driver_id,
        "context": preview.context,
        "transport": "postgres"
        if preview.transport_extension in POSTGRES_PREVIEW_TRANSPORTS
        else preview.data_transport_mode,
        "application_prefix": effective_preview_app_name_prefix(profile=profile),
        "application_suffix": preview.app_name_suffix,
        "database_prefix": preview.transport_database_prefix,
        "slug_template": preview.slug_template.strip(),
        "template_instance": preview.template_instance,
        "template_context": template.context if template is not None else "",
    }


def preview_product_operation_key(product: str) -> str:
    return f"product-retirement:{product}"


def preview_resource_declaration_changed(
    previous: LaunchplaneProductProfileRecord | None, replacement: LaunchplaneProductProfileRecord
) -> bool:
    return previous is None or _resource_declaration(previous) != _resource_declaration(replacement)


def validate_preview_profile_writes(
    *,
    profiles: Iterable[LaunchplaneProductProfileRecord],
    replacements: Iterable[LaunchplaneProductProfileRecord],
    previews: Iterable[PreviewRecord],
) -> None:
    current = {profile.product: profile for profile in profiles}
    updates = {profile.product: profile for profile in replacements}
    prospective = {**current, **updates}
    retained_contexts = {preview.context for preview in previews if preview.state != "destroyed"}
    for product, replacement in updates.items():
        previous = current.get(product)
        changed = preview_resource_declaration_changed(previous, replacement)
        if changed and previous is not None and previous.preview.context in retained_contexts:
            raise ValueError(
                "Clean up existing previews under their current declaration before changing preview resource names, source or transport."
            )
        if (
            changed
            or previous is not None
            and replacement.preview.enabled
            and not previous.preview.enabled
        ):
            if replacement.preview.enabled or replacement.preview.context in retained_contexts:
                require_unique_preview_namespaces(
                    replacement, prospective.values(), retained_contexts=retained_contexts
                )
