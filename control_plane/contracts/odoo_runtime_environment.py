from __future__ import annotations

from collections.abc import Iterable


LAUNCHPLANE_REQUIRED_ODOO_ADDON_PATHS = (
    "/opt/launchplane/addons",
    "/opt/enterprise",
)


def _merge_odoo_addons_path(*path_groups: str | Iterable[str]) -> str:
    merged_paths: list[str] = []
    for path_group in path_groups:
        if isinstance(path_group, str):
            raw_paths: Iterable[str] = path_group.split(",")
        else:
            raw_paths = path_group
        for path in raw_paths:
            normalized_path = str(path).strip()
            if not normalized_path or normalized_path in merged_paths:
                continue
            merged_paths.append(normalized_path)
    return ",".join(merged_paths)


def merge_required_odoo_addons_path(raw_path: str) -> str:
    normalized_path = _merge_odoo_addons_path(raw_path)
    if not normalized_path:
        return ""
    return _merge_odoo_addons_path(normalized_path, LAUNCHPLANE_REQUIRED_ODOO_ADDON_PATHS)
