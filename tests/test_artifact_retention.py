from datetime import UTC, datetime, timedelta
from unittest import IsolatedAsyncioTestCase, TestCase

from control_plane.artifact_retention import RetentionVersion, plan_retention, read_package_versions
from control_plane.contracts.protected_artifacts import ProtectedArtifactSet

IMAGE = "ghcr.io/example/site"
NOW = datetime(2026, 10, 16, tzinfo=UTC)


def version(number: int, *, tags: tuple[str, ...] | None = None, age: int = 60) -> RetentionVersion:
    return RetentionVersion(
        version_id=number,
        digest="sha256:" + f"{number:064x}",
        tags=tags if tags is not None else ("sha-" + f"{number:040x}",),
        created_at=NOW - timedelta(days=age),
    )


class ArtifactRetentionTests(TestCase):
    def test_protected_old_recent_unknown_and_untagged_retained(self) -> None:
        protected = ProtectedArtifactSet(
            image_digests=(version(1).digest,),
            image_references=(f"{IMAGE}:pr-2",),
        )
        versions = (
            version(1),
            version(2, tags=("pr-2",)),
            version(3),
            version(4, age=1),
            version(5, tags=("release-1",)),
            version(6, tags=()),
            version(7),
        )
        plan = plan_retention(
            image_repository=IMAGE,
            protected=protected,
            versions=versions,
            now=NOW,
            retain_count=1,
        )
        self.assertEqual(set(plan.candidates), {3, 7})
        self.assertEqual(set(plan.retained), {1, 2, 4, 5, 6})

    def test_inventory_warning_and_duplicate_reads_never_yield_candidates(self) -> None:
        plan = plan_retention(
            image_repository=IMAGE,
            protected=ProtectedArtifactSet(warnings=("missing artifact",)),
            versions=(version(1),),
            now=NOW,
        )
        self.assertEqual((plan.status, plan.candidates), ("unavailable", ()))
        with self.assertRaises(ValueError):
            plan_retention(
                image_repository=IMAGE,
                protected=ProtectedArtifactSet(),
                versions=(version(1), version(1)),
                now=NOW,
            )

    def test_package_read_requires_exact_repository_and_complete_inventory(self) -> None:
        class Transport:
            def __init__(self) -> None:
                self.repository_id = 42
                self.full_pages = False
                self.paths: list[str] = []

            def get_bytes(self, path: str) -> bytes:
                raise AssertionError("Retention does not download build artifacts")

            def get_json(self, path: str) -> object:
                self.paths.append(path)
                if path == "/users/example":
                    return {"type": "User"}
                if "?" not in path:
                    return {
                        "name": "site",
                        "package_type": "container",
                        "repository": {"id": self.repository_id},
                    }
                item = {
                    "id": 1,
                    "name": version(1).digest,
                    "created_at": version(1).created_at.isoformat(),
                    "metadata": {"container": {"tags": list(version(1).tags)}},
                }
                return [item] * (100 if self.full_pages else 1)

        transport = Transport()
        self.assertEqual(
            read_package_versions(transport=transport, image_repository=IMAGE, repository_id="42"),
            (version(1),),
        )
        transport.repository_id = 43
        with self.assertRaises(ValueError):
            read_package_versions(transport=transport, image_repository=IMAGE, repository_id="42")
        transport.repository_id = 42
        transport.full_pages = True
        with self.assertRaises(ValueError):
            read_package_versions(transport=transport, image_repository=IMAGE, repository_id="42")
        self.assertTrue(all(not path.startswith("http") for path in transport.paths))


class ArtifactRetentionHttpTests(IsolatedAsyncioTestCase):
    async def test_existing_product_wide_read_authority_guards_provider_access(self) -> None:
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from control_plane.artifact_retention import ArtifactRetentionPlan
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.storage.filesystem import FilesystemRecordStore
        from tests.http_app_test_support import (
            _RejectingVerifier,
            _local_operator_artifact_protection_policy,
            _local_operator_bearer_config,
        )
        from tests.support.http import get
        from tests.support.protected_artifacts import seed_protected_artifact_store

        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            seed_protected_artifact_store(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_local_operator_artifact_protection_policy(
                    products=("verireel",), contexts=("*",)
                ),
                record_store_factory=lambda: store,
                bearer_identity_config=_local_operator_bearer_config(),
            )
            with patch("control_plane.http_routes.products.product_retention_dry_run") as planner:
                planner.return_value = ArtifactRetentionPlan(
                    status="available",
                    image_repository=IMAGE,
                    observed_at=NOW.isoformat(),
                    candidates=(1,),
                    retained=(2,),
                )
                response = await get(
                    app,
                    "/v1/artifacts/retention?product=verireel",
                    headers={"Authorization": "Bearer local-operator-token"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["retention"]["candidates"], [1])
                planner.assert_called_once()
                from control_plane.artifact_scan import ArtifactScanResult

                with patch(
                    "control_plane.http_routes.products.qualify_product_artifact"
                ) as scanner:
                    scanner.return_value = ArtifactScanResult(
                        image_reference=IMAGE,
                        status="pass",
                        reason="clean",
                        scanned_at=NOW.isoformat(),
                    )
                    qualified = await get(
                        app,
                        "/v1/artifacts/scan?product=verireel",
                        headers={"Authorization": "Bearer local-operator-token"},
                    )
                    self.assertEqual(qualified.status_code, 200)
                    self.assertEqual(qualified.json()["scan"]["status"], "pass")
                    scanner.assert_called_once_with(store, product="verireel")
                planner.reset_mock()
                anonymous = await get(app, "/v1/artifacts/retention?product=verireel")
                self.assertEqual(anonymous.status_code, 401)
                planner.assert_not_called()

    async def test_ungranted_identity_never_reaches_package_provider(self) -> None:
        from unittest.mock import patch
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import _local_operator_artifact_protection_policy
        from tests.support.auth import _identity, _StubVerifier
        from tests.support.http import get

        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_artifact_protection_policy(),
            record_store_factory=object,
        )
        with (
            patch("control_plane.http_routes.products.product_retention_dry_run") as planner,
            patch("control_plane.http_routes.products.qualify_product_artifact") as scanner,
        ):
            for route in ("retention", "scan"):
                response = await get(
                    app,
                    f"/v1/artifacts/{route}?product=example",
                    headers={"Authorization": "Bearer valid-token"},
                )
                self.assertEqual(response.status_code, 403)
                planner.assert_not_called()
                scanner.assert_not_called()
