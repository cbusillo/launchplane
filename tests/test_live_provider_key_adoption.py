from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretRecord, SecretBinding
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _AsgiResponse,
    _post_product_config_apply,
    _RejectingVerifier,
)
from tests.support.auth import _local_operator_policy, scoped_agent_transport
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _seed_tracked_target_records, _sqlite_database_url


class LiveProviderKeyAdoptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_SECRET_KEYS_JSON": "",
                    "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "fictional-test-hmac-key",
                },
            )
        )
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_url = _sqlite_database_url(Path(directory.name) / "records.sqlite3")
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile = _generic_site_profile_payload()
        profile["production_use"] = "live"
        self.profile = LaunchplaneProductProfileRecord.model_validate(profile)
        self.store.write_product_profile_record(self.profile)
        self.original = RuntimeEnvironmentRecord(
            schema_version=2,
            scope="instance",
            context="example-site",
            instance="prod",
            env={"APP_EXISTING": "keep-me"},
            retired_provider_keys=("OLD_CREDENTIAL",),
            updated_at="2026-11-01T00:00:00Z",
        )
        self.store.write_runtime_environment_record(self.original)
        self.global_record = self.original.model_copy(
            update={
                "scope": "global",
                "context": "",
                "instance": "",
                "env": {"APP_LABEL": "global-label"},
                "retired_provider_keys": (),
            }
        )
        self.store.write_runtime_environment_record(self.global_record)
        self.secret = SecretRecord(
            secret_id="fictional-existing-secret",
            scope="context_instance",
            context="example-site",
            instance="prod",
            integration="runtime_environment",
            name="existing-credential",
            current_version_id="fictional-version",
            created_at=self.original.updated_at,
            updated_at=self.original.updated_at,
        )
        self.store.write_secret_record(self.secret)
        self.binding = SecretBinding(
            binding_id="fictional-existing-binding",
            secret_id=self.secret.secret_id,
            integration=self.secret.integration,
            context="example-site",
            instance="prod",
            binding_key="EXISTING_CREDENTIAL",
            created_at=self.original.updated_at,
            updated_at=self.original.updated_at,
        )
        self.store.write_secret_binding(self.binding)
        _seed_tracked_target_records(
            database_url=self.database_url,
            context="example-site",
            instance="prod",
            target_id="fictional-prod",
            target_type="application",
            target_name="fictional-site",
        )
        self.provider_env = {"APP_LABEL": "provider-label", "APP_COLOR": "blue"}
        self.fetch = self.enterContext(
            patch.object(dokploy_api, "fetch_dokploy_target_payload", side_effect=self._provider)
        )
        self.enterContext(
            patch.object(
                dokploy_source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "fictional-token"),
            )
        )
        self.provider_write = self.enterContext(
            patch.object(
                dokploy_api,
                "update_dokploy_target_env",
                side_effect=AssertionError("No provider writes"),
            )
        )
        self.enterContext(
            patch.object(
                dokploy_api,
                "dokploy_request",
                side_effect=AssertionError("No other provider calls"),
            )
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_local_operator_policy(
                actions=("product_config.plan", "product_config.apply"),
                products=("example-site",),
                contexts=("example-site",),
            ),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fictional-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

    def _provider(self, **kwargs: object) -> dict[str, str]:
        self.assertEqual(kwargs["target_id"], "fictional-prod")
        return {"env": "\n".join(f"{key}={value}" for key, value in self.provider_env.items())}

    def _payload(self, mode: str) -> dict[str, object]:
        return {
            "schema_version": 2,
            "mode": mode,
            "product": "example-site",
            "context": "example-site",
            "instance": "prod",
            "reason": "Record existing plain provider facts without changing them.",
            "runtime_env": {"adopt_provider_keys": list(self.provider_env)},
        }

    async def _post(self, mode: str, key: str = "", **updates: object) -> _AsgiResponse:
        return await _post_product_config_apply(
            self.app,
            {**self._payload(mode), **updates},
            authorization="Bearer fictional-operator-token",
            idempotency_key=key,
        )

    async def test_reviewed_live_adoption_records_current_provider_facts_without_live_effects(
        self,
    ) -> None:
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        self.assertEqual(
            self.store.list_runtime_environment_records(
                scope="instance", context_name="example-site", instance_name="prod"
            )[0],
            self.original,
        )
        with patch.object(
            self.store,
            "write_product_authority_bundle",
            wraps=self.store.write_product_authority_bundle,
        ) as write:
            applied = await self._post("apply", "record-facts")
        self.assertEqual(applied.status_code, 202, applied.text)
        bundle = write.call_args.args[0]
        self.assertFalse(
            bundle.model_copy(
                update={"runtime_environment_writes": (), "idempotency_record": None}
            ).requires_write()
        )
        self.assertEqual(applied.json()["result"]["next_actions"], [])
        self.assertEqual(applied.json()["result"]["status"], "ok")
        with closing(PostgresRecordStore(database_url=self.database_url)) as reader:
            actual = reader.list_runtime_environment_records(
                scope="instance", context_name="example-site", instance_name="prod"
            )[0]
            self.assertEqual(actual.env, {**self.original.env, **self.provider_env})
            self.assertEqual(actual.retired_provider_keys, self.original.retired_provider_keys)
            self.assertEqual(
                reader.list_runtime_environment_records(scope="global")[0], self.global_record
            )
            self.assertEqual(reader.list_secret_records(), (self.secret,))
            self.assertEqual(reader.list_secret_bindings(), (self.binding,))
            self.assertEqual(reader.read_product_profile_record("example-site"), self.profile)
        self.provider_write.assert_not_called()
        for response in (review, applied):
            for value in self.provider_env.values():
                self.assertNotIn(value, response.text)

    def _assert_original(self) -> None:
        self.assertEqual(
            self.store.list_runtime_environment_records(
                scope="instance", context_name="example-site", instance_name="prod"
            ),
            (self.original,),
        )
        self.assertEqual(self.store.list_secret_records(), (self.secret,))
        self.assertEqual(self.store.list_secret_bindings(), (self.binding,))
        self.provider_write.assert_not_called()

    async def test_values_retirement_and_secret_changes_cannot_use_record_only_review(self) -> None:
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        for changes in (
            {
                "runtime_env": {
                    "adopt_provider_keys": ["APP_LABEL"],
                    "env": {"APP_INJECTED": "new-value"},
                }
            },
            {"runtime_env": {"env": {"APP_LABEL": "provider-label"}}},
            {"runtime_env": {"adopt_provider_keys": ["APP_LABEL"], "retired_provider_keys": []}},
            {
                "runtime_env": {
                    "adopt_provider_keys": ["APP_LABEL"],
                    "retired_provider_keys": ["APP_COLOR"],
                }
            },
            {"secrets": [{"binding_key": "EXISTING_CREDENTIAL", "value": "arbitrary-new-secret"}]},
            {
                "disable_secret_bindings": [
                    {
                        "binding_key": "EXISTING_CREDENTIAL",
                        "expected_version_id": "fictional-version",
                    }
                ]
            },
        ):
            with self.subTest(changes=changes):
                response = await self._post("apply", "unsafe-request", **changes)
                self.assertIn(response.status_code, (400, 403, 409), response.text)
                self._assert_original()

    async def test_changed_provider_fact_or_review_never_records_an_unreviewed_value(self) -> None:
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        self.provider_env["APP_LABEL"] = "changed-after-review"
        changed = await self._post("apply", "changed-provider")
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["error"]["code"], "matching_dry_run_required")
        self._assert_original()
        self.provider_env["APP_LABEL"] = "provider-label"
        changed_request = await self._post(
            "apply", "changed-request", runtime_env={"adopt_provider_keys": ["APP_LABEL"]}
        )
        self.assertEqual(changed_request.status_code, 409, changed_request.text)
        self._assert_original()

    async def test_missing_review_or_action_and_scoped_actor_cannot_borrow_this_path(self) -> None:
        unreviewed = await self._post("apply", "unreviewed")
        self.assertEqual(unreviewed.status_code, 409, unreviewed.text)
        with scoped_agent_transport():
            scoped = await self._post("apply", "foreign-actor")
        self.assertEqual(scoped.status_code, 403, scoped.text)
        self.assertEqual(scoped.json()["error"]["code"], "live_product_requires_operator")
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fictional-operator-token",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        missing_action = await self._post("apply", "no-action")
        self.assertEqual(missing_action.status_code, 403, missing_action.text)
        self.assertEqual(missing_action.json()["error"]["code"], "authorization_denied")
        self._assert_original()

    async def test_credential_missing_and_template_retirement_dispositions_are_not_record_only(
        self,
    ) -> None:
        for value in ("https://user:password@example.invalid", "ghp_" + "a" * 36):
            with self.subTest(value=value):
                self.provider_env["APP_LABEL"] = value
                review = await self._post("dry-run")
                self.assertEqual(review.status_code, 202, review.text)
                refused = await self._post("apply", "credential-value")
                self.assertEqual(refused.status_code, 400, refused.text)
                self.assertNotIn(value, refused.text)
                self._assert_original()
        self.provider_env["APP_LABEL"] = "provider-label"
        missing = {"adopt_provider_keys": ["APP_MISSING"]}
        review = await self._post("dry-run", runtime_env=missing)
        self.assertEqual(review.status_code, 202, review.text)
        refused = await self._post("apply", "missing-provider-key", runtime_env=missing)
        self.assertEqual(refused.status_code, 400, refused.text)
        self._assert_original()
        with patch(
            "control_plane.product_config_service.odoo_compose_template_defaults",
            return_value={"APP_LABEL": "provider-label"},
        ):
            profile = self.profile.model_copy(update={"driver_id": "odoo"})
            self.store.write_product_profile_record(profile)
            target = self.store.read_dokploy_target_record(
                context_name="example-site", instance_name="prod"
            )
            self.store.write_dokploy_target_record(
                target.model_copy(update={"target_type": "compose"})
            )
            review = await self._post("dry-run")
            self.assertEqual(review.status_code, 202, review.text)
            refused = await self._post("apply", "template-retirement")
            self.assertEqual(refused.status_code, 403, refused.text)
            self._assert_original()

    async def test_wrong_owner_and_nested_target_cannot_adopt_another_lanes_facts(self) -> None:
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        for target in ({"context": "another-site"}, {"instance": "testing"}, {"scope": "context"}):
            with self.subTest(target=target):
                response = await self._post(
                    "apply",
                    "wrong-target",
                    runtime_env={"adopt_provider_keys": ["APP_LABEL"], **target},
                )
                self.assertIn(response.status_code, (400, 403), response.text)
                self._assert_original()
        foreign = self.profile.model_copy(update={"product": "other-site"})
        self.store.write_product_profile_record(foreign)
        refused = await self._post("apply", "wrong-owner")
        self.assertEqual(refused.status_code, 403, refused.text)
        self._assert_original()

    async def test_target_rebinding_invalidates_review_even_with_same_provider_values(self) -> None:
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        target = self.store.read_dokploy_target_id_record(
            context_name="example-site", instance_name="prod"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"target_id": "another-fictional-prod"})
        )
        self.fetch.side_effect = None
        self.fetch.return_value = {"env": "APP_LABEL=provider-label\nAPP_COLOR=blue"}
        refused = await self._post("apply", "rebound-target")
        self.assertEqual(refused.status_code, 409, refused.text)
        self._assert_original()

    async def test_same_key_replays_without_provider_read_and_changed_payload_conflicts(
        self,
    ) -> None:
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        applied = await self._post("apply", "stable-adoption")
        self.assertEqual(applied.status_code, 202, applied.text)
        self.fetch.reset_mock()
        self.provider_env["APP_LABEL"] = "later-provider-drift"
        replayed = await self._post("apply", "stable-adoption")
        self.assertEqual(replayed.status_code, 202, replayed.text)
        self.assertTrue(replayed.json()["replayed"])
        self.fetch.assert_not_called()
        conflict = await self._post(
            "apply", "stable-adoption", runtime_env={"adopt_provider_keys": ["APP_LABEL"]}
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_reused")
        self.fetch.assert_not_called()

    async def test_atomic_commit_refuses_a_concurrent_runtime_change(self) -> None:
        review = await self._post("dry-run")
        self.assertEqual(review.status_code, 202, review.text)
        write = self.store.write_product_authority_bundle

        def concurrent_write(
            bundle: ProductAuthorityBundle, *, identity: LaunchplaneIdentity | None = None
        ) -> None:
            self.store.write_runtime_environment_record(
                self.original.model_copy(
                    update={"env": {"APP_CONCURRENT": "keep-concurrent-value"}}
                )
            )
            write(bundle, identity=identity)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=concurrent_write
        ):
            refused = await self._post("apply", "concurrent-runtime")
        self.assertEqual(refused.status_code, 409, refused.text)
        actual = self.store.list_runtime_environment_records(
            scope="instance", context_name="example-site", instance_name="prod"
        )[0]
        self.assertEqual(actual.env, {"APP_CONCURRENT": "keep-concurrent-value"})
        self.assertEqual(self.store.list_secret_records(), (self.secret,))
        self.provider_write.assert_not_called()

    async def test_atomic_commit_rechecks_target_and_secret_metadata(self) -> None:
        target = self.store.read_dokploy_target_id_record(
            context_name="example-site", instance_name="prod"
        )
        write = self.store.write_product_authority_bundle
        for changed in ("target", "secret"):
            with self.subTest(changed=changed):
                self.store.write_dokploy_target_id_record(target)
                self.store.write_secret_binding(self.binding)
                review = await self._post("dry-run")
                self.assertEqual(review.status_code, 202, review.text)

                def concurrent_write(
                    bundle: ProductAuthorityBundle, *, identity: LaunchplaneIdentity | None = None
                ) -> None:
                    if changed == "target":
                        self.store.write_dokploy_target_id_record(
                            target.model_copy(update={"target_id": "changed-before-commit"})
                        )
                    else:
                        self.store.write_secret_binding(
                            self.binding.model_copy(update={"binding_key": "APP_LABEL"})
                        )
                    write(bundle, identity=identity)

                with patch.object(
                    self.store, "write_product_authority_bundle", side_effect=concurrent_write
                ):
                    refused = await self._post("apply", f"concurrent-{changed}")
                self.assertIn(refused.status_code, (400, 409), refused.text)
                self.assertEqual(
                    self.store.list_runtime_environment_records(
                        scope="instance", context_name="example-site", instance_name="prod"
                    ),
                    (self.original,),
                )
                self.provider_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
