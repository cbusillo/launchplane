import unittest

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneProfile,
)
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.ingress_route_scope import (
    IngressRouteInstanceScopeError,
    validate_ingress_route_instance_scope,
)


def _profile(*, shared_prod_domain: bool = False) -> LaunchplaneProductProfileRecord:
    testing_domain = "app-testing.example.test"
    return LaunchplaneProductProfileRecord(
        product="example-product",
        display_name="Example Product",
        repository="every/example-product",
        driver_id="odoo",
        image=ProductImageProfile(repository="ghcr.io/every/example-product"),
        runtime_port=8069,
        health_path="/launchplane/health",
        lanes=(
            ProductLaneProfile(
                context="example",
                instance="testing",
                base_url=f"https://{testing_domain}",
                health_url=f"https://health-{testing_domain}/launchplane/health",
                health_monitoring=ProductLaneHealthMonitoringPolicy(
                    monitoring_intent="public",
                    checks=(
                        ProductLaneHealthCheck(
                            name="public-ingress",
                            url=f"https://{testing_domain}/launchplane/health",
                        ),
                    ),
                ),
            ),
            ProductLaneProfile(
                context="example",
                instance="prod",
                base_url=(
                    f"https://{testing_domain}"
                    if shared_prod_domain
                    else "https://app.example.test"
                ),
            ),
        ),
        updated_at="2026-07-22T00:00:00Z",
        source="test",
    )


class IngressRouteInstanceScopeTests(unittest.TestCase):
    def test_private_testing_scope_requires_its_active_endpoint_and_access_list(self) -> None:
        profile = _profile()
        profile = profile.model_copy(
            update={
                "lanes": (
                    ProductLaneProfile(context="example", instance="testing"),
                    profile.lanes[1],
                )
            }
        )
        endpoint = PrivateHealthEndpointRecord(
            endpoint_key="testing-bridge",
            product=profile.product,
            context="example",
            instance="testing",
            url="https://private-testing.example.test/health",
            updated_at=profile.updated_at,
        )

        def validate(
            *, private_endpoints: tuple[PrivateHealthEndpointRecord, ...], access_list_id: int = 0
        ) -> None:
            validate_ingress_route_instance_scope(
                profile=profile,
                context="example",
                instance="testing",
                requested_domains=("private-testing.example.test",),
                private_endpoints=private_endpoints,
                access_list_id=access_list_id,
            )

        validate(private_endpoints=(endpoint,), access_list_id=1)
        for update in ({"status": "disabled"}, {"product": "other"}, {"instance": "prod"}):
            with self.subTest(update=update), self.assertRaises(IngressRouteInstanceScopeError):
                validate(
                    private_endpoints=(endpoint.model_copy(update=update),),
                    access_list_id=1,
                )
        with self.assertRaises(IngressRouteInstanceScopeError):
            validate(private_endpoints=(endpoint,))
        with self.assertRaises(IngressRouteInstanceScopeError):
            validate(
                private_endpoints=(endpoint, endpoint.model_copy(update={"product": "other"})),
                access_list_id=1,
            )

    def test_accepts_domains_owned_by_exact_lane(self) -> None:
        validate_ingress_route_instance_scope(
            profile=_profile(),
            context="example",
            instance="testing",
            requested_domains=("app-testing.example.test",),
        )

    def test_private_endpoint_on_another_port_does_not_break_public_scope(self) -> None:
        profile = _profile()
        endpoint = PrivateHealthEndpointRecord(
            endpoint_key="testing-alternate-port",
            product=profile.product,
            context="example",
            instance="testing",
            url="https://private-testing.example.test:8443/health",
            updated_at=profile.updated_at,
        )
        validate_ingress_route_instance_scope(
            profile=profile,
            context="example",
            instance="testing",
            requested_domains=("app-testing.example.test",),
            private_endpoints=(endpoint,),
        )
        with self.assertRaises(IngressRouteInstanceScopeError):
            validate_ingress_route_instance_scope(
                profile=profile,
                context="example",
                instance="testing",
                requested_domains=("private-testing.example.test",),
                private_endpoints=(endpoint,),
                access_list_id=1,
            )

    def test_rejects_missing_lane(self) -> None:
        with self.assertRaisesRegex(
            IngressRouteInstanceScopeError,
            "does not identify exactly one product lane",
        ) as raised:
            validate_ingress_route_instance_scope(
                profile=_profile(),
                context="example",
                instance="missing",
                requested_domains=("app-testing.example.test",),
            )

        self.assertEqual(raised.exception.code, "invalid_ingress_instance_scope")

    def test_rejects_domain_outside_lane(self) -> None:
        with self.assertRaises(IngressRouteInstanceScopeError) as raised:
            validate_ingress_route_instance_scope(
                profile=_profile(),
                context="example",
                instance="testing",
                requested_domains=("app.example.test",),
            )

        self.assertEqual(raised.exception.code, "ingress_route_domain_scope_mismatch")

    def test_rejects_domain_shared_with_another_lane(self) -> None:
        with self.assertRaises(IngressRouteInstanceScopeError) as raised:
            validate_ingress_route_instance_scope(
                profile=_profile(shared_prod_domain=True),
                context="example",
                instance="testing",
                requested_domains=("app-testing.example.test",),
            )

        self.assertEqual(raised.exception.code, "ingress_route_domain_scope_ambiguous")

    def test_ignores_sibling_lane_without_public_domains(self) -> None:
        profile = _profile()
        profile_with_empty_sibling = profile.model_copy(
            update={
                "lanes": (
                    profile.lanes[0],
                    profile.lanes[1].model_copy(update={"base_url": ""}),
                )
            }
        )

        validate_ingress_route_instance_scope(
            profile=profile_with_empty_sibling,
            context="example",
            instance="testing",
            requested_domains=("app-testing.example.test",),
        )

    def test_rejects_non_https_lane_url(self) -> None:
        profile = _profile()
        invalid_profile = profile.model_copy(
            update={
                "lanes": (
                    profile.lanes[0].model_copy(
                        update={"base_url": "http://app-testing.example.test"}
                    ),
                    profile.lanes[1],
                )
            }
        )

        with self.assertRaises(IngressRouteInstanceScopeError) as raised:
            validate_ingress_route_instance_scope(
                profile=invalid_profile,
                context="example",
                instance="testing",
                requested_domains=("app-testing.example.test",),
            )

        self.assertEqual(raised.exception.code, "ingress_route_lane_url_invalid")
