from collections.abc import Iterable
from pathlib import Path

PROJECT_ADDONS_ROOT = Path("/opt/project/addons")


def addon_group_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        child
        for child in sorted(root.iterdir())
        if child.is_dir()
        and not child.name.startswith((".", "__"))
        and not (child / "__manifest__.py").is_file()
        and not (child / "__openerp__.py").is_file()
        and any(
            (addon / "__init__.py").is_file() and ((addon / "__manifest__.py").is_file() or (addon / "__openerp__.py").is_file())
            for addon in child.iterdir()
            if addon.is_dir()
        )
    ]


def expand_addon_paths(paths: Iterable[Path], *, source_root: Path | None = None) -> list[Path]:
    resolved: list[Path] = []
    for path in paths:
        additions = [path]
        if path == PROJECT_ADDONS_ROOT:
            groups = addon_group_dirs(source_root if source_root is not None else PROJECT_ADDONS_ROOT)
            additions.extend(PROJECT_ADDONS_ROOT / group.name for group in groups)
        for addition in additions:
            if addition not in resolved:
                resolved.append(addition)
    copies: set[Path] = set()
    for path in resolved:
        local = (
            source_root / path.relative_to(PROJECT_ADDONS_ROOT)
            if source_root is not None and path.is_relative_to(PROJECT_ADDONS_ROOT)
            else path
        )
        module = local / "disable_odoo_online"
        if (module / "__manifest__.py").is_file():
            copies.add(module.resolve())
    if len(copies) > 1:
        locations = ", ".join(str(path) for path in sorted(copies))
        raise ValueError(f"Multiple disable_odoo_online copies on addons path: {locations}. Keep exactly one source.")
    return resolved
