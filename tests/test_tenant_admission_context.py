from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.tenant_admission_context import (
    build_tenant_admission_evaluation_read_model,
)
from control_plane.tenant_admission_controller import (
    evaluate_tenant_admission_candidate,
)
from control_plane.tenant_admission_status import get_tenant_admission_status
from tests.test_tenant_admission_controller import (
    _TenantControllerTransport,
    _request,
)
from tests.test_tenant_admission_status import (
    EVALUATED_AT,
    _candidate,
    _classification,
    _classification_only_store,
    _status_with_path_states,
)


class TenantAdmissionContextTests(unittest.TestCase):
    def test_tenant_context_exposes_human_guidance_without_agent_authority(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="stale",
            manager_state="pending",
        )
        with TemporaryDirectory() as temporary_name:
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                evaluation = evaluate_tenant_admission_candidate(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    transport_factory=lambda _token: _TenantControllerTransport(),
                )

        read_model = build_tenant_admission_evaluation_read_model(
            evaluation=evaluation,
            generated_at=EVALUATED_AT,
        )

        self.assertFalse(read_model.agent_authoring_allowed)
        self.assertEqual(read_model.evaluation.outcome, "blocked")
        technical_checks = read_model.evaluation.technical_checks
        self.assertIsNotNone(technical_checks)
        assert technical_checks is not None
        self.assertEqual(technical_checks.status, "pass")
        actions = {action.action_kind: action for action in read_model.human_actions}
        self.assertEqual(actions["manager_preview_approval"].availability, "available")
        self.assertEqual(actions["manager_preview_approval"].path_state, "pending")
        self.assertFalse(actions["manager_preview_approval"].agent_authoring_allowed)
        self.assertEqual(actions["technical_human_waiver"].availability, "available")
        self.assertEqual(actions["technical_human_waiver"].path_state, "stale")
        self.assertFalse(actions["technical_human_waiver"].agent_authoring_allowed)

    def test_satisfied_human_path_is_reported_without_hiding_alternative(self) -> None:
        admission = _status_with_path_states(
            trusted_state="pending",
            waiver_state="pending",
            manager_state="satisfied",
        )
        with TemporaryDirectory() as temporary_name:
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                evaluation = evaluate_tenant_admission_candidate(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    transport_factory=lambda _token: _TenantControllerTransport(),
                )

        read_model = build_tenant_admission_evaluation_read_model(
            evaluation=evaluation,
            generated_at=EVALUATED_AT,
        )
        actions = {action.action_kind: action for action in read_model.human_actions}
        self.assertEqual(read_model.evaluation.outcome, "ready")
        self.assertEqual(actions["manager_preview_approval"].availability, "satisfied")
        self.assertEqual(actions["technical_human_waiver"].availability, "available")

    def test_engineering_context_has_no_human_gate(self) -> None:
        admission = get_tenant_admission_status(
            store=_classification_only_store((_classification(kind="engineering"),)),
            candidate=_candidate(),
            evaluated_at=EVALUATED_AT,
        )
        with TemporaryDirectory() as temporary_name:
            with patch(
                "control_plane.tenant_admission_controller.get_tenant_admission_status",
                return_value=admission,
            ):
                evaluation = evaluate_tenant_admission_candidate(
                    request=_request(mutate=False),
                    store=FilesystemRecordStore(state_dir=Path(temporary_name)),
                    token="token",
                    transport_factory=lambda _token: _TenantControllerTransport(),
                )

        read_model = build_tenant_admission_evaluation_read_model(
            evaluation=evaluation,
            generated_at=EVALUATED_AT,
        )
        self.assertEqual(read_model.evaluation.outcome, "not_applicable")
        self.assertEqual(read_model.human_actions, ())


if __name__ == "__main__":
    unittest.main()
