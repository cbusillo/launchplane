from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy


DEFAULT_STACK_CHILD_DISPOSITION_LABEL = "stack-landed"


def resolve_stack_child_disposition_label(policy: MergeTrainRepositoryPolicy) -> str:
    # Resolve at the effect boundary; stored policy bytes and digests stay unchanged.
    label = policy.stack_child_disposition_label or DEFAULT_STACK_CHILD_DISPOSITION_LABEL
    if label in {policy.enqueue_label, policy.blocked_label}:
        raise ValueError("Stack child disposition label conflicts with a train control label.")
    return label
