import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_retirement import (
    ProductRetirementIdentity,
    ProductRetirementProviderObservation,
    ProductRetirementRecord,
    ProductRetirementRequest,
    provider_identifier_sha256,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.product_retirement import (
    DokployProductRetirementAdapter,
    ProductRetirementBlockedError,
    ProductRetirementStore,
    bind_product_retirement_authority,
    build_product_retirement_plan_record,
    build_provider_observation,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _generic_site_profile_payload


NOW = "2026-08-11T01:00:00Z"
TARGET_ID = "application-private-1"
TARGET_SHA256 = provider_identifier_sha256(TARGET_ID)


class _Lease:
    def __init__(self) -> None:
        self.phases: list[str] = []

    def assert_current(self) -> None:
        return None

    def checkpoint_effect(self, phase: str) -> None:
        self.phases.append(phase)


class _Store:
    def __init__(self) -> None:
        profile_payload = _generic_site_profile_payload()
        profile_payload["preview"] = {
            "enabled": False,
            "context": "example-site-preview",
        }
        self.profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
        self.provider_target: ProviderTargetRecord | None = ProviderTargetRecord(
            context="example-site",
            instance="prod",
            provider_id="dokploy",
            target_category="application",
            target_id=TARGET_ID,
            display_name="example-site-prod",
            provider_target_type="application",
            updated_at=NOW,
        )
        self.dokploy_target: DokployTargetRecord | None = DokployTargetRecord(
            context="example-site",
            instance="prod",
            target_type="application",
            target_name="example-site-prod",
            updated_at=NOW,
        )
        self.target_id: DokployTargetIdRecord | None = DokployTargetIdRecord(
            context="example-site",
            instance="prod",
            target_id=TARGET_ID,
            updated_at=NOW,
        )
        self.runtime_records: tuple[RuntimeEnvironmentRecord, ...] = (
            RuntimeEnvironmentRecord(
                scope="instance",
                context="example-site",
                instance="prod",
                env={"PORT": "3000"},
                updated_at=NOW,
            ),
        )
        self.previews: tuple[PreviewRecord, ...] = ()
        self.records: dict[str, object] = {}

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        return (self.profile,) if self.profile.is_active else ()

    def compare_and_write_product_profile_record(
        self,
        *,
        expected_record: LaunchplaneProductProfileRecord,
        replacement_record: LaunchplaneProductProfileRecord,
    ) -> object:
        if self.profile != expected_record:
            return SimpleNamespace(status="changed")
        self.profile = replacement_record
        return SimpleNamespace(status="written")

    def read_provider_target_record(self, **_: str) -> ProviderTargetRecord:
        if self.provider_target is None:
            raise FileNotFoundError
        return self.provider_target

    def read_dokploy_target_record(self, **_: str) -> DokployTargetRecord:
        if self.dokploy_target is None:
            raise FileNotFoundError
        return self.dokploy_target

    def read_dokploy_target_id_record(self, **_: str) -> DokployTargetIdRecord:
        if self.target_id is None:
            raise FileNotFoundError
        return self.target_id

    def list_runtime_environment_records(self, **_: str) -> tuple[RuntimeEnvironmentRecord, ...]:
        return self.runtime_records

    def list_secret_records(self, **_: object) -> tuple[object, ...]:
        return ()

    def list_preview_records(self, **_: object) -> tuple[PreviewRecord, ...]:
        return self.previews

    def delete_provider_target_record(self, **_: object) -> object:
        self.provider_target = None
        return None

    def delete_dokploy_target_record(self, **_: object) -> object:
        self.dokploy_target = None
        return None

    def delete_dokploy_target_id_record(self, **_: object) -> object:
        self.target_id = None
        return None

    def delete_runtime_environment_record_with_event(self, **_: object) -> object:
        self.runtime_records = ()
        return None

    def write_product_retirement_record(self, record: object) -> object:
        record_id = str(getattr(record, "record_id"))
        self.records[record_id] = record
        return None


def _request(*, mode: str = "plan", **overrides: object) -> ProductRetirementRequest:
    payload: dict[str, object] = {
        "mode": mode,
        "product": "example-site",
        "instance": "prod",
        "expected_target_sha256": TARGET_SHA256,
        "reason": "Retire an obsolete stable application.",
        "related_issue": "cbusillo/launchplane#2008",
    }
    payload.update(overrides)
    return ProductRetirementRequest.model_validate(payload)


def _observation(*, domains: tuple[str, ...] = ()) -> ProductRetirementProviderObservation:
    return build_provider_observation(
        target_id=TARGET_ID,
        payload={
            "applicationId": TARGET_ID,
            "name": "example-site-prod",
            "applicationStatus": "running",
        },
        domains=tuple(
            {"domainId": domain_id, "host": f"{domain_id}.example"} for domain_id in domains
        ),
        latest_deployment={"status": "done"},
        observed_at=NOW,
    )


def _plan(
    store: _Store, observation: ProductRetirementProviderObservation
) -> ProductRetirementRecord:
    request = _request()
    bound = bind_product_retirement_authority(
        record_store=cast(ProductRetirementStore, store),
        request=request,
    )
    return build_product_retirement_plan_record(
        request=request,
        identity=ProductRetirementIdentity(actor="test", identity_kind="test"),
        trace_id="plan-trace",
        idempotency_key="retire-example-site",
        requested_at=NOW,
        bound=bound,
        observation=observation,
    )


class ProductRetirementTests(unittest.TestCase):
    def test_plan_binds_exact_tracked_application_and_hashes_public_identity(self) -> None:
        store = _Store()
        plan = _plan(store, _observation())
        self.assertEqual(plan.context, "example-site")
        self.assertEqual(plan.provider_observation.target_id, TARGET_ID)
        self.assertEqual(plan.provider_observation.target_id_sha256, TARGET_SHA256)
        self.assertEqual(plan.outcome, "planned")

    def test_binding_rejects_non_application_and_active_preview(self) -> None:
        store = _Store()
        assert store.dokploy_target is not None
        store.dokploy_target = store.dokploy_target.model_copy(update={"target_type": "compose"})
        with self.assertRaisesRegex(ProductRetirementBlockedError, "exact tracked"):
            bind_product_retirement_authority(
                record_store=cast(ProductRetirementStore, store), request=_request()
            )
        store = _Store()
        store.previews = (
            PreviewRecord(
                preview_id="preview-1",
                context="example-site-preview",
                anchor_repo="every/example-site",
                anchor_pr_number=1,
                anchor_pr_url="https://github.com/every/example-site/pull/1",
                preview_label="launchplane-preview",
                canonical_url="https://pr-1.example.invalid",
                state="active",
                created_at=NOW,
                updated_at=NOW,
                eligible_at=NOW,
            ),
        )
        with self.assertRaisesRegex(ProductRetirementBlockedError, "active preview"):
            bind_product_retirement_authority(
                record_store=cast(ProductRetirementStore, store), request=_request()
            )

    def test_provider_observation_rejects_busy_application(self) -> None:
        observation = build_provider_observation(
            target_id=TARGET_ID,
            payload={"applicationId": TARGET_ID, "status": "deploying"},
            domains=(),
            latest_deployment={"status": "running"},
            observed_at=NOW,
        )
        self.assertFalse(observation.retirable)

    def test_provider_observation_rejects_unknown_states(self) -> None:
        observation = build_provider_observation(
            target_id=TARGET_ID,
            payload={"applicationId": TARGET_ID, "applicationStatus": "mystery"},
            domains=(),
            latest_deployment={"status": "mystery"},
            observed_at=NOW,
        )
        self.assertFalse(observation.retirable)

    def test_apply_transitions_before_effect_and_retires_authority(self) -> None:
        store = _Store()
        observation = _observation(domains=("domain-1",))
        plan = _plan(store, observation)
        request = _request(
            mode="apply",
            reviewed_plan_record_id=plan.record_id,
            reviewed_plan_sha256=plan.plan_sha256,
            confirmation=(f"retire product example-site instance prod target {TARGET_SHA256}"),
        )
        adapter = DokployProductRetirementAdapter(
            control_plane_root=Path("."),
            record_store=cast(ProductRetirementStore, store),
            request=request,
            plan=plan,
            identity=ProductRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="apply-trace",
            idempotency_key="retire-example-site",
            requested_at=NOW,
        )
        lease = _Lease()
        absent = observation.model_copy(
            update={
                "state": "absent",
                "application_fingerprint_sha256": "",
                "application_name_sha256": "",
                "project_reference_sha256": "",
                "domain_ids": (),
                "domain_id_sha256": (),
                "domain_host_sha256": (),
                "deployment_status": "",
                "retirable": False,
            }
        )
        with (
            patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                side_effect=(observation, absent),
            ),
            patch(
                "control_plane.product_retirement.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.invalid", "token"),
            ),
            patch("control_plane.product_retirement.dokploy_api.delete_dokploy_domain"),
            patch("control_plane.product_retirement.dokploy_api.delete_dokploy_application"),
        ):
            outcome = adapter.apply("provider-operation:test", lease)

        self.assertTrue(outcome.provider_effect_performed)
        self.assertEqual(lease.phases[0], "profile_retiring")
        self.assertEqual(store.profile.lifecycle_state, "retired")
        self.assertFalse(store.profile.preview.enabled)
        self.assertEqual(store.runtime_records, ())
        self.assertIsNone(store.provider_target)

    def test_partial_domain_failure_leaves_retiring_profile_for_reconciliation(self) -> None:
        store = _Store()
        observation = _observation(domains=("domain-1", "domain-2"))
        plan = _plan(store, observation)
        request = _request(
            mode="apply",
            reviewed_plan_record_id=plan.record_id,
            reviewed_plan_sha256=plan.plan_sha256,
            confirmation=(f"retire product example-site instance prod target {TARGET_SHA256}"),
        )
        adapter = DokployProductRetirementAdapter(
            control_plane_root=Path("."),
            record_store=cast(ProductRetirementStore, store),
            request=request,
            plan=plan,
            identity=ProductRetirementIdentity(actor="test", identity_kind="test"),
            trace_id="partial-trace",
            idempotency_key="retire-example-site",
            requested_at=NOW,
        )
        failure = Exception("lost provider response")
        with (
            patch(
                "control_plane.product_retirement.observe_tracked_dokploy_application",
                return_value=observation,
            ),
            patch(
                "control_plane.product_retirement.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.invalid", "token"),
            ),
            patch(
                "control_plane.product_retirement.dokploy_api.delete_dokploy_domain",
                side_effect=failure,
            ),
        ):
            with self.assertRaises(Exception):
                adapter.apply("provider-operation:test", _Lease())
        self.assertEqual(store.profile.lifecycle_state, "retiring")

    def test_filesystem_store_is_append_only_and_migrates_active_lifecycle(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir)
            profile_payload = _generic_site_profile_payload()
            profile_path = state_dir / "launchplane_product_profiles" / "example-site.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(json.dumps(profile_payload), encoding="utf-8")

            migrated = store.read_product_profile_record("example-site")
            self.assertEqual(migrated.lifecycle_state, "active")
            self.assertIn("lifecycle_state", profile_path.read_text(encoding="utf-8"))
            retiring = migrated.model_copy(
                update={
                    "lifecycle_state": "retiring",
                    "preview": migrated.preview.model_copy(update={"enabled": False}),
                }
            )
            store.compare_and_write_product_profile_record(
                expected_record=migrated,
                replacement_record=retiring,
            )
            self.assertEqual(store.list_product_profile_records(), ())
            self.assertEqual(
                store.read_product_profile_record("example-site").lifecycle_state,
                "retiring",
            )
            plan = _plan(_Store(), _observation())
            store.write_product_retirement_record(plan)
            store.write_product_retirement_record(plan)
            self.assertEqual(store.read_product_retirement_record(plan.record_id), plan)
            changed = plan.model_copy(update={"reason": "different"})
            with self.assertRaisesRegex(ValueError, "append-only"):
                store.write_product_retirement_record(changed)

    def test_postgres_store_persists_append_only_retirement_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{database_path}")
            store.ensure_schema()
            profile_payload = _generic_site_profile_payload()
            profile_payload["preview"] = {
                "enabled": False,
                "context": "example-site-preview",
            }
            retiring_profile = LaunchplaneProductProfileRecord.model_validate(
                {**profile_payload, "lifecycle_state": "retiring"}
            )
            store.write_product_profile_record(retiring_profile)
            self.assertEqual(store.list_product_profile_records(), ())
            self.assertEqual(
                store.read_product_profile_record("example-site").lifecycle_state,
                "retiring",
            )
            plan = _plan(_Store(), _observation())
            store.write_product_retirement_record(plan)
            self.assertEqual(store.read_product_retirement_record(plan.record_id), plan)
            self.assertEqual(
                store.list_product_retirement_records(product="example-site"),
                (plan,),
            )
            with self.assertRaisesRegex(ValueError, "append-only"):
                store.write_product_retirement_record(
                    plan.model_copy(update={"reason": "different"})
                )
            store.close()


if __name__ == "__main__":
    unittest.main()
