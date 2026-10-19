import json
from pathlib import Path
from unittest import TestCase

from pydantic import ValidationError

from control_plane.contracts.merge_train_controller_reply import (
    build_merge_train_controller_reply_schema,
    validate_merge_train_controller_reply,
)

SCHEMA_PATH = Path("contracts/merge-train-controller-reply.schema.json")
REGENERATE = (
    "uv run --extra dev python -m control_plane.contracts.merge_train_controller_reply "
    f"{SCHEMA_PATH}"
)


def _reply(**result: object) -> dict[str, object]:
    return {
        "status": "accepted",
        "trace_id": "trace-1",
        "records": {"merge_train_batch_candidate_record_id": "candidate-1"},
        "result": {
            "repository": "cbusillo/alpha",
            "base_branch": "main",
            "controller_action": "block",
            **result,
        },
    }


class MergeTrainControllerReplyContractTests(TestCase):
    def test_published_schema_matches_the_contract(self) -> None:
        published = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(
            published,
            build_merge_train_controller_reply_schema(),
            f"Regenerate the published schema: {REGENERATE}",
        )

    def test_reply_fields_outside_the_contract_are_rejected(self) -> None:
        block_result = {
            "status": "blocked",
            "pull_request_number": 7,
            "train_should_continue": True,
        }
        validate_merge_train_controller_reply(_reply(block_result=block_result))

        undeclared = {
            "result": _reply(new_result_field="x"),
            "records": {**_reply(), "records": {"new_record_id": "r-1"}},
            "nested": _reply(block_result={**block_result, "new_block_field": "x"}),
            "envelope": {**_reply(), "new_envelope_field": "x"},
        }
        for name, reply in undeclared.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                validate_merge_train_controller_reply(reply)
