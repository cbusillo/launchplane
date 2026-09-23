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
)


class TenantAdmissionContextTests(unittest.TestCase):
    def test_tenant_context_reports_checks_without_retired_human_actions(self) -> None:
        evaluation = evaluate_tenant_admission_candidate(
            request=_request(mutate=False),
            store=_classification_only_store((_classification(),)),
            token="token",
            transport_factory=lambda _token: _TenantControllerTransport(),
        )
        read_model = build_tenant_admission_evaluation_read_model(
            evaluation=evaluation,
            generated_at=EVALUATED_AT,
        )
        self.assertEqual(read_model.evaluation.outcome, "ready")
        self.assertEqual(read_model.human_actions, ())
        self.assertFalse(read_model.agent_authoring_allowed)

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
