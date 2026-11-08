import io
import base64
import json
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from typing import Any

from httpx2 import Response

from control_plane.contracts.odoo_walkthrough import WALKTHROUGH_FORBIDDEN_GROUPS
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.odoo_client_signin import (
    CLIENT_USER_XMLID_PREFIX,
    CLIENT_SIGNIN_LIST_PROGRAM,
    CLIENT_SIGNIN_ODOO_PROGRAM,
    client_signin_command,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_client_signup import (
    ClientSigninLane,
    list_client_signin_users,
    mint_client_signin,
)
from control_plane.odoo_runtime_reads import (
    OdooRuntimeConnection,
    OdooRuntimeEvidence,
    OdooRuntimeSelection,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import (
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
    _RejectingVerifier,
)
from tests.test_product_review import _human, _profile, _operator_read_policy
from tests.support.auth import StubVerifier, identity as workflow_identity


def runtime() -> RuntimeIdentity:
    return RuntimeIdentity(
        product="example-site",
        context="example",
        instance="testing",
        artifact_id="artifact-example",
        deployment_record_id="deployment-example",
        source_git_ref="a" * 40,
        image_reference="example@sha256:" + "a" * 64,
    )


class NativeClientListConnection:
    def __init__(self) -> None:
        self.database = sqlite3.connect(":memory:")
        self.database.create_function(
            "starts_with", 2, lambda value, prefix: value.startswith(prefix)
        )
        self.database.executescript("""
CREATE TABLE res_users(id INTEGER PRIMARY KEY, partner_id INTEGER,login TEXT,password TEXT,active BOOLEAN);
CREATE TABLE res_partner(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE ir_model_data(module TEXT,name TEXT,model TEXT,res_id INTEGER);
CREATE TABLE res_users_log(create_uid INTEGER);
CREATE TABLE res_groups_users_rel(uid INTEGER,gid INTEGER);
CREATE TABLE res_groups_implied_rel(gid INTEGER,hid INTEGER);
""")
        for user_id in range(1, 11):
            self.database.execute(
                "INSERT INTO res_partner VALUES (?,?)", (user_id, f"Example {user_id}")
            )
            self.database.execute(
                "INSERT INTO res_users VALUES (?,?,?,?,?)",
                (
                    user_id,
                    user_id,
                    f"example-{user_id}@example.invalid",
                    "fixture-password" if user_id == 4 else "",
                    user_id != 6,
                ),
            )
            if user_id != 5:
                name = (
                    CLIENT_USER_XMLID_PREFIX + str(user_id)
                    if user_id != 10
                    else "launchplaneXclientX10"
                )
                self.database.execute(
                    "INSERT INTO ir_model_data VALUES (?,?,?,?)",
                    ("fixture", name, "res.users", user_id),
                )
        module, name = WALKTHROUGH_FORBIDDEN_GROUPS[0].split(".", 1)
        self.database.execute(
            "INSERT INTO ir_model_data VALUES (?,?,?,?)", (module, name, "res.groups", 20)
        )
        self.database.execute("INSERT INTO res_groups_users_rel VALUES (8,20)")
        self.database.execute("INSERT INTO res_groups_users_rel VALUES (9,21)")
        self.database.execute("INSERT INTO res_groups_implied_rel VALUES (21,20)")
        self.database.execute("INSERT INTO res_users_log VALUES (7)")
        self.database.commit()
        self.rows: list[tuple[Any, ...]] = []
        self.readonly = False

    def set_session(self, *, readonly: bool, autocommit: bool) -> None:
        self.readonly = readonly and not autocommit
        self.database.execute("PRAGMA query_only=ON")

    def cursor(self) -> "NativeClientListConnection":
        return self

    def __enter__(self) -> "NativeClientListConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> None:
        if query.startswith("SET LOCAL"):
            return
        prefix, forbidden = parameters
        statement = query.replace(
            "=ANY(%s)", " IN (" + ",".join("?" for _ in forbidden) + ")"
        ).replace("%s", "?")
        self.rows = self.database.execute(statement, (prefix, *forbidden)).fetchall()

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def rollback(self) -> None:
        self.database.rollback()

    def close(self) -> None:
        self.database.close()


class NativeClientSigninTests(TestCase):
    def test_private_transport_checks_runner_identity_and_keeps_invitation_out_of_argv(
        self,
    ) -> None:
        invitation = {
            "xmlid": "fixture." + CLIENT_USER_XMLID_PREFIX + "example",
            "name": "Example Client",
            "token": "fixture-invitation",
        }
        for changed in ("", "image", "database", "identity", "ambiguous"):
            with self.subTest(changed=changed):
                calls: list[list[str]] = []
                identity = runtime().model_dump(mode="json")
                observed = dict(identity)
                if changed == "identity":
                    observed["artifact_id"] = "other-artifact"
                environment = [
                    "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(observed),
                    "PLATFORM_INSTANCE=testing",
                    "ODOO_DB_HOST=database",
                    "ODOO_DB_NAME=" + ("other" if changed == "database" else "fixture"),
                ]

                def docker(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
                    calls.append(arguments)
                    if arguments[1] == "ps":
                        value = "one two" if changed == "ambiguous" else "one"
                    elif arguments[1] == "inspect":
                        value = json.dumps(
                            [
                                {
                                    "Config": {
                                        "Image": "other"
                                        if changed == "image"
                                        else identity["image_reference"],
                                        "Env": environment,
                                    }
                                }
                            ]
                        )
                    else:
                        value = json.dumps(invitation)
                    return SimpleNamespace(returncode=0, stdout=value.encode())

                def private_io(**kwargs: Any) -> None:
                    output = io.StringIO()
                    with (
                        patch(
                            "sys.argv",
                            [
                                "python",
                                base64.b64encode(json.dumps(kwargs["payload"]).encode()).decode(),
                                "FRAME:",
                            ],
                        ),
                        patch.object(subprocess, "run", side_effect=docker),
                        redirect_stdout(output),
                    ):
                        exec(kwargs["program"], {})
                    kwargs["receive"](json.loads(output.getvalue().removeprefix("FRAME:")))

                with patch(
                    "control_plane.dokploy.odoo_client_signin._read_remote", side_effect=private_io
                ):
                    arguments = {
                        "identity": identity,
                        "identity_key": "LAUNCHPLANE_RUNTIME_IDENTITY_JSON",
                        "image": identity["image_reference"],
                        "database": "fixture",
                        "project": "project",
                        "operation": "mint",
                        "xmlid": invitation["xmlid"],
                    }
                    if changed:
                        with self.assertRaises(ValueError):
                            client_signin_command(
                                host="fixture",
                                token="fixture",
                                server_id="server",
                                payload=arguments,
                            )
                        self.assertFalse(any(call[1] == "exec" for call in calls))
                    else:
                        self.assertEqual(
                            client_signin_command(
                                host="fixture",
                                token="fixture",
                                server_id="server",
                                payload=arguments,
                            ),
                            invitation,
                        )
                self.assertNotIn(invitation["token"], repr(calls))

    def test_native_read_lists_only_declared_active_passwordless_non_admin_users(self) -> None:
        connection = NativeClientListConnection()
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"psycopg2": SimpleNamespace(connect=lambda **_: connection)}),
            patch.dict(
                "os.environ",
                {
                    "ODOO_DB_HOST": "fixture",
                    "ODOO_DB_USER": "fixture",
                    "ODOO_DB_PASSWORD": "fixture",
                },
            ),
            patch(
                "sys.stdin",
                io.StringIO(
                    json.dumps(
                        {
                            "database": "fixture",
                            "xmlid_prefix": CLIENT_USER_XMLID_PREFIX,
                            "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
                        }
                    )
                ),
            ),
            redirect_stdout(output),
        ):
            exec(CLIENT_SIGNIN_LIST_PROGRAM, {})
        self.assertTrue(connection.readonly)
        self.assertEqual(
            [user["xmlid"] for user in json.loads(output.getvalue())["users"]],
            ["fixture." + CLIENT_USER_XMLID_PREFIX + "3"],
        )
        self.assertNotIn("fixture-password", output.getvalue())

    def test_native_mint_refuses_password_set_unrelated_inactive_used_and_admin_accounts(
        self,
    ) -> None:
        for changes in (
            {"password": True},
            {"id": 2},
            {"active": False},
            {"login_date": "used"},
            {"admin": True},
            {"shared_partner": True},
            {"xmlid": "fixture.unrelated"},
        ):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                env, user, partner = self.native_environment(
                    **{key: value for key, value in changes.items() if key != "xmlid"}
                )
                request = {
                    "xmlid": changes.get(
                        "xmlid", "fixture." + CLIENT_USER_XMLID_PREFIX + "example"
                    ),
                    "xmlid_prefix": CLIENT_USER_XMLID_PREFIX,
                    "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
                    "result_file": str(Path(directory) / "result.json"),
                }
                with self.assertRaises(RuntimeError), patch("logging.disable"):
                    exec(CLIENT_SIGNIN_ODOO_PROGRAM, {"env": env, "request": request})
                partner._generate_signup_token.assert_not_called()
                env.cr.commit.assert_not_called()
                user.write.assert_not_called()

    def test_native_mint_uses_existing_partner_and_native_configured_validity_without_mail(
        self,
    ) -> None:
        with TemporaryDirectory() as directory, patch("logging.disable"):
            env, user, partner = self.native_environment()
            request = {
                "xmlid": "fixture." + CLIENT_USER_XMLID_PREFIX + "example",
                "xmlid_prefix": CLIENT_USER_XMLID_PREFIX,
                "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
                "result_file": str(Path(directory) / "result.json"),
            }
            exec(CLIENT_SIGNIN_ODOO_PROGRAM, {"env": env, "request": request})
            result = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(result["token"], partner._generate_signup_token.return_value)
            partner._generate_signup_token.assert_called_once_with()
            env.cr.commit.assert_called_once()
            user.write.assert_not_called()
            user.action_reset_password.assert_not_called()

    @staticmethod
    def native_environment(**changes: Any) -> tuple[Mock, Mock, Mock]:
        env, user, partner = Mock(), Mock(), Mock()
        user._name = "res.users"
        user.id = changes.get("id", 7)
        user.active = changes.get("active", True)
        user.login_date = changes.get("login_date", False)
        user.name = "Example Client"
        user.has_group.return_value = changes.get("admin", False)
        user.partner_id = partner
        partner.sudo.return_value = partner
        partner.user_ids = [user, Mock()] if changes.get("shared_partner") else [user]
        partner._generate_signup_token.return_value = "fixture-invitation"
        env.ref.side_effect = lambda xmlid, **_: user if xmlid.startswith("fixture.") else Mock()
        env.cr.fetchone.return_value = (changes.get("password", False),)
        return env, user, partner


class ClientSigninServiceTests(TestCase):
    def setUp(self) -> None:
        self.store = Mock()
        self.profile = _profile().model_copy(update={"driver_id": "odoo"})
        self.store.read_product_profile_record.return_value = self.profile
        self.store.list_product_profile_records.return_value = (self.profile,)
        self.store.list_preview_records.return_value = ()
        self.identity = _human(login="renamed-client", github_id=9001)
        self.selection = OdooRuntimeSelection(runtime(), "https://testing.example.invalid")
        self.connection = OdooRuntimeConnection(
            selection=self.selection,
            evidence=OdooRuntimeEvidence(
                identity=runtime(),
                target_id="target",
                target_name="project",
                container_id="container",
                observed_at="2026-11-01T00:00:00Z",
            ),
            host="fixture",
            token="fixture",
            server_id="server",
            app_name="project",
            credentials={"ODOO_DB_NAME": "fixture"},
        )
        self.lane = ClientSigninLane(
            self.profile.repository, None, runtime().artifact_id, "testing"
        )
        self.native = self.enterContext(
            patch("control_plane.odoo_client_signup.client_signin_command")
        )
        self.enterContext(
            patch(
                "control_plane.odoo_client_signup.select_stable_runtime",
                return_value=self.selection,
            )
        )
        self.enterContext(
            patch("control_plane.odoo_client_signup.connect_runtime", return_value=self.connection)
        )
        self.current = self.enterContext(
            patch("control_plane.odoo_client_signup.assert_selection_current")
        )

    def users(self) -> dict[str, object]:
        return list_client_signin_users(
            store=self.store,
            root=Path("."),
            profile=self.profile,
            identity=self.identity,
            lane=self.lane,
        )

    def mint(self, binding: str) -> dict[str, str]:
        return mint_client_signin(
            store=self.store,
            root=Path("."),
            profile=self.profile,
            identity=self.identity,
            lane=self.lane,
            reviewed_runtime_binding=binding,
            xmlid="fixture." + CLIENT_USER_XMLID_PREFIX + "example",
        )

    def test_list_has_no_invitation_and_click_mints_for_the_exact_current_lane_without_store_writes(
        self,
    ) -> None:
        self.native.return_value = {
            "users": [
                {
                    "xmlid": "fixture." + CLIENT_USER_XMLID_PREFIX + "example",
                    "name": "Example Client",
                    "login": "client@example.invalid",
                }
            ]
        }
        listed = self.users()
        self.assertNotIn("token", json.dumps(listed))
        self.native.return_value = {
            "xmlid": "fixture." + CLIENT_USER_XMLID_PREFIX + "example",
            "name": "Example Client",
            "token": "fixture-invitation",
        }
        result = self.mint(str(listed["runtime_binding"]))
        url = urlsplit(result["sign_in_url"])
        self.assertEqual(url.netloc, urlsplit(self.selection.base_url).netloc)
        self.assertEqual(parse_qs(url.query)["token"], [self.native.return_value["token"]])
        self.assertFalse(any(call[0].startswith("write_") for call in self.store.mock_calls))

    def test_non_client_including_admin_is_rejected_before_native_or_store_reads(self) -> None:
        self.identity = replace(_human(login="site-owner", github_id=111), role="admin")
        with self.assertRaises(PermissionError):
            self.users()
        self.native.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])

    def test_changed_runtime_and_wrong_binding_never_disclose_a_credential(self) -> None:
        with self.assertRaises(ValueError):
            self.mint("wrong-runtime")
        self.native.assert_not_called()
        self.native.return_value = {"users": []}
        binding = str(self.users()["runtime_binding"])
        self.native.return_value = {
            "xmlid": "fixture." + CLIENT_USER_XMLID_PREFIX + "example",
            "name": "Example Client",
            "token": "fixture-invitation",
        }
        self.current.side_effect = (None, ValueError("Runtime changed"))
        with self.assertRaises(ValueError):
            self.mint(binding)

    def test_foreign_lane_with_equivalent_hostname_is_refused_before_native_read(self) -> None:
        foreign_lane = self.profile.lanes[0].model_copy(
            update={"base_url": "https://TESTING.example.invalid.:443/"}
        )
        self.store.list_product_profile_records.return_value = (
            self.profile.model_copy(
                update={"product": "foreign-product", "lanes": (foreign_lane,)}
            ),
        )
        with self.assertRaises(ValueError):
            self.users()
        self.native.assert_not_called()

    def test_changed_client_during_mint_prevents_disclosure(self) -> None:
        self.native.return_value = {"users": []}
        binding = str(self.users()["runtime_binding"])

        def mint_result(**_kwargs: object) -> dict[str, str]:
            self.store.read_product_profile_record.return_value = self.profile.model_copy(
                update={"owner": self.profile.owner.model_copy(update={"github_id": "999"})}
            )
            return {
                "xmlid": "fixture." + CLIENT_USER_XMLID_PREFIX + "example",
                "name": "Example Client",
                "token": "fixture-invitation",
            }

        self.native.side_effect = mint_result
        with self.assertRaises(PermissionError):
            self.mint(binding)


class ClientSigninHttpTests(IsolatedAsyncioTestCase):
    async def test_machine_identity_cannot_list_or_mint_native_client_invitations(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(workflow_identity()),
            authz_policy=_operator_read_policy(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
        )
        with (
            patch("control_plane.odoo_client_signup.list_client_signin_users") as listed,
            patch("control_plane.odoo_client_signup.mint_client_signin") as minted,
        ):
            for method in ("GET", "POST"):
                response = await _asgi_request(
                    app,
                    method,
                    "/v1/product-review/sign-in"
                    + ("?" + urlencode(self.lookup) if method == "GET" else ""),
                    headers={"Authorization": "Bearer valid-token"},
                    payload=None if method == "GET" else self.payload,
                )
                self.assertEqual(response.status_code, 403)
            listed.assert_not_called()
            minted.assert_not_called()

    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/records.db")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_product_profile_record(_profile())
        self.sessions = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=_operator_read_policy(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.sessions,
            control_plane_root_path=Path(directory),
        )
        self.lookup = {
            "repository": "every/example-site",
            "pull_request": 42,
            "generation_id": "generation",
            "instance": "preview",
        }
        self.payload = {
            **self.lookup,
            "xmlid": "fixture." + CLIENT_USER_XMLID_PREFIX + "example",
            "reviewed_runtime_binding": "a" * 64,
        }

    async def request(
        self, github_id: int, *, method: str = "GET", login: str = "site-owner", csrf: bool = True
    ) -> Response:
        session = self.sessions.issue(_human(login=login, github_id=github_id))
        headers = _browser_mutation_headers(self.sessions, session)
        if not csrf:
            headers.pop("X-CSRF-Token", None)
        return await _asgi_request(
            self.app,
            method,
            "/v1/product-review/sign-in"
            + ("?" + urlencode(self.lookup) if method == "GET" else ""),
            headers=headers,
            payload=None if method == "GET" else self.payload,
        )

    async def test_only_recorded_client_can_list_and_mint_and_responses_are_uncacheable(
        self,
    ) -> None:
        with (
            patch(
                "control_plane.odoo_client_signup.list_client_signin_users",
                return_value={"users": [], "runtime_binding": "a" * 64},
            ),
            patch(
                "control_plane.odoo_client_signup.mint_client_signin",
                return_value={
                    "name": "Example Client",
                    "sign_in_url": "https://preview.example/web/signup?token=fictional",
                },
            ) as mint,
        ):
            listed = await self.request(9001, login="renamed-client")
            self.assertEqual(listed.status_code, 200)
            mint.assert_not_called()
            minted = await self.request(9001, method="POST")
            self.assertEqual(minted.status_code, 200)
            self.assertIn("no-store", minted.headers["cache-control"])
            self.assertEqual(minted.headers["referrer-policy"], "no-referrer")

    async def test_non_clients_anonymous_and_missing_csrf_never_reach_native_calls(self) -> None:
        with (
            patch("control_plane.odoo_client_signup.list_client_signin_users") as listed,
            patch("control_plane.odoo_client_signup.mint_client_signin") as mint,
        ):
            for method in ("GET", "POST"):
                for github_id, login in ((111, "site-owner"), (123, "example-operator")):
                    response = await self.request(github_id, method=method, login=login)
                    self.assertEqual(response.status_code, 403)
                anonymous = await _asgi_request(
                    self.app,
                    method,
                    "/v1/product-review/sign-in"
                    + ("?" + urlencode(self.lookup) if method == "GET" else ""),
                    payload=None if method == "GET" else self.payload,
                )
                self.assertEqual(anonymous.status_code, 401)
            self.assertEqual((await self.request(9001, method="POST", csrf=False)).status_code, 403)
            self.lookup["repository"] = "foreign/product"
            self.assertEqual((await self.request(9001)).status_code, 403)
            listed.assert_not_called()
            mint.assert_not_called()

    async def test_private_native_error_does_not_escape_the_response(self) -> None:
        with patch(
            "control_plane.odoo_client_signup.mint_client_signin",
            side_effect=ValueError("private-native-value"),
        ):
            response = await self.request(9001, method="POST")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("private-native-value", response.text)
