import json
from control_plane.contracts.merge_train_controller_reply import _REPLY_ADAPTER


def validate_merge_train_controller_reply(reply: object) -> None:

    _REPLY_ADAPTER.validate_json(json.dumps(reply), strict=True)
