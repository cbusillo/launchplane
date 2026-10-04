from pathlib import Path
import re
from unittest import TestCase


_REAL_TOPOLOGY_PATTERNS = (
    re.compile(r"^\s*default:\s*(?:sellyouroutboard|odoo-tenant-\S*|cm|opw)\s*$", re.MULTILINE),
    re.compile(r"sellyouroutboard", re.IGNORECASE),
    re.compile(r"shinycomputers\.com"),
    re.compile(r"odoo-tenant-(?:cm|opw)\b"),
    re.compile(r"allowed_targets="),
)


class OdooStableAuthorityTests(TestCase):
    def test_workflows_do_not_embed_real_product_topology(self) -> None:
        offenders: list[str] = []
        for workflow_path in sorted(Path(".github/workflows").glob("*.y*ml")):
            workflow_text = workflow_path.read_text(encoding="utf-8")
            for pattern in _REAL_TOPOLOGY_PATTERNS:
                if pattern.search(workflow_text):
                    offenders.append(f"{workflow_path.as_posix()}: {pattern.pattern}")

        self.assertEqual(offenders, [])

    def test_odoo_stable_workflows_do_not_use_retired_testing_deploy_route(self) -> None:
        retired_tokens = (
            "/v1/drivers/odoo/testing-deploy",
            "odoo_testing_deploy.execute",
            "control_plane.workflows.odoo_testing_deploy",
        )
        scanned_paths = tuple(
            path
            for root in (Path("control_plane"), Path(".github/workflows"), Path("scripts/deploy"))
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".sh", ".yml", ".yaml"}
        )

        offenders: list[str] = []
        for path in scanned_paths:
            text = path.read_text(encoding="utf-8")
            for token in retired_tokens:
                if token in text:
                    offenders.append(f"{path.as_posix()}: {token}")

        self.assertEqual(offenders, [])

    def test_prod_rollback_has_no_direct_provider_mutation(self) -> None:
        rollback_source = Path("control_plane/workflows/odoo_prod_rollback.py").read_text(
            encoding="utf-8"
        )
        required_tokens = (
            "execute_odoo_stable_target_replacement_apply",
            "OdooStableTargetReplacementApplyRequest",
        )
        forbidden_tokens = (
            "dokploy_api.trigger_deployment",
            "dokploy_compose.sync_dokploy_compose_raw_source",
            "dokploy_api.update_dokploy_target_env",
            "execute_odoo_post_deploy",
            "render_odoo_raw_compose_file",
            "wait_for_target_deployment",
        )

        for token in required_tokens:
            self.assertIn(token, rollback_source)
        for token in forbidden_tokens:
            self.assertNotIn(token, rollback_source)
