from typing import Callable
import unittest
from unittest.mock import Mock

from control_plane.merge_train_batch_candidate import (
    require_merge_train_batch_candidate_record_store,
    require_merge_train_stack_collapse_plan_record_store,
)
from control_plane.merge_train_stack_collapse import (
    require_merge_train_stack_collapse_plan_record_store as require_stack_collapse_store,
)


class MergeTrainRecordStoreCapabilityTests(unittest.TestCase):
    def test_complete_store_is_returned_without_wrapping(self) -> None:
        for name, guard, methods, _ in self._guards():
            with self.subTest(guard=name, methods=methods):
                store = Mock(spec=methods)
                self.assertIs(guard(store), store)

    def test_incomplete_store_keeps_its_specific_refusal(self) -> None:
        for name, guard, methods, message in self._guards():
            for available in ([], methods[:1], methods[1:]):
                with self.subTest(guard=name, available=available):
                    with self.assertRaisesRegex(TypeError, message):
                        guard(Mock(spec=available))

    @staticmethod
    def _guards() -> tuple[tuple[str, Callable[[object], object], list[str], str], ...]:
        return (
            (
                "batch_candidate",
                require_merge_train_batch_candidate_record_store,
                [
                    "write_merge_train_batch_candidate_record",
                    "list_merge_train_batch_candidate_records",
                ],
                "record store does not support merge train batch candidate records",
            ),
            (
                "batch_candidate_stack_collapse",
                require_merge_train_stack_collapse_plan_record_store,
                [
                    "write_merge_train_stack_collapse_plan_record",
                    "list_merge_train_stack_collapse_plan_records",
                ],
                "record store does not support merge train stack collapse plans",
            ),
            (
                "stack_collapse",
                require_stack_collapse_store,
                [
                    "write_merge_train_stack_collapse_plan_record",
                    "list_merge_train_stack_collapse_plan_records",
                ],
                "record store does not support merge train stack collapse plans",
            ),
        )
