import unittest

from control_plane.workflows.promote import build_promotion_record


class PromoteWorkflowTests(unittest.TestCase):
    def test_build_promotion_record_returns_pending_record(self) -> None:
        record = build_promotion_record(
            record_id="promotion-20260410-182231-opw-testing-prod",
            artifact_id="artifact-20260410-f45db648",
            context_name="opw",
            from_instance_name="testing",
            to_instance_name="prod",
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="",
        )

        self.assertEqual(record.artifact_identity.artifact_id, "artifact-20260410-f45db648")
        self.assertEqual(record.deploy.status, "pending")
        self.assertEqual(record.deploy.target_name, "opw-prod")
        self.assertEqual(record.from_instance, "testing")


if __name__ == "__main__":
    unittest.main()
