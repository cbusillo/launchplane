from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from control_plane import generic_web_testing_rebuild as rebuild
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.dokploy import api
from control_plane.testing_rebuild_service import NativeTestingRebuildPending
from tests import test_generic_web_testing_rebuild as generic


class PredecessorLineageTests(generic.GenericWebTestingRebuildFlowFixture):
    def retain_original(self) -> str:
        read = rebuild.GenericWebRebuildActivationAdapter.read_edge_health

        def failed(adapter: Any, record: Any, **kwargs: Any) -> Any:
            if record.published:
                return HealthcheckEvidence(
                    verified=True,
                    status="fail",
                    urls=("https://testing.example.invalid/health",),
                    timeout_seconds=5,
                    runtime_identity_status="match",
                    observed_runtime_identity=kwargs.get("expected"),
                )
            return read(adapter, record, **kwargs)

        with patch.object(
            rebuild.GenericWebRebuildActivationAdapter, "read_edge_health", new=failed
        ):
            result = self.execute(1)
        self.assertEqual(result.health_status, "fail")
        self.retained_predecessors.add(self.flow.old_id)
        self.assertEqual(self.flow.normal("repair-before-retirement").status, "completed")
        target_id = result.target_id
        assert isinstance(target_id, str)
        return target_id

    def test_lost_ancestor_delete_reply_keeps_its_own_runtime_removal_authority(self) -> None:
        previous = self.retain_original()
        self.keep_predecessor_service = True
        delete = api.delete_dokploy_application

        def lost(**kwargs: Any) -> None:
            delete(**kwargs)
            if kwargs["application_id"] == self.flow.old_id:
                raise OSError("Lost ancestor delete reply")

        with patch.object(api, "delete_dokploy_application", side_effect=lost):
            with self.assertRaises(NativeTestingRebuildPending):
                self.execute(2)
        self.assertNotIn(self.flow.old_id, self.flow.payloads)
        self.assertIn("old-app", self.flow.services)
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            return_value=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        ):
            result = self.execute(2)
        self.assertEqual(result.health_status, "pass")
        self.assertNotIn("old-app", self.flow.services)
        self.assertEqual(self.deleted, [previous, self.flow.old_id])
        self.assertEqual(self.flow.normal("after-ancestor-retirement").status, "completed")

    def test_provider_only_reference_protects_an_ancestor_from_retirement(self) -> None:
        self.retain_original()
        provider = self.store.read_provider_target_record(
            context_name=self.flow.context, instance_name="testing"
        )
        alias = provider.model_copy(
            update={"context": "another-lane", "target_id": self.flow.old_id}
        )
        self.store.write_provider_target_record(alias)
        with self.assertRaises(NativeTestingRebuildPending):
            self.execute(2)
        self.assertIn(self.flow.old_id, self.flow.payloads)
        self.assertNotIn(self.flow.old_id, self.deleted)
        self.assertEqual(
            self.store.read_provider_target_record(
                context_name=alias.context, instance_name=alias.instance
            ),
            alias,
        )
