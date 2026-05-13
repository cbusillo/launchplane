import os
from pathlib import Path

from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_policy import default_merge_train_policy_path
from control_plane.contracts.merge_train_policy import load_merge_train_policy
from control_plane.contracts.merge_train_policy import parse_merge_train_policy_toml


_MERGE_TRAIN_POLICY_TOML_ENV_KEY = "LAUNCHPLANE_MERGE_TRAIN_POLICY_TOML"
_MERGE_TRAIN_POLICY_FILE_ENV_KEY = "LAUNCHPLANE_MERGE_TRAIN_POLICY_FILE"


def load_launchplane_merge_train_policy() -> MergeTrainPolicy:
    policy_toml = os.environ.get(_MERGE_TRAIN_POLICY_TOML_ENV_KEY, "").strip()
    if policy_toml:
        return parse_merge_train_policy_toml(policy_toml)

    policy_file = os.environ.get(_MERGE_TRAIN_POLICY_FILE_ENV_KEY, "").strip()
    if policy_file:
        return load_merge_train_policy(Path(policy_file))

    return load_merge_train_policy(default_merge_train_policy_path())
