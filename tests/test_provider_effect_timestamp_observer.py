from tempfile import TemporaryDirectory
import unittest

from control_plane.provider_operations import (
    ProviderMutationUnknownError,
    ProviderObservation,
)
from tests.test_provider_operations import _FakeAdapter, _StoreFixture


class _TimestampObserver(_FakeAdapter):
    def __init__(self) -> None:
        super().__init__(observation=ProviderObservation(outcome="unknown"))
        self.provider_effect_started_at = ""

    def observe_with_effect_started_at(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
        provider_effect_started_at: str,
    ) -> ProviderObservation:
        self.provider_effect_started_at = provider_effect_started_at
        return super().observe(
            provider_operation_key,
            provider_effect_phase,
            reconciliation_key,
        )


class ProviderEffectTimestampObserverTests(unittest.TestCase):
    def test_reconciliation_forwards_stored_effect_timestamp(self) -> None:
        with TemporaryDirectory() as directory:
            fixture = _StoreFixture(directory)
            first_attempt = _FakeAdapter(
                apply_error=ProviderMutationUnknownError("provider outcome unknown"),
                checkpoint_before_rejection=True,
            )

            first_result = fixture.run(first_attempt)
            expected_started_at = fixture.stored().provider_effect_started_at
            observer = _TimestampObserver()
            recovery_result = fixture.run(observer, lease_owner="instance-b")

        self.assertEqual(first_result.status, "reconcile_required")
        self.assertTrue(expected_started_at)
        self.assertEqual(recovery_result.status, "reconcile_required")
        self.assertEqual(observer.provider_effect_started_at, expected_started_at)


if __name__ == "__main__":
    unittest.main()
