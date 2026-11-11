import inspect


def storage_capacity_requirements(
    demand: dict[str, int],
    backings: dict[str, str],
    reserve: int,
    *,
    staging_cleanup_before_snapshot: bool = False,
    phase: str | None = None,
) -> dict[str, int]:
    if type(reserve) is not int or reserve < 0:
        raise ValueError("Testing storage reserve is unavailable")
    if phase not in (None, "restore", "snapshot"):
        raise ValueError("Testing storage phase is unavailable")
    phases: dict[str, dict[str, int]] = {"restore": {}, "snapshot": {}}
    for role, size in demand.items():
        backing = backings.get(role)
        if not isinstance(backing, str) or not backing.strip() or type(size) is not int or size < 0:
            raise ValueError("Testing storage demand or backing is unavailable")
        if not size:
            continue
        for phase_name, required in phases.items():
            if phase is not None and phase_name != phase:
                continue
            if staging_cleanup_before_snapshot and (
                (role == "data_staging" and phase_name == "snapshot")
                or (role == "snapshot" and phase_name == "restore")
            ):
                continue
            required[backing] = required.get(backing, 0) + size
    return {
        backing: max(required.get(backing, 0) for required in phases.values()) + reserve
        for backing in set().union(*(required.keys() for required in phases.values()))
    }


STORAGE_CAPACITY_LIBRARY = inspect.getsource(storage_capacity_requirements)
