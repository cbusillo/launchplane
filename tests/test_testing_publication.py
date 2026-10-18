"""Serving records move together after proof; rollback follows the committed outcome."""

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.testing_publication import (
    TestingPublicationPlan,
    TestingPublicationRecord,
    TestingServingSnapshot,
)
from control_plane.odoo_testing_restore import generation_binding
from control_plane.testing_candidate_runtime import CandidateSource
from control_plane.contracts.testing_candidate_application import TestingCandidatePrepared
from control_plane.contracts.testing_rebuild import TestingRouteObservation
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.dokploy.testing_route import compile_testing_route
from control_plane import testing_publication_storage as publication
from control_plane import testing_candidate_application as candidate_application
from control_plane.testing_publication import plan_testing_publication
from tests import test_testing_candidate_application as application_tests
from tests import test_testing_route as route_tests
from tests import test_testing_candidate_allocation as allocation_tests
from tests import test_odoo_testing_restore as restore_tests


class TestingPublicationTests(unittest.TestCase):
    database_url = ""
    source_mode: CandidateSource = "empty"

    def setUp(self) -> None:
        self.fixture = application_tests.CandidateApplicationTests()
        with patch.object(
            allocation_tests.TestingCandidateTests, "database_url", self.database_url
        ):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.fixture.execution(self.source_mode)
        readiness = self.fixture.readiness

        def witnessed(**kwargs: object) -> TestingCandidatePrepared:
            result = readiness(**kwargs)
            original = self.fixture.compiled.plan
            return result.model_copy(
                update={
                    "source_witness": generation_binding(original.generation)
                    if original.generation
                    else "empty:" + original.digest
                }
            )

        with patch.object(
            candidate_application, "candidate_private_readiness", side_effect=witnessed
        ):
            self.candidate = self.fixture.prepare_application()
        app = self.candidate.application
        assert app is not None and app.prepared is not None
        self.now = datetime.now(UTC)
        self.before = publication.read_snapshot(
            self.store, product=self.candidate.product, context=self.candidate.context
        )
        self.secrets = self.store.list_secret_records()
        self.versions = tuple(
            version
            for secret in self.secrets
            for version in self.store.list_secret_versions(secret_id=secret.secret_id)
        )
        routed = route_tests.TestingRouteTests()
        routed.setUp()
        host = str(urlsplit(app.plan.base_url).hostname)
        for router in routed.raw["routers"].values():
            router["rule"] = f"Host(`{host}`)"
        self.route = compile_testing_route(
            product=self.candidate.product,
            context=self.candidate.context,
            host=host,
            old_backend=routed.old,
            old_backend_addresses=(routed.old_address,),
            new_backend=app.prepared.private_endpoint,
            raw=routed.raw,
            public_entrypoints=("web", "websecure"),
        )
        self.reservation = self.store.reserve_mutation(
            scope="testing-rebuild",
            route_path="testing-rebuild",
            idempotency_key="fixture",
            request_fingerprint=app.plan.digest,
            lease_owner="fixture",
            lease_seconds=300,
            reconciliation_key=self.candidate.operation_id,
            provider_target_key="testing-lane:" + self.candidate.context,
        ).record
        self.plan = plan_testing_publication(
            self.store,
            candidate=self.candidate,
            before=self.before,
            route=self.route,
            reservation=self.reservation,
            now=self.now,
        )
        self.health = HealthcheckEvidence(
            verified=True,
            status="pass",
            urls=(app.plan.base_url + "/web/health",),
            timeout_seconds=10,
            runtime_identity_status="match",
            observed_runtime_identity=app.plan.runtime_identity,
        )
        self.observation = TestingRouteObservation(
            route_id=self.route.route_id,
            host=host,
            target_url=self.route.new_backend,
            slot_id=self.candidate.namespace,
            applied=True,
            authoritative_router_count=1,
            observed_at=self.now,
            public_health=self.health,
        )

    def current(self) -> TestingServingSnapshot:
        return publication.read_snapshot(
            self.store, product=self.candidate.product, context=self.candidate.context
        )

    def prepare(self) -> TestingPublicationRecord:
        return publication.prepare_publication(
            self.store, plan=self.plan, reservation=self.reservation
        )

    def publish(
        self, *, inverse: bool = False, observation: TestingRouteObservation | None = None
    ) -> TestingPublicationRecord:
        return publication.publish(
            self.store,
            plan=self.plan,
            reservation=self.reservation,
            now=self.now,
            inverse=inverse,
            observation=self.observation if observation is None else observation,
        )

    def test_canonical_local_database_matches_the_prepared_copy(self) -> None:
        self.prepare()
        self.publish()
        runtime = self.current().runtime
        assert runtime is not None
        self.assertEqual(
            runtime.env.get("ODOO_DB_HOST"),
            self.fixture.compiled.runtime_environment["ODOO_DB_HOST"],
        )
        self.assertEqual(
            runtime.env.get("ODOO_MAX_CRON_THREADS"),
            self.fixture.compiled.runtime_environment["ODOO_MAX_CRON_THREADS"],
        )

    def test_preparation_preserves_old_authority_then_adoption_is_atomic(self) -> None:
        self.prepare()
        self.assertEqual(self.current(), self.before)
        result = self.publish()
        self.assertEqual(self.current(), publication.effective_after(result))
        current_binding, planned_binding = self.current().binding, self.plan.after.binding
        assert current_binding is not None and planned_binding is not None
        self.assertEqual(current_binding.domains, planned_binding.domains)
        self.assertEqual(self.store.list_secret_records(), self.secrets)
        self.assertEqual(
            tuple(
                version
                for secret in self.secrets
                for version in self.store.list_secret_versions(secret_id=secret.secret_id)
            ),
            self.versions,
        )
        self.assertEqual(
            self.store.read_deployment_record(self.plan.deployment.record_id).destination_health,
            self.health,
        )
        for target in (self.candidate.compose_id, self.candidate.serving_target_id):
            with self.assertRaises(ValueError):
                self.store.write_provider_target_record(
                    self.plan.after.provider.model_copy(
                        update={"context": "foreign", "instance": "prod", "target_id": target}
                    )
                )
        self.assertEqual(self.publish(), result)

    def test_failed_transaction_restores_every_record_and_retry_is_safe(self) -> None:
        self.prepare()
        with patch.object(
            self.store,
            "_after_product_authority_bundle_step",
            side_effect=lambda name: (
                (_ for _ in ()).throw(TimeoutError("fixture"))
                if name == "testing_publication"
                else None
            ),
        ):
            with self.assertRaises(TimeoutError):
                self.publish()
        self.assertEqual(self.current(), self.before)
        self.assertEqual(self.prepare().state, "planned")
        self.assertEqual(self.publish().state, "applied")

    def test_lost_committed_reply_reconciles_and_inverse_restores_exact_preimage(self) -> None:
        self.prepare()
        committed = self.publish()
        # The caller lost this reply. A new store adapter observes the same receipt.
        self.assertEqual(self.publish(), committed)
        self.assertEqual(self.publish(inverse=True).state, "reversed")
        self.assertEqual(self.current(), self.before)
        self.assertEqual(self.publish(inverse=True).state, "reversed")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(
            tuple(
                version
                for secret in self.secrets
                for version in self.store.list_secret_versions(secret_id=secret.secret_id)
            ),
            self.versions,
        )

    def test_inverse_of_uncommitted_forward_proves_no_adoption(self) -> None:
        self.prepare()
        self.assertEqual(self.publish(inverse=True).state, "reversed")
        self.assertEqual(self.current(), self.before)

    def test_missing_unknown_or_stale_public_health_cannot_publish(self) -> None:
        self.prepare()
        for changes in (
            {"public_health": None},
            {"public_health": HealthcheckEvidence()},
            {"observed_at": self.now - timedelta(minutes=3)},
            {"target_url": self.route.old_backend},
            {
                "public_health": self.health.model_copy(
                    update={"urls": ("https://foreign.invalid/web",)}
                )
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.publish(observation=self.observation.model_copy(update=changes))
            self.assertEqual(self.current(), self.before)

    def test_upstream_restore_publishes_and_reverses_its_actual_generation(self) -> None:
        fixture = TestingPublicationTests()
        fixture.source_mode = "upstream"
        fixture.setUp()
        try:
            fixture.prepare()
            result = fixture.publish()
            self.assertIsNotNone(fixture.current().generation)
            self.assertEqual(fixture.current().generation, result.plan.after.generation)
            fixture.publish(inverse=True)
            self.assertEqual(fixture.current(), fixture.before)
        finally:
            fixture.doCleanups()

    def test_changed_generated_credential_metadata_refuses_without_adoption(self) -> None:
        self.prepare()
        record = next(
            item
            for item in self.secrets
            if item.context == self.candidate.context and item.instance == "testing"
        )
        self.store.write_secret_record(record.model_copy(update={"current_version_id": "rotated"}))
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.current(), self.before)
        self.assertEqual(self.publish(inverse=True).state, "reversed")
        self.assertEqual(self.current(), self.before)

    def test_foreign_compatibility_alias_refuses_even_without_a_physical_registry_row(self) -> None:
        self.prepare()
        self.store.write_dokploy_target_id_record(
            self.before.target_id.model_copy(
                update={
                    "context": "foreign",
                    "instance": "prod",
                    "target_id": self.candidate.compose_id,
                }
            )
        )
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.current(), self.before)

    def test_foreign_predecessor_alias_refuses_inverse_without_changing_serving(self) -> None:
        self.prepare()
        self.publish()
        serving = self.current()
        self.store.write_dokploy_target_id_record(
            self.before.target_id.model_copy(update={"context": "foreign", "instance": "prod"})
        )
        with self.assertRaises(ValueError):
            self.publish(inverse=True)
        self.assertEqual(self.current(), serving)
        self.assertEqual(self.prepare().state, "applied")

    def test_undelivered_global_change_does_not_block_publication(self) -> None:
        self.prepare()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="global", env={"ODOO_WORKERS": "4"}, updated_at=self.now.isoformat()
            )
        )
        self.assertEqual(self.publish().state, "applied")

    def test_runtime_insert_refuses_before_any_adoption(self) -> None:
        self.prepare()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=self.candidate.context,
                env={"ODOO_WORKERS": "4"},
                updated_at=self.now.isoformat(),
            )
        )
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.current(), self.before)
        self.assertEqual(self.publish(inverse=True).state, "reversed")
        self.assertEqual(self.current(), self.before)

    def test_uncommitted_inverse_preserves_a_later_serving_snapshot(self) -> None:
        self.prepare()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=self.candidate.context,
                instance="testing",
                env={"ODOO_WORKERS": "4"},
                updated_at=self.now.isoformat(),
            )
        )
        later = self.current()
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.publish(inverse=True).state, "reversed")
        self.assertEqual(self.current(), later)

    def test_new_plan_cannot_bind_settings_that_changed_since_private_preparation(self) -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=self.candidate.context,
                env={"ODOO_WORKERS": "4"},
                updated_at=self.now.isoformat(),
            )
        )
        self.plan = plan_testing_publication(
            self.store,
            candidate=self.candidate,
            before=self.before,
            route=self.route,
            reservation=self.reservation,
            now=self.now,
        )
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.current(), self.before)

    def test_replaced_or_expired_lease_cannot_publish(self) -> None:
        self.prepare()
        changed = self.reservation.model_copy(update={"lease_owner": "other"})
        with self.store._session_factory() as session:
            session.merge(self.store._idempotency_row(changed))
            session.commit()
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.current(), self.before)

    def test_changed_route_or_source_cannot_rebind_original_publication(self) -> None:
        self.prepare()
        generation = restore_tests.generation()
        generation = type(generation).model_validate(
            {
                **generation.model_dump(),
                "product": self.candidate.product,
                "context": self.candidate.context,
            }
        )
        with self.assertRaises(ValueError):
            TestingPublicationPlan.model_validate(
                {
                    **self.plan.model_dump(),
                    "after": {**self.plan.after.model_dump(), "generation": generation},
                }
            )
        route = type(self.route).model_validate(
            {**self.route.model_dump(), "new_backend": "http://different-private-copy:8069"}
        )
        with self.assertRaises(ValueError):
            TestingPublicationPlan.model_validate({**self.plan.model_dump(), "route": route})
        changed = self.plan.model_copy(update={"provider_target_key": "foreign"})
        with self.assertRaises(ValueError):
            publication.prepare_publication(self.store, plan=changed, reservation=self.reservation)
        self.assertEqual(self.current(), self.before)
