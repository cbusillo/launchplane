"""Fresh generation continuation against real record transactions and fake provider IO."""

from pathlib import Path
from datetime import datetime, timedelta
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import ProductOdooLaneDataPolicy
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.odoo_testing_resources import OdooTestingResources
from control_plane.odoo_testing_restore import (
    TestingCredentialsOverlap,
    TestingCredentialVersionsMissing,
)
from control_plane.live_target_runtime import LiveTargetRuntimeError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
)
from control_plane.testing_replacement_bootstrap import (
    BootstrapPrerequisiteCode,
    TestingReplacementBootstrapAdapter,
    TestingReplacementBootstrapRequest,
    TestingBootstrapPrerequisiteError,
    bootstrap_plan,
)
from control_plane.testing_replacement_resources import (
    TestingReplacementResourceAdapter,
    TestingReplacementResourcesRequest,
)
from tests import test_testing_replacement_resources as resource_tests


class FreshTestingBootstrapScenario(unittest.TestCase):
    def seed(self, store_url: str, *, initial_generation: bool = False) -> None:
        self.fixture = resource_tests.TestingReplacementResourceHttpTests()
        self.fixture.database_url = store_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        self.store = fixture.store
        profile = self.store.read_product_profile_record(fixture.profile.product)
        source_lane = next(lane for lane in profile.lanes if lane.instance == "prod")
        self.profile = profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_data_policy": ProductOdooLaneDataPolicy(
                                data_authority="resettable",
                                allowed_rebuild_sources=("upstream_restore",),
                                upstream_source=f"{profile.product}:{source_lane.context}:prod",
                            )
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in profile.lanes
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.production_before = self.store.read_dokploy_target_record(
            context_name=fixture.context, instance_name="prod"
        )
        if initial_generation:
            self.legacy_operation = fixture.operation.model_copy(
                update={
                    "status": "pass",
                    "error_code": "",
                    "error_message": "",
                    "phase": "completed",
                    "finished_at": fixture.operation.updated_at,
                }
            )
            self.store.write_odoo_stable_target_replacement_operation_record(self.legacy_operation)
            fixture.operation = fixture.operation.model_copy(
                update={"operation_id": "initial-fixture"}
            )
            initial_request = TestingReplacementResourcesRequest(
                context=fixture.context,
                operation_id=fixture.operation.operation_id,
                initial_generation=True,
                reason="fixture isolated resources",
            )
            self.store.prepare_initial_testing_generation(
                product=profile.product, request=initial_request, prepared_by="fixture-director"
            )
            initial = self.store.read_disposable_resource_preparation(
                fixture.operation.operation_id
            )
            assert initial is not None
            self.store.activate_initial_testing_preparation(initial)
        else:
            self.assertTrue(
                self.store.prepare_disposable_testing_replacement(
                    fixture.operation, "fixture-director"
                )
            )
        allocation = self.store.read_disposable_resource_preparation(fixture.operation.operation_id)
        assert allocation is not None
        assigned = (
            allocation
            if initial_generation
            else allocation.model_copy(update={"resource_namespace": "lp-testing-" + "a" * 32})
        )
        self.store.compare_and_write_disposable_resource_preparation(
            expected=allocation, replacement=assigned
        )
        target = DokployTargetRecord(
            context=fixture.context,
            instance="testing",
            target_name=assigned.resource_namespace,
            updated_at=profile.updated_at,
        )
        target_id = DokployTargetIdRecord(
            context=fixture.context,
            instance="testing",
            target_id="fresh-compose",
            updated_at=profile.updated_at,
        )
        provider = ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        )
        with self.store.capture_disposable_resource_adoption(assigned, checkpoint=lambda: None):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    dokploy_targets=(target,),
                    dokploy_target_ids=(target_id,),
                    provider_target_writes=(
                        ProviderTargetWrite(
                            record=provider,
                            expected_record=self.store.read_provider_target_record(
                                context_name=fixture.context, instance_name="testing"
                            ),
                        ),
                    ),
                )
            )
        self.resources = OdooTestingResources(
            product=profile.product,
            context=fixture.context,
            operation_id=fixture.operation.operation_id,
            target_id=target_id.target_id,
            namespace=assigned.resource_namespace,
        )
        resource_adapter = TestingReplacementResourceAdapter(
            fixture.root,
            self.store,
            profile.product,
            TestingReplacementResourcesRequest(
                context=fixture.context,
                operation_id=fixture.operation.operation_id,
                reason="fixture isolated resources",
                initial_generation=initial_generation,
            ),
            "fixture",
        )
        resource_adapter.publish(self.resources, "fixture-schedule")
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=fixture.context,
                env={
                    "ODOO_UPSTREAM_HOST": "source.invalid",
                    "ODOO_UPSTREAM_USER": "restore-reader",
                    "ODOO_UPSTREAM_DB_USER": "postgres",
                    "ODOO_UPSTREAM_DB_NAME": "source_db",
                    "ODOO_UPSTREAM_FILESTORE_PATH": "/source/filestore/source_db",
                },
                updated_at=profile.updated_at,
            )
        )
        self.request = TestingReplacementBootstrapRequest(
            context=fixture.context,
            operation_id=fixture.operation.operation_id,
            domain_host="fresh.example.invalid",
            reason="prepare fixture sanitized source copy",
            initial_generation=initial_generation,
        )
        self.routes: list[dict[str, Any]] = []
        self.route_writes = 0
        self.lease = Mock(spec=["assert_current", "checkpoint_effect"])

    def route(self, **kwargs: Any) -> str:
        self.route_writes += 1
        self.routes[:] = [
            {
                "domainId": "fresh-domain",
                "host": kwargs["domain_host"],
                "composeId": kwargs["compose_id"],
                "serviceName": "web",
                "port": kwargs["runtime_port"],
                "https": True,
                "certificateType": kwargs["certificate_type"],
                "path": "/",
                "internalPath": "/",
                "stripPath": False,
            }
        ]
        return "fresh-domain"

    def patches(self) -> None:
        self.enterContext(
            patch("control_plane.odoo_testing_restore.require_distinct_testing_credentials")
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.require_distinct_testing_credentials"
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.source.read_dokploy_config",
                return_value=("provider.invalid", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.fetch_dokploy_compose_domains_for_target_setup",
                side_effect=lambda **_: tuple(self.routes),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.compose.ensure_compose_web_domain_route",
                side_effect=self.route,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.run_resource_schedule",
                side_effect=lambda **kw: kw["checkpoint"](),
            )
        )

    def adapter(self) -> TestingReplacementBootstrapAdapter:
        return TestingReplacementBootstrapAdapter(
            self.fixture.root, self.store, self.profile.product, self.request, "fixture-trace"
        )

    def apply(self) -> Any:
        return self.adapter().apply("fixture-original-key", self.lease)

    def review(self) -> None:
        plan = bootstrap_plan(self.store, self.profile.product, self.request)
        self.request = self.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
        )

    def scenario(self, fault: str) -> None:
        self.patches()
        self.review()
        original_bundle = self.store.write_product_public_hosts_bundle
        original_queue = self.store.request_fresh_testing_reconcile
        if fault == "route-receipt":

            def interrupted(bundle: Any, **kwargs: Any) -> None:
                def lose() -> None:
                    raise RuntimeError("fixture lost route reply")

                original_bundle(bundle, **{**kwargs, "verify_provider": lose})

            with patch.object(
                self.store, "write_product_public_hosts_bundle", side_effect=interrupted
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
        elif fault == "fence":
            with patch.object(
                self.store,
                "supersede_disposable_testing_replacement",
                side_effect=RuntimeError("fixture crash before supersession"),
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
        elif fault == "queue":
            with patch.object(
                self.store,
                "request_fresh_testing_reconcile",
                side_effect=RuntimeError("fixture crash before queue"),
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
        elif fault == "queue-commit":

            def lost_queue(generation: Any) -> None:
                original_queue(generation)
                raise RuntimeError("fixture lost queue reply")

            with patch.object(
                self.store, "request_fresh_testing_reconcile", side_effect=lost_queue
            ):
                with self.assertRaises(RuntimeError):
                    self.apply()
            self.assertEqual(
                self.adapter()
                .observe("key", "bind_independent_testing_route", "reconcile")
                .outcome,
                "present",
            )
            return
        if fault != "normal":
            from control_plane.odoo_testing_restore import read_current_testing_generation

            with self.assertRaises(ValueError):
                read_current_testing_generation(
                    self.store,
                    product=self.profile.product,
                    context=self.fixture.context,
                    instance="testing",
                )
            observed = self.adapter().observe("key", "bind_independent_testing_route", "reconcile")
            self.assertEqual(observed.outcome, "absent")
            self.assertTrue(observed.retry_safe)
        self.apply()
        self.assertEqual(self.route_writes, 1)
        result = self.adapter().readback()
        self.assertTrue(result["applied"])
        held = self.store.read_odoo_stable_target_replacement_operation_record(
            self.fixture.operation.operation_id
        )
        self.assertEqual(held.status, "superseded")
        self.assertIsNone(held.recovery_journal)
        self.assertEqual(held.request, self.fixture.operation.request)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="prod"
            ),
            self.production_before,
        )
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        assert generation is not None
        self.assertTrue(generation.reconcile_requested_at)
        queued = self.store.read_product_reconcile_request(f"{self.profile.product}:testing")
        self.store.request_fresh_testing_reconcile(generation)
        self.assertEqual(self.store.read_product_reconcile_request(queued.target_key), queued)


class FreshTestingBootstrapPrerequisiteScenario(FreshTestingBootstrapScenario):
    def first_execution_result_transition(self) -> None:
        from control_plane.contracts.odoo_instance_override_record import (
            OdooInstanceOverrideRecord,
            OdooConfigParameterOverride,
            OdooOverrideValue,
        )
        from control_plane.dokploy import api
        from control_plane.testing_replacement_recovery import (
            current_binding_fingerprint,
            guard_testing_replacement,
            testing_replacement_recovery_read,
            recover_stranded_testing_replacements,
        )
        from control_plane.odoo_testing_restore import fresh_testing_authority_sha256
        from control_plane.workflows.odoo_post_deploy import (
            _write_odoo_instance_override_apply_result,
        )
        from tests.test_testing_replacement_recovery import stamp
        from tests.test_product_reconcile import _inventory
        from control_plane.contracts.secret_record import SecretRecord, SecretVersion
        from control_plane.contracts.odoo_website_settings import (
            OdooWebsiteSettings,
            OdooWebsiteSettingsIntent,
        )
        from control_plane.odoo_instance_overrides import (
            render_post_deploy_payload,
        )
        from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PARAMETER

        self.patches()
        self.review()
        self.apply()
        self.store.write_repository_inventory_record(_inventory(repository=self.profile.repository))
        original = self.store.read_odoo_stable_target_replacement_operation_record(
            self.fixture.operation.operation_id
        )
        override = OdooInstanceOverrideRecord(
            context=self.fixture.context,
            instance="testing",
            updated_at=stamp(),
            config_parameters=(
                OdooConfigParameterOverride(
                    key="fixture.setting",
                    value=OdooOverrideValue(source="literal", value="initial"),
                ),
                OdooConfigParameterOverride(
                    key=WEBSITE_INDEXING_PARAMETER,
                    value=OdooOverrideValue(source="literal", value="0"),
                ),
            ),
        )
        self.store.write_odoo_instance_override_record(override)
        secret = SecretRecord(
            secret_id="fixture-local",
            scope="context_instance",
            integration="runtime_environment",
            name="fixture",
            context=override.context,
            instance="testing",
            current_version_id="fixture-v1",
            created_at=stamp(),
            updated_at=stamp(),
        )
        for version in (secret.current_version_id, "fixture-v2"):
            self.store.write_secret_version(
                SecretVersion(
                    version_id=version,
                    secret_id=secret.secret_id,
                    ciphertext="opaque-fixture-ciphertext",
                    created_at=stamp(),
                )
            )
        self.store.write_secret_record(secret)
        operation = original.model_copy(
            update={
                "operation_id": "fresh-first-execution",
                "idempotency_key": "first-execution-key",
                "status": "running",
                "phase": "running",
                "started_at": stamp(),
                "updated_at": stamp(),
                "lease_owner": "worker",
                "lease_expires_at": stamp(600),
                "attempt": 1,
                "disposable_supersession": None,
                "request": original.request.model_copy(
                    update={"data_source_mode": "upstream_restore"}
                ),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        with (
            guard_testing_replacement(
                self.store, operation, "worker", lambda _: None
            ) as checkpoint,
            patch.object(api, "_dokploy_request", return_value={}),
        ):
            api.dokploy_request(
                host="https://provider.invalid",
                token="fixture",
                path="/api/compose.update",
                method="POST",
                payload={"composeId": self.resources.target_id},
            )
            before = self.store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            assert before.recovery_journal is not None
            # Preserve the exact old full-record fingerprint to prove an old
            # journal cannot acquire the new meaning after its result changes.
            generation = self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            )
            assert generation is not None
            legacy = before.model_copy(
                update={
                    "recovery_journal": before.recovery_journal.model_copy(
                        update={
                            "target_fingerprint": fresh_testing_authority_sha256(
                                self.store, generation, include_override_results=True
                            )
                        }
                    )
                }
            )
            _write_odoo_instance_override_apply_result(
                record_store=self.store,
                record=override,
                status="fail",
                detail="Native testing copy integrity or recovery is unverified.",
            )
            checkpoint("post_deploy_result_published")
        current = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        assert current.recovery_journal is not None
        self.assertEqual(
            current_binding_fingerprint(self.store, current),
            current.recovery_journal.target_fingerprint,
        )
        assert legacy.recovery_journal is not None
        self.assertNotEqual(
            current_binding_fingerprint(self.store, legacy),
            legacy.recovery_journal.target_fingerprint,
        )
        held = current.model_copy(
            update={
                "status": "reconciliation_required",
                "lease_owner": "",
                "lease_expires_at": "",
                "heartbeat_at": "",
                "finished_at": "",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(held)
        for field, value in (("reason", "changed request"), ("source_mode", "container-export")):
            with self.subTest(request_field=field), self.assertRaises(ValueError):
                changed_adapter = self.adapter()
                changed_adapter.request = self.request.model_copy(update={field: value})
                changed_adapter.readback()
        # A new executing generation must not invalidate the original bootstrap
        # receipt, and reading it must not requeue, recapture or repeat a route.
        with patch.object(
            self.store,
            "retained_disposable_hostnames",
            return_value=frozenset({generation.domain_host}),
        ):
            with self.assertRaises(ValueError):
                bootstrap_plan(self.store, self.profile.product, self.request)
            self.assertTrue(self.adapter().readback()["applied"])
        self.assertEqual(self.route_writes, 1)
        with patch(
            "control_plane.testing_replacement_recovery.inspect_testing_replacement",
            return_value={"state": "quiescent_safe_to_retry"},
        ):
            readback = testing_replacement_recovery_read(self.store, held, self.fixture.root)
            self.assertEqual(readback["status"], "eligible", readback)
        baseline_override = self.store.read_odoo_instance_override_record(
            context_name=override.context, instance_name="testing"
        )
        changed = baseline_override.model_copy(deep=True)
        changed.config_parameters[0].value.value = "changed-intent"
        history_changed = baseline_override.model_copy(
            update={
                "last_apply": baseline_override.last_apply.model_copy(
                    update={
                        "website_settings": OdooWebsiteSettingsIntent(
                            artifact_id="fixture-artifact",
                            settings=OdooWebsiteSettings(
                                website_id=1, public_origin="https://fixture.invalid"
                            ),
                        )
                    }
                )
            }
        )
        self.assertNotEqual(
            render_post_deploy_payload(baseline_override),
            render_post_deploy_payload(history_changed),
        )
        profile = self.store.read_product_profile_record(self.profile.product)
        runtime = self.store.list_runtime_environment_records(
            scope="context", context_name=override.context
        )[0]
        target = self.store.read_dokploy_target_record(
            context_name=override.context, instance_name="testing"
        )
        changes: tuple[tuple[Any, Any, Any], ...] = (
            (self.store.write_odoo_instance_override_record, changed, baseline_override),
            (self.store.write_odoo_instance_override_record, history_changed, baseline_override),
            (
                self.store.write_product_profile_record,
                profile.model_copy(update={"display_name": "changed product"}),
                profile,
            ),
            (
                self.store.write_runtime_environment_record,
                runtime.model_copy(
                    update={"env": {**runtime.env, "ODOO_UPSTREAM_DB_NAME": "different-source"}}
                ),
                runtime,
            ),
            (
                self.store.write_dokploy_target_record,
                target.model_copy(
                    update={"env": {**target.env, "ODOO_DATA_VOLUME": "different-storage"}}
                ),
                target,
            ),
            (
                self.store.write_secret_record,
                secret.model_copy(update={"current_version_id": "fixture-v2"}),
                secret,
            ),
        )
        for write, replacement, baseline in changes:
            with self.subTest(record=type(baseline).__name__):
                write(replacement)
                with patch(
                    "control_plane.testing_replacement_recovery.inspect_testing_replacement"
                ) as inspect:
                    refusal = testing_replacement_recovery_read(self.store, held, self.fixture.root)
                    self.assertEqual(refusal["status"], "refused", refusal)
                    inspect.assert_not_called()
                write(baseline)
        legacy_held = held.model_copy(update={"recovery_journal": legacy.recovery_journal})
        self.store.write_odoo_stable_target_replacement_operation_record(legacy_held)
        with patch(
            "control_plane.testing_replacement_recovery.inspect_testing_replacement"
        ) as inspect:
            self.assertEqual(
                testing_replacement_recovery_read(self.store, legacy_held, self.fixture.root)[
                    "reason"
                ],
                "recovery_binding_changed",
            )
            inspect.assert_not_called()
        self.store.write_odoo_stable_target_replacement_operation_record(held)
        # Exercise the real store's final authority check and publication, not
        # just the read-time eligibility classification.
        assert legacy.recovery_journal is not None
        retried = held.model_copy(
            update={
                "status": "running",
                "phase": "running",
                "attempt": held.attempt + 1,
                "lease_owner": "worker",
                "lease_expires_at": stamp(600),
                "recovery_journal": legacy.recovery_journal,
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(retried)
        with guard_testing_replacement(self.store, retried, "worker", lambda _: None) as checkpoint:
            checkpoint("new_attempt")
            _write_odoo_instance_override_apply_result(
                record_store=self.store,
                record=baseline_override,
                status="pass",
                detail="fixture recovered",
            )
            checkpoint("new_attempt_result")
        retried = self.store.read_odoo_stable_target_replacement_operation_record(held.operation_id)
        held = retried.model_copy(
            update={
                "status": "reconciliation_required",
                "lease_owner": "",
                "lease_expires_at": "",
                "heartbeat_at": "",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(held)
        cancel = self.store.cancel_pending_odoo_stable_target_replacement_operation_record

        def rotate_before_commit(*args: Any, **kwargs: Any) -> bool:
            self.store.write_secret_record(
                secret.model_copy(update={"current_version_id": "fixture-v2"})
            )
            return cancel(*args, **kwargs)

        with (
            patch.object(
                self.store,
                "cancel_pending_odoo_stable_target_replacement_operation_record",
                side_effect=rotate_before_commit,
            ),
            patch(
                "control_plane.testing_replacement_recovery.inspect_testing_replacement",
                return_value={"state": "quiescent_safe_to_retry"},
            ),
        ):
            self.assertEqual(
                recover_stranded_testing_replacements(self.store, self.fixture.root), ()
            )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(held.operation_id), held
        )
        self.store.write_secret_record(secret)
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import DBAPIError
        from psycopg.errors import LockNotAvailable

        peer = PostgresRecordStore(
            database_url=make_url(self.store.database_url)
            .update_query_dict({"options": "-c lock_timeout=100ms"})
            .render_as_string(hide_password=False)
        )
        self.addCleanup(peer.close)
        excluded: list[bool] = []
        fingerprint = current_binding_fingerprint

        def check_writer_exclusion(store: Any, record: Any) -> str:
            if getattr(store._disposable_authority_context, "session", None) is not None:
                with self.assertRaises(DBAPIError) as blocked:
                    peer.write_secret_record(
                        secret.model_copy(update={"current_version_id": "fixture-v2"})
                    )
                self.assertIsInstance(blocked.exception.orig, LockNotAvailable)
                excluded.append(True)
            return fingerprint(store, record)

        with (
            patch(
                "control_plane.testing_replacement_recovery.inspect_testing_replacement",
                return_value={"state": "quiescent_safe_to_retry"},
            ),
            patch(
                "control_plane.testing_replacement_recovery.current_binding_fingerprint",
                side_effect=check_writer_exclusion,
            ),
        ):
            self.assertEqual(
                recover_stranded_testing_replacements(self.store, self.fixture.root),
                (held.operation_id,),
            )
        self.assertTrue(excluded)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                held.operation_id
            ).status,
            "cancelled",
        )
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            ),
            generation,
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="prod"
            ),
            self.production_before,
        )

    def native_capture_custody(self) -> None:
        from control_plane.odoo_testing_native_source import native_testing_source_authority
        from control_plane.contracts.odoo_testing_source import (
            NativeOdooTestingSource,
            NativeTestingCopyCapture,
            NativeTestingCopyArtifact,
        )
        from control_plane.odoo_testing_restore import generation_binding

        self.patches()
        source_context = next(
            lane.context for lane in self.profile.lanes if lane.instance == "prod"
        )
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=source_context,
                instance="prod",
                env={
                    "ODOO_DB_NAME": "source_database",
                    "ODOO_FILESTORE_PATH": "/volumes/data/filestore",
                },
                updated_at=self.profile.updated_at,
            )
        )
        self.request = self.request.model_copy(update={"source_mode": "container-export"})
        self.review()
        self.apply()
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        assert generation is not None
        authority = native_testing_source_authority(
            self.store,
            profile=self.store.read_product_profile_record(self.profile.product),
            testing_context=self.fixture.context,
        )
        self.assertEqual(authority.binding_sha256, generation.source_binding_sha256)
        # An unrelated production setting or bookkeeping update does not change
        # the exact database/filestore/target that this read was reviewed for.
        source_runtime = next(
            row
            for row in self.store.list_runtime_environment_records(context_name=source_context)
            if row.scope == "instance" and row.instance == "prod"
        )
        self.store.write_runtime_environment_record(
            source_runtime.model_copy(
                update={
                    "env": {**source_runtime.env, "UNRELATED_SETTING": "fixture-update"},
                    "updated_at": (
                        datetime.fromisoformat(source_runtime.updated_at) + timedelta(seconds=1)
                    ).isoformat(),
                }
            )
        )
        self.assertEqual(
            native_testing_source_authority(
                self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
                testing_context=self.fixture.context,
            ).binding_sha256,
            authority.binding_sha256,
        )
        observed = NativeOdooTestingSource(
            **authority.model_dump(mode="json"),
            server_id="source",
            compose_project="source-project",
            database_container="a" * 64,
            web_container="b" * 64,
            database_image="sha256:" + "c" * 64,
            web_image="sha256:" + "d" * 64,
        )
        capture = NativeTestingCopyCapture(
            product=generation.product,
            context=generation.context,
            generation_binding_sha256=generation_binding(generation),
            source_binding_sha256=authority.binding_sha256,
            capture_id="e" * 32,
            database=NativeTestingCopyArtifact(size=10, sha256="f" * 64),
            filestore=NativeTestingCopyArtifact(size=20, sha256="a" * 64),
            captured_at="fixture",
            source_observation=observed,
        )
        self.store.write_native_testing_copy_capture(capture)
        self.store.write_native_testing_copy_capture(capture)
        self.assertEqual(
            self.store.read_native_testing_copy_capture(generation_binding(generation)), capture
        )
        with self.assertRaises(ValueError):
            self.store.write_native_testing_copy_capture(
                capture.model_copy(update={"capture_id": "d" * 32})
            )
        with self.assertRaises(ValueError):
            self.store.write_native_testing_copy_capture(
                capture.model_copy(update={"generation_binding_sha256": "b" * 64})
            )
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=source_context, instance_name="prod"
            ),
            self.production_before,
        )

        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=source_context,
                instance="prod",
                env={"ODOO_DB_NAME": "later_production_database"},
                updated_at="2026-01-02T00:00:00Z",
            )
        )
        self.assertNotEqual(
            native_testing_source_authority(
                self.store,
                profile=self.store.read_product_profile_record(self.profile.product),
                testing_context=self.fixture.context,
            ).binding_sha256,
            capture.source_binding_sha256,
        )
        self.assertEqual(
            self.adapter().readback()["source_binding_sha256"], capture.source_binding_sha256
        )

    def credential_comparison_failure(self) -> None:
        self.patches()
        for dependency, failure, expected in (
            (
                "evaluate_runtime_key_safety_for_live_target_sync",
                LiveTargetRuntimeError("private fixture credential"),
                BootstrapPrerequisiteCode.CREDENTIALS_UNSAFE,
            ),
            (
                "require_distinct_testing_credentials",
                TestingCredentialVersionsMissing("private fixture credential"),
                BootstrapPrerequisiteCode.CREDENTIAL_VERSIONS_MISSING,
            ),
            (
                "require_distinct_testing_credentials",
                TestingCredentialsOverlap("private fixture credential"),
                BootstrapPrerequisiteCode.CREDENTIALS_OVERLAP,
            ),
            (
                "require_distinct_testing_credentials",
                ValueError("private fixture credential"),
                BootstrapPrerequisiteCode.CREDENTIALS_UNVERIFIED,
            ),
        ):
            with (
                self.subTest(prerequisite=expected),
                patch(
                    f"control_plane.testing_replacement_bootstrap.{dependency}", side_effect=failure
                ),
                patch(
                    "control_plane.testing_replacement_bootstrap.encoded_restore_source"
                ) as source,
                self.assertRaises(TestingBootstrapPrerequisiteError) as caught,
            ):
                bootstrap_plan(self.store, self.profile.product, self.request)
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn("private fixture credential", str(caught.exception))
            source.assert_not_called()

    def missing_source_failure(self) -> None:
        self.patches()
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context=self.fixture.context,
                env={"FIXTURE_SETTING": "present"},
                updated_at=self.profile.updated_at,
            )
        )
        with self.assertRaises(TestingBootstrapPrerequisiteError) as caught:
            bootstrap_plan(self.store, self.profile.product, self.request)
        self.assertEqual(
            caught.exception.code,
            BootstrapPrerequisiteCode.RESTORE_SOURCE_UNCONFIGURED,
        )
        self.assertIsNone(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            )
        )
        self.assertEqual(self.routes, [])


class FreshTestingBootstrapObservationTests(unittest.TestCase):
    def test_unknown_dispatched_route_is_never_blindly_repeated(self) -> None:
        store = Mock(spec=PostgresRecordStore)
        from tests.test_odoo_testing_restore import generation

        store.read_fresh_odoo_testing_generation.return_value = generation()
        adapter = TestingReplacementBootstrapAdapter(
            Path("."),
            store,
            "example-odoo",
            TestingReplacementBootstrapRequest(
                context="example",
                operation_id="held-example",
                domain_host="fresh.example.invalid",
                reason="recover fixture route",
            ),
            "fixture",
        )
        with (
            patch.object(adapter, "readback", side_effect=ValueError),
            patch.object(adapter, "_domains", return_value=()),
            patch("control_plane.testing_replacement_bootstrap.bootstrap_plan"),
        ):
            observation = adapter.observe("key", "bind_independent_testing_route", "reconcile")
            self.assertEqual(observation.outcome, "unknown")
            self.assertFalse(observation.retry_safe)


class TestingBootstrapHttpRefusalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = resource_tests.TestingReplacementResourceHttpTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    async def post(self, **changes: object) -> Any:
        from tests.support.http import request
        from control_plane.testing_replacement_bootstrap import ROUTE

        return await request(
            self.fixture.app,
            "POST",
            ROUTE.format(product=self.fixture.profile.product),
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "original-key",
            },
            payload={
                "context": self.fixture.context,
                "operation_id": self.fixture.operation.operation_id,
                "domain_host": "fresh.example.invalid",
                "reason": "fixture copy",
                **changes,
            },
        )

    async def test_wrong_context_operation_production_and_client_never_reach_provider(self) -> None:
        from tests.support.auth import scoped_agent_transport

        with patch(
            "control_plane.testing_replacement_bootstrap.source.read_dokploy_config"
        ) as provider:
            for changes in (
                {"instance": "prod"},
                {"context": "another"},
                {"operation_id": "missing"},
            ):
                with self.subTest(changes=changes):
                    self.assertGreaterEqual((await self.post(**changes)).status_code, 400)
            with scoped_agent_transport():
                self.assertEqual((await self.post()).status_code, 403)
            provider.assert_not_called()
        self.assertIsNone(
            self.fixture.store.read_fresh_odoo_testing_generation(
                self.fixture.profile.product, self.fixture.context
            )
        )

    async def test_sqlite_cannot_publish_bootstrap_or_private_failure_details(self) -> None:
        for mode in ("apply", "readback"):
            result = await self.post(mode=mode, reviewed_plan_sha256="a" * 64)
            self.assertEqual(result.status_code, 503, result.text)
        with patch(
            "control_plane.http_routes.testing_replacement_bootstrap.bootstrap_plan",
            side_effect=ValueError("private-value"),
        ):
            result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertNotIn("private-value", result.text)
        self.assertNotIn(result.json()["error"]["code"], tuple(BootstrapPrerequisiteCode))

    async def test_dry_run_returns_the_prerequisite_classification(self) -> None:
        prerequisite = TestingBootstrapPrerequisiteError(
            BootstrapPrerequisiteCode.CREDENTIALS_UNVERIFIED
        )
        with patch(
            "control_plane.http_routes.testing_replacement_bootstrap.bootstrap_plan",
            side_effect=prerequisite,
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], prerequisite.code)
