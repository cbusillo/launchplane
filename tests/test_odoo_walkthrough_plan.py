from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.secret_record import SecretRecord
from control_plane.odoo_runtime_reads import (
    OdooRuntimeConnection,
    OdooRuntimeEvidence,
    OdooRuntimeSelection,
)
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration
from control_plane.odoo_walkthrough import (
    plan_walkthrough,
    read_walkthrough_prerequisites,
    WALKTHROUGH_SECRET_INTEGRATION,
    WalkthroughPrerequisite,
    WalkthroughPrerequisiteError,
)
from control_plane.contracts.odoo_walkthrough import WALKTHROUGH_CREDENTIAL_LIFETIME_SECONDS
from tests.test_odoo_walkthrough import task_request


class WalkthroughPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = MagicMock()
        self.store.read_product_profile_record.return_value = SimpleNamespace(is_active=True)
        self.store.list_product_profile_records.return_value = ()
        self.store.find_secret_record.return_value = None
        self.now = datetime.now(timezone.utc)
        self.generation = FreshOdooTestingGeneration(
            product="example-odoo",
            context="example",
            operation_id="testing-operation",
            target_id="testing-target",
            namespace="lp-testing-" + "a" * 32,
            domain_host="testing.example",
            resources_binding_sha256="a" * 64,
            bootstrap_plan_sha256="b" * 64,
            request_sha256="c" * 64,
            prepared_at=(self.now - timedelta(minutes=2)).isoformat(),
            completed_at=(self.now - timedelta(minutes=1)).isoformat(),
            reconcile_requested_at=self.now.isoformat(),
        )
        self.identity = RuntimeIdentity(
            product="example-odoo",
            context="example",
            instance="testing",
            deployment_record_id="deployment",
            artifact_id="artifact",
            source_git_ref="a" * 40,
        )
        self.selection = OdooRuntimeSelection(self.identity, "https://testing.example")
        self.connection = OdooRuntimeConnection(
            selection=self.selection,
            evidence=OdooRuntimeEvidence(
                identity=self.identity,
                target_id="testing-target",
                target_name="testing",
                container_id="d" * 64,
                observed_at=self.now.isoformat(),
            ),
            host="https://provider.example",
            token="private-provider-token",
            server_id="testing-server",
            app_name="testing-project",
            credentials={"ODOO_DB_NAME": "testing"},
        )
        self.select = self.stack.enter_context(
            patch(
                "control_plane.odoo_walkthrough.select_stable_runtime", return_value=self.selection
            )
        )
        self.generation_read = self.stack.enter_context(
            patch(
                "control_plane.odoo_walkthrough.read_current_testing_generation",
                return_value=self.generation,
            )
        )
        self.usage = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.read_drill_recent_signins", return_value=False)
        )
        self.unused = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.testing_task_unused", return_value=True)
        )
        self.isolated = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.testing_connection_isolated", return_value=True)
        )
        self.hold = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.require_drill_use_unprotected")
        )
        self.connect = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.connect_runtime", return_value=self.connection)
        )
        self.domain = self.stack.enter_context(
            patch("control_plane.odoo_walkthrough.odoo_compose_has_domain", return_value=True)
        )
        self.environment = self.stack.enter_context(
            patch(
                "control_plane.odoo_walkthrough.testing_drill_environment",
                return_value={"ODOO_DB_NAME": "testing"},
            )
        )
        self.physical = self.stack.enter_context(
            patch(
                "control_plane.odoo_walkthrough.inspect_native_testing_receiver",
                side_effect=lambda **kwargs: kwargs["receive"](
                    {"type": "complete", "cleanup_qualified": True}
                ),
            )
        )

    def plan(self, operation: str = "provision") -> object:
        return plan_walkthrough(
            self.store, Path("/fixture"), task_request(operation=operation), "fixture-actor"
        )

    def secret(self, *, age: timedelta = timedelta(), disabled: bool = False) -> SecretRecord:
        created = (self.now - age).isoformat()
        return SecretRecord(
            secret_id="secret-fixture",
            scope="context_instance",
            integration=WALKTHROUGH_SECRET_INTEGRATION,
            name="task-fixture",
            context="example",
            instance="testing",
            current_version_id="version-fixture",
            status="disabled" if disabled else "configured",
            created_at=created,
            updated_at=created,
        )

    def test_preflight_keeps_independent_runtime_blockers_and_skips_missing_evidence(self) -> None:
        self.domain.return_value = False
        self.environment.return_value = {"ODOO_DB_NAME": "other-database"}
        self.store.find_secret_record.return_value = self.secret(disabled=True)
        checks = {
            checkpoint: state
            for checkpoint, code, state in read_walkthrough_prerequisites(
                self.store, Path("/fixture"), "example-odoo", "example", task_request()
            )
        }
        for code in (
            WalkthroughPrerequisite.ORIGIN,
            WalkthroughPrerequisite.DATABASE,
            WalkthroughPrerequisite.CREDENTIAL,
        ):
            self.assertEqual(checks[code], "blocked")
        self.assertEqual(checks[WalkthroughPrerequisite.ISOLATION_OR_USAGE], "unknown")
        self.unused.assert_not_called()
        # Action planning retains the first refusal and cannot consume diagnostic clearance.
        with self.assertRaises(WalkthroughPrerequisiteError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, WalkthroughPrerequisite.ORIGIN)

    def test_preflight_missing_generation_does_not_hide_hold_or_probe_dependent_runtime(
        self,
    ) -> None:
        self.generation_read.return_value = None
        self.hold.side_effect = ValueError("staff protection")
        checks = {
            checkpoint: state
            for checkpoint, code, state in read_walkthrough_prerequisites(
                self.store, Path("/fixture"), "example-odoo", "example"
            )
        }
        self.assertEqual(checks[WalkthroughPrerequisite.GENERATION], "blocked")
        self.assertEqual(checks[WalkthroughPrerequisite.USE], "blocked")
        self.assertEqual(checks[WalkthroughPrerequisite.RUNTIME], "unknown")
        self.assertEqual(checks[WalkthroughPrerequisite.CREDENTIAL], "unknown")
        self.connect.assert_not_called()
        self.usage.assert_not_called()
        self.store.find_secret_record.assert_not_called()

    def test_preflight_unreadable_use_is_unknown_without_hiding_other_checks(self) -> None:
        from control_plane.odoo_testing_drill import DrillEvidenceUnavailable

        self.hold.side_effect = DrillEvidenceUnavailable("unreadable")
        checks = {
            checkpoint: state
            for checkpoint, code, state in read_walkthrough_prerequisites(
                self.store, Path("/fixture"), "example-odoo", "example", task_request()
            )
        }
        self.assertEqual(checks[WalkthroughPrerequisite.USE], "unknown")
        self.assertEqual(checks[WalkthroughPrerequisite.ORIGIN], "clear")

    def test_production_foreign_context_and_origin_aliases_refuse_before_provider(self) -> None:
        with self.assertRaises(ValueError):
            plan_walkthrough(
                self.store,
                Path("/fixture"),
                task_request().model_copy(update={"instance": "prod"}),
                "actor",
            )
        self.store.read_product_profile_record.assert_not_called()
        for selection in (
            replace(self.selection, base_url="http://testing.example"),
            replace(
                self.selection, identity=self.identity.model_copy(update={"context": "foreign"})
            ),
        ):
            self.select.return_value = selection
            with self.assertRaises(ValueError):
                self.plan()
        self.select.return_value = self.selection
        self.store.list_product_profile_records.return_value = (
            SimpleNamespace(
                lanes=(SimpleNamespace(instance="prod", base_url="https://TESTING.example.:443"),)
            ),
        )
        with self.assertRaises(ValueError):
            self.plan()
        self.connect.assert_not_called()

    def test_missing_or_incomplete_generation_and_unreadable_isolation_refuse(self) -> None:
        for generation, code in (
            (None, WalkthroughPrerequisite.GENERATION_MISSING),
            (
                self.generation.model_copy(update={"completed_at": ""}),
                WalkthroughPrerequisite.GENERATION_INCOMPLETE,
            ),
        ):
            self.generation_read.return_value = generation
            with self.assertRaises(WalkthroughPrerequisiteError) as caught:
                self.plan()
            self.assertEqual(caught.exception.code, code)
        self.hold.assert_not_called()
        self.usage.assert_not_called()
        self.connect.assert_not_called()
        self.store.find_secret_record.assert_not_called()
        self.generation_read.return_value = self.generation
        self.usage.side_effect = ValueError("unreadable physical isolation")
        with self.assertRaises(ValueError):
            self.plan()
        self.connect.assert_not_called()

    def test_failed_private_reads_report_the_checkpoint_without_exception_details(self) -> None:
        private_detail = "private-password private-provider.example private-copy-id"
        for dependency, code in (
            (self.select, WalkthroughPrerequisite.IDENTITY),
            (self.generation_read, WalkthroughPrerequisite.GENERATION),
            (self.hold, WalkthroughPrerequisite.USE),
            (self.usage, WalkthroughPrerequisite.PHYSICAL_COPY),
            (self.connect, WalkthroughPrerequisite.RUNTIME),
            (self.domain, WalkthroughPrerequisite.ORIGIN),
            (self.environment, WalkthroughPrerequisite.DATABASE),
            (self.unused, WalkthroughPrerequisite.ISOLATION_OR_USAGE),
            (self.store.find_secret_record, WalkthroughPrerequisite.CREDENTIAL),
        ):
            with self.subTest(checkpoint=code):
                dependency.side_effect = RuntimeError(private_detail)
                try:
                    with self.assertRaises(WalkthroughPrerequisiteError) as caught:
                        self.plan()
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn(private_detail, str(caught.exception))
                finally:
                    dependency.side_effect = None

    def test_recipient_domain_and_database_must_all_match_before_secret_selection(self) -> None:
        self.connect.return_value = replace(
            self.connection,
            evidence=self.connection.evidence.model_copy(update={"target_id": "another-target"}),
        )
        with self.assertRaises(ValueError):
            self.plan()
        self.connect.return_value = self.connection
        self.domain.return_value = False
        with self.assertRaises(ValueError):
            self.plan()
        self.domain.return_value = True
        self.environment.return_value = {"ODOO_DB_NAME": "another-database"}
        with self.assertRaises(ValueError):
            self.plan()
        self.store.find_secret_record.assert_not_called()

    def test_closed_missing_and_expired_credentials_refuse_browser_work(self) -> None:
        for secret in (
            None,
            self.secret(disabled=True),
            self.secret(age=timedelta(seconds=WALKTHROUGH_CREDENTIAL_LIFETIME_SECONDS + 1)),
        ):
            self.store.find_secret_record.return_value = secret
            with self.assertRaises(ValueError):
                self.plan("run")

    def test_initial_provision_plan_refuses_recent_signins_before_any_account_exists(self) -> None:
        self.unused.return_value = False
        with self.assertRaises(ValueError):
            self.plan()

    def test_prior_task_signins_do_not_refuse_a_new_task_when_native_witnesses_qualify(
        self,
    ) -> None:
        self.usage.return_value = True
        self.plan()
        self.assertFalse(self.unused.call_args.kwargs["require_task"])

    def test_cleanup_still_qualifies_physical_copy_but_does_not_require_unused_lane(self) -> None:
        self.store.find_secret_record.return_value = self.secret(
            age=timedelta(seconds=WALKTHROUGH_CREDENTIAL_LIFETIME_SECONDS + 1)
        )
        self.usage.side_effect = ValueError("login history unavailable")
        self.unused.side_effect = ValueError("login history unavailable")
        self.hold.side_effect = ValueError("staff hold")
        self.plan("cleanup")
        self.usage.assert_not_called()
        self.unused.assert_not_called()
        self.hold.assert_not_called()
        self.physical.assert_called_once()
        self.isolated.assert_called_once()
        self.isolated.return_value = False
        with self.assertRaises(ValueError):
            self.plan("cleanup")
        self.isolated.return_value = True
        self.physical.side_effect = lambda **kwargs: kwargs["receive"](
            {"type": "complete", "cleanup_qualified": False}
        )
        with self.assertRaises(ValueError):
            self.plan("cleanup")
