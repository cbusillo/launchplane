import unittest
from unittest.mock import patch

from control_plane.dokploy import api
from control_plane import dokploy_empty_automation as automation
from control_plane.dokploy_inventory import InventoryProject
from typing import Any

from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
from tests.test_odoo_first_empty_inputs import FirstEmptyInputsFixture
from tests.support.http import request


class EmptyAutomationTests(FirstEmptyInputsFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider_payload.update(
            {
                "autoDeploy": True,
                "environmentId": "environment-original",
                "name": self.target.target_name,
                "sourceType": "raw",
            }
        )
        self.projects = [
            InventoryProject.model_validate(
                {
                    "project_id": "project-original",
                    "name": "Original",
                    "environments": [
                        {
                            "environment_id": "environment-original",
                            "name": "production",
                            "targets": [
                                {
                                    "target_id": self.target_id.target_id,
                                    "category": "compose",
                                    "name": self.target.target_name,
                                    "placement": {
                                        "kind": "server",
                                        "server_id": "server-original",
                                        "server_name": "Original",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )
        ]
        self.enterContext(
            patch.object(
                automation, "read_dokploy_project_membership", side_effect=lambda **_: self.projects
            )
        )
        self.driver = self.enterContext(
            patch.object(api, "dokploy_request", side_effect=self.provider_request)
        )
        self.writes: list[dict[str, object]] = []

    def provider_request(self, *, path: str, **kwargs: Any) -> Any:
        if path == "/api/compose.update":
            self.writes.append(kwargs["payload"])
            self.provider_payload.update(kwargs["payload"])
            self.provider_payload["updatedAt"] = "2026-11-11T00:00:00Z"
            return {"autoDeploy": True}
        return []

    async def post(self, mode: str = "dry-run", key: str = "", **extra: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/dokploy-targets/setup",
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
            payload={
                "operation": "disable-empty-compose-auto-deploy",
                "context": self.context,
                "instance": "prod",
                "target_id": self.target_id.target_id,
                "project_id": "project-original",
                "environment_id": "environment-original",
                "server_id": "server-original",
                "expected_current_provider_target": self.provider.to_deployed_target_reference().model_dump(
                    mode="json"
                ),
                "reason": "Disable automatic deployment on original empty setup",
                "mode": mode,
                **extra,
            },
        )

    async def test_existing_original_target_can_review_only_automatic_deployment(self) -> None:
        response = await self.post()
        body = response.json()
        self.assertEqual(response.status_code, 202, body)
        self.assertIs(body["result"]["setup"]["automation_review"]["observed_auto_deploy"], True)

    def test_existing_target_intent_has_a_typed_request(self) -> None:
        request_model = DokployTargetSetupEnvelope.model_validate(
            {
                "operation": "disable-empty-compose-auto-deploy",
                "context": self.context,
                "instance": "prod",
                "target_id": self.target_id.target_id,
                "project_id": "project-original",
                "environment_id": "environment-original",
                "server_id": "server-original",
                "expected_current_provider_target": self.provider.to_deployed_target_reference(),
            }
        )
        self.assertEqual(request_model.target_id, self.target_id.target_id)

    async def review(self) -> dict[str, Any]:
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.json())
        return dict(response.json()["result"]["setup"]["automation_review"])

    async def apply_review(
        self, review: dict[str, Any], key: str = "correction", **extra: Any
    ) -> Any:
        return await self.post(
            "apply",
            key,
            automation_review=review,
            confirmation="APPLY DOKPLOY TARGET SETUP",
            **extra,
        )

    async def test_only_false_patch_and_independent_readback_preserve_all_other_state(self) -> None:
        before = dict(self.provider_payload)
        records = (self.profile, self.target, self.target_id, self.provider, self.runtime)
        response = await self.apply_review(await self.review())
        self.assertEqual(response.status_code, 202, response.json())
        self.assertTrue(response.json()["result"]["setup"]["automatic_deployment_disabled"])
        self.assertEqual(
            self.writes, [{"composeId": self.target_id.target_id, "autoDeploy": False}]
        )
        self.assertIs(self.provider_payload["autoDeploy"], False)
        self.assertEqual(
            {
                key: value
                for key, value in self.provider_payload.items()
                if key not in {"autoDeploy", "updatedAt"}
            },
            {key: value for key, value in before.items() if key not in {"autoDeploy", "updatedAt"}},
        )
        self.assertEqual(
            records,
            (
                self.store.read_product_profile_record(self.profile.product),
                self.store.read_dokploy_target_record(
                    context_name=self.context, instance_name="prod"
                ),
                self.store.read_dokploy_target_id_record(
                    context_name=self.context, instance_name="prod"
                ),
                self.store.read_provider_target_record(
                    context_name=self.context, instance_name="prod"
                ),
                next(
                    record
                    for record in self.store.list_runtime_environment_records()
                    if (record.scope, record.context, record.instance)
                    == ("instance", self.context, "prod")
                ),
            ),
        )
        self.assertFalse(
            self.store.list_deployment_records(context_name=self.context, instance_name="prod")
        )
        self.assertFalse(
            self.store.list_promotion_records(context_name=self.context, to_instance_name="prod")
        )
        self.assertFalse(
            self.store.list_secret_records(
                integration="runtime_environment", context_name=self.context
            )
        )

    async def test_already_false_is_an_independently_verified_noop(self) -> None:
        self.provider_payload["autoDeploy"] = False
        response = await self.apply_review(await self.review())
        self.assertEqual(response.status_code, 202, response.json())
        self.assertIs(self.provider_payload["autoDeploy"], False)
        self.assertEqual(self.writes, [])

    async def test_boolean_observation_is_required_before_and_after_effect(self) -> None:
        original = dict(self.provider_payload)
        for wrong in (None, 0, "false", "missing"):
            with self.subTest(wrong=wrong):
                self.provider_payload = dict(original)
                if wrong == "missing":
                    self.provider_payload.pop("autoDeploy")
                else:
                    self.provider_payload["autoDeploy"] = wrong
                response = await self.post()
                self.assertEqual(response.status_code, 409, response.json())
                self.assertEqual(self.writes, [])
        for index, wrong in enumerate((None, 0, "false", True, "missing")):
            with self.subTest(readback=wrong):
                self.provider_payload = dict(original)
                review = await self.review()

                def update(*, path: str, **kwargs: Any) -> Any:
                    if path == "/api/compose.update":
                        self.writes.append(kwargs["payload"])
                        if wrong == "missing":
                            self.provider_payload.pop("autoDeploy")
                        else:
                            self.provider_payload["autoDeploy"] = wrong
                        return {"autoDeploy": False}
                    return []

                with patch.object(api, "dokploy_request", side_effect=update):
                    response = await self.apply_review(review, key=f"bad-readback-{index}")
                self.assertEqual(response.status_code, 409, response.json())
                self.assertEqual(len(self.writes), 1)
                self.provider_payload["autoDeploy"] = False
                recovered = await self.post(
                    "readback", f"bad-readback-{index}", automation_review=review
                )
                self.assertEqual(recovered.status_code, 202, recovered.json())
                self.assertEqual(len(self.writes), 1)
                self.writes.clear()

    async def test_stale_review_and_changed_authority_or_placement_cannot_mutate(self) -> None:
        from datetime import datetime, timedelta

        review = await self.review()
        stale = {
            **review,
            "observed_at": (
                datetime.fromisoformat(review["observed_at"])
                - timedelta(seconds=automation.REVIEW_SECONDS)
            ).isoformat(),
        }
        response = await self.apply_review(stale, key="stale")
        self.assertEqual(response.status_code, 409, response.json())
        for field, value in (
            ("serverId", "foreign"),
            ("environmentId", "foreign"),
            ("appName", "changed"),
            ("autoDeploy", False),
            ("env", "PRIVATE_VALUE=do-not-output"),
            ("composeFile", "private-compose"),
            ("mounts", [{"private": "mount"}]),
        ):
            with self.subTest(field=field):
                before = self.provider_payload[field]
                self.provider_payload[field] = value
                response = await self.apply_review(review, key=f"changed-{field}")
                self.assertEqual(response.status_code, 409, response.json())
                self.assertNotIn("do-not-output", response.text)
                self.assertNotIn("private-compose", response.text)
                self.assertNotIn('private":', response.text)
                self.provider_payload[field] = before
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"production_use": "live"})
        )
        response = await self.apply_review(review, key="became-live")
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(self.writes, [])

    async def test_completed_replay_never_becomes_a_new_correction(self) -> None:
        review = await self.review()
        first = await self.apply_review(review)
        self.assertEqual(first.status_code, 202, first.json())
        self.provider_payload["autoDeploy"] = True
        with patch.object(
            api,
            "fetch_dokploy_target_payload",
            side_effect=AssertionError("completed replay must use stored evidence"),
        ):
            replay = await self.apply_review(review)
        self.assertEqual(replay.status_code, 202, replay.json())
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["result"], first.json()["result"])
        self.assertEqual(len(self.writes), 1)
        next_review = await self.review()
        next_result = await self.apply_review(next_review, key="distinct-reviewed-correction")
        self.assertEqual(next_result.status_code, 202, next_result.json())
        self.assertEqual(len(self.writes), 2)

    async def test_uncertain_provider_effect_requires_original_readback_without_repeat(
        self,
    ) -> None:
        review = await self.review()

        def lost(*, path: str, **kwargs: Any) -> Any:
            if path == "/api/compose.update":
                self.writes.append(kwargs["payload"])
                raise OSError("fictional lost provider response")
            return []

        with patch.object(api, "dokploy_request", side_effect=lost):
            response = await self.apply_review(review)
        self.assertEqual(response.status_code, 409, response.json())
        retry = await self.apply_review(review)
        self.assertEqual(retry.status_code, 409, retry.json())
        self.assertEqual(len(self.writes), 1)
        other = await self.apply_review(review, key="new-key-escape")
        self.assertEqual(other.status_code, 409, other.json())
        self.assertEqual(len(self.writes), 1)
        self.provider_payload["autoDeploy"] = False
        recovered = await self.post("readback", "correction", automation_review=review)
        self.assertEqual(recovered.status_code, 202, recovered.json())
        self.assertEqual(len(self.writes), 1)

    async def test_scoped_identity_cannot_borrow_director_setup_authority(self) -> None:
        from tests.support.auth import scoped_agent_transport

        with scoped_agent_transport():
            response = await self.post()
        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(self.writes, [])

    async def test_foreign_project_and_unknown_storage_or_provider_work_remain_protected(
        self,
    ) -> None:
        project = self.projects[0]
        self.projects[0] = project.model_copy(update={"project_id": "foreign"})
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.json())
        self.projects[0] = project
        with patch.object(api, "dokploy_request", return_value={}):
            response = await self.post()
        self.assertEqual(response.status_code, 409, response.json())
        self.probe.side_effect = lambda **kwargs: kwargs["receive"](
            {"type": "complete", "status": "unknown"}
        )
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(self.writes, [])

    async def test_recorded_use_only_copy_and_shared_target_refuse_before_provider_effects(
        self,
    ) -> None:
        from datetime import UTC, datetime, timedelta
        from control_plane.contracts.disposable_lane import DisposableLaneSafety

        now = datetime.now(UTC)
        for fields in (
            {"usage": "in_use", "usage_evidence": "invented staff use"},
            {"data": "only_copy", "data_evidence": "invented needed copy"},
            {"production_resources": "shared", "isolation_evidence": "invented shared storage"},
        ):
            self.store.write_disposable_lane_safety(
                DisposableLaneSafety.model_validate(
                    {
                        "product": self.profile.product,
                        "context": self.context,
                        "instance": "prod",
                        "observed_by": "fixture",
                        "observer_kind": "fixture",
                        "binding_sha256": "a" * 64,
                        "observed_at": now,
                        "expires_at": now + timedelta(seconds=60),
                        **fields,
                    }
                )
            )
            response = await self.post()
            self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(self.writes, [])
        self.probe.assert_not_called()

    async def test_shared_physical_target_refuses(self) -> None:
        original = self.target_id.model_copy(update={"context": "foreign", "instance": "testing"})
        self.store.write_dokploy_target_id_record(original)
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(self.writes, [])

    async def test_provider_deployment_history_and_schedules_cannot_be_treated_as_empty(
        self,
    ) -> None:
        for path, rows in (
            (
                "/api/deployment.allByType",
                [
                    {
                        "deploymentId": "previous",
                        "composeId": self.target_id.target_id,
                        "status": "done",
                        "startedAt": "2026-01-01T00:00:00Z",
                        "finishedAt": "2026-01-01T00:00:01Z",
                    }
                ],
            ),
            ("/api/schedule.list", [{"scheduleId": "previous"}]),
        ):
            with (
                self.subTest(path=path),
                patch.object(
                    api,
                    "dokploy_request",
                    side_effect=lambda **kwargs: rows if kwargs["path"] == path else [],
                ),
            ):
                response = await self.post()
                self.assertEqual(response.status_code, 409, response.json())
                self.assertEqual(self.writes, [])

    async def test_unresolved_original_setup_prevents_corrective_intent(self) -> None:
        from control_plane.dokploy_target_setup_http import first_production_target_lock_name

        original = self.store.reserve_mutation(
            scope="original-owner",
            route_path="/v1/dokploy-targets/setup",
            idempotency_key="original-allocation",
            request_fingerprint="original-setup",
            lease_owner="original-executor",
            lease_seconds=300,
            reconciliation_key="original-allocation-binding",
            provider_target_key=first_production_target_lock_name(self.context),
        ).record
        assert original is not None
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.json())
        self.store.mark_mutation_reconcile_required(
            reservation=original, reconciliation_key=original.reconciliation_key
        )
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(self.writes, [])

    async def test_authority_change_at_effect_admission_is_not_a_provider_write(self) -> None:
        review = await self.review()
        checkpoint = self.store.checkpoint_mutation_provider_effect

        def change_before_checkpoint(**kwargs: Any) -> Any:
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"production_use": "live"})
            )
            return checkpoint(**kwargs)

        with patch.object(
            self.store, "checkpoint_mutation_provider_effect", side_effect=change_before_checkpoint
        ):
            response = await self.apply_review(review)
        self.assertEqual(response.status_code, 409, response.json())
        self.assertEqual(self.writes, [])

    async def test_storage_change_and_missing_provider_field_invalidate_review(self) -> None:
        review = await self.review()
        self.storage_identity = "b" * 64
        response = await self.apply_review(review, key="changed-storage")
        self.assertEqual(response.status_code, 409, response.json())
        self.storage_identity = "a" * 64
        for field in (
            "env",
            "composeFile",
            "mounts",
            "serverId",
            "environmentId",
            "appName",
            "composeId",
        ):
            with self.subTest(field=field):
                value = self.provider_payload.pop(field)
                response = await self.post()
                self.assertEqual(response.status_code, 409, response.json())
                self.provider_payload[field] = value
        self.assertEqual(self.writes, [])
