"""Applicability of immutable policy provenance to one train target."""

from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    merge_train_repository_execution_sha256,
)


def merge_train_policy_bindings_match(
    *, record_store: object | None, policy_key: str, recorded_sha256: str, current_sha256: str
) -> bool:
    if recorded_sha256 == current_sha256:
        return True
    projections = resolve_merge_train_execution_bindings(
        record_store=record_store,
        policy_key=policy_key,
        policy_sha256s=(recorded_sha256, current_sha256),
    )
    before = projections.get(recorded_sha256)
    return before is not None and before == projections.get(current_sha256)


def resolve_merge_train_execution_bindings(
    *, record_store: object | None, policy_key: str, policy_sha256s: tuple[str, ...]
) -> dict[str, str]:
    reader = getattr(record_store, "list_merge_train_policy_records", None)
    if not callable(reader):
        return {}
    try:
        records = tuple(reader())
        projections: dict[str, set[str]] = {digest: set() for digest in policy_sha256s}
        repository, base_branch = policy_key.rsplit(":", 1)
        for value in records:
            record = MergeTrainPolicyRecord.model_validate(value)
            if record.policy_sha256 in projections:
                target = record.policy.find_repository_policy(
                    repository=repository, base_branch=base_branch
                )
                projections[record.policy_sha256].add(
                    merge_train_repository_execution_sha256(target)
                )
        return {
            digest: next(iter(values)) for digest, values in projections.items() if len(values) == 1
        }
    except (AttributeError, KeyError, LookupError, OSError, TypeError, ValueError):
        return {}
