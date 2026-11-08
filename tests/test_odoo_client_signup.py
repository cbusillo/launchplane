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
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from typing import Any

from httpx2 import Response

from control_plane.contracts.odoo_walkthrough import WALKTHROUGH_FORBIDDEN_GROUPS
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.odoo_client_signin import (
    CLIENT_USER_MANIFEST_KEY,
    CLIENT_USER_DECLARATION_PROGRAM,
    CLIENT_SIGNIN_USERS_PROGRAM,
    CLIENT_SIGNIN_ODOO_PROGRAM,
    client_signin_command,
)
from control_plane.dokploy.odoo_shell import ODOO_SHELL_WRAPPER
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
import tests.test_preview_fixture as preview_support


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
        self.database.executescript("""
CREATE TABLE ir_module_module(name TEXT,state TEXT);
INSERT INTO ir_module_module VALUES ('fixture','installed');
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
                name = "user_" + str(user_id) if user_id != 10 else "launchplane_client_unrelated"
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
        flattened: list[object] = []
        statement = query
        for value in parameters:
            if isinstance(value, (tuple, list)):
                statement = statement.replace(
                    "=ANY(%s)", " IN (" + ",".join("?" for _ in value) + ")", 1
                )
                flattened.extend(value)
            else:
                statement = statement.replace("%s", "?", 1)
                flattened.append(value)
        self.rows = self.database.execute(statement, flattened).fetchall()

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def rollback(self) -> None:
        self.database.rollback()

    def close(self) -> None:
        self.database.close()


class NativeClientSigninTests(TestCase):
    def setUp(self) -> None:
        self.manifests: dict[str, object] = {
            "fixture": {CLIENT_USER_MANIFEST_KEY: ["fixture.user_client"]},
            "uninstalled": {CLIENT_USER_MANIFEST_KEY: ["uninstalled.user_client"]},
        }
        self.enterContext(patch("logging.disable"))
        addons = Path(self.enterContext(TemporaryDirectory()))
        (addons / "fixture").mkdir()
        (addons / "fixture" / "__manifest__.py").write_text("{}")
        self.enterContext(
            patch.dict(
                sys.modules,
                {
                    "odoo": SimpleNamespace(addons=SimpleNamespace(__path__=[str(addons)])),
                    "odoo.addons": SimpleNamespace(__path__=[str(addons)]),
                    "odoo.modules.module": SimpleNamespace(
                        Manifest=SimpleNamespace(
                            for_addon=lambda module, **_: self.manifests.get(module)
                        ),
                    ),
                },
            )
        )

    def test_native_manifest_declaration_is_exact_optional_and_owned_by_installed_addon(
        self,
    ) -> None:
        namespace: dict[str, Any] = {}
        exec(CLIENT_USER_DECLARATION_PROGRAM, namespace)
        read = namespace["declared_client_user_xmlids"]
        self.assertEqual(read(["fixture"]), ["fixture.user_client"])
        optional_metadata: tuple[dict[str, object], ...] = ({}, {CLIENT_USER_MANIFEST_KEY: []})
        for metadata in optional_metadata:
            self.manifests["fixture"] = metadata
            self.assertEqual(read(["fixture"]), [])
        for values in (
            None,
            "fixture.user_client",
            ("fixture.user_client",),
            [None],
            ["fixture.*"],
            ["foreign.user_client"],
            ["fixture.user_client"] * 2,
        ):
            self.manifests["fixture"] = {CLIENT_USER_MANIFEST_KEY: values}
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                read(["fixture"])
        self.manifests.pop("fixture")
        with self.assertRaises(RuntimeError):
            read(["fixture"])

    def test_private_shell_executes_native_program_and_returns_only_its_private_result(
        self,
    ) -> None:
        env, user, partner = self.native_environment()
        calls: list[list[str]] = []

        def shell(command: list[str], **kwargs: Any) -> SimpleNamespace:
            calls.append(command)
            exec(kwargs["input"].decode(), {"env": env})
            return SimpleNamespace(returncode=0)

        startup = SimpleNamespace(
            _load_settings=lambda _: object(),
            _write_runtime_config=lambda _: None,
            _build_odoo_shell_command=lambda _: ["odoo", "shell"],
            _odoo_environment=lambda _: {},
        )
        request = {
            "program": CLIENT_SIGNIN_ODOO_PROGRAM,
            "failure_program": "env.cr.rollback()",
            "fallback_result": {"status": "unverified"},
            "database": "fixture",
            "xmlid": "fixture.user_client",
            "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
        }
        output = io.StringIO()
        with (
            patch.dict(sys.modules, {"run_odoo_startup": startup}),
            patch.object(subprocess, "run", side_effect=shell),
            patch("sys.stdin", io.StringIO(json.dumps(request))),
            redirect_stdout(output),
        ):
            exec(ODOO_SHELL_WRAPPER, {})
        result = json.loads(output.getvalue())
        self.assertEqual(result["token"], "fixture-invitation")
        self.assertEqual(len(calls), 1)
        user.write.assert_not_called()
        partner.signup_prepare.assert_called_once_with(signup_type="signup")

    def test_private_transport_refuses_changed_or_ambiguous_runner(
        self,
    ) -> None:
        invitation = {
            "xmlid": "fixture.user_client",
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

    def test_native_read_lists_only_declared_active_passwordless_non_admin_users(self) -> None:
        connection = NativeClientListConnection()
        self.manifests["fixture"] = {
            CLIENT_USER_MANIFEST_KEY: [f"fixture.user_{i}" for i in range(1, 10) if i != 5]
        }
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
                            "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
                        }
                    )
                ),
            ),
            redirect_stdout(output),
        ):
            exec(CLIENT_SIGNIN_USERS_PROGRAM, {})
        self.assertTrue(connection.readonly)
        self.assertEqual(
            [user["xmlid"] for user in json.loads(output.getvalue())["users"]],
            ["fixture.user_3"],
        )
        self.assertNotIn("fixture-password", output.getvalue())

    def test_native_list_opt_out_and_unresolved_declaration_are_not_guessed(self) -> None:
        for values in (None, [], ["fixture.missing"], ["fixture.user_3"]):
            connection = NativeClientListConnection()
            self.manifests["fixture"] = {} if values is None else {CLIENT_USER_MANIFEST_KEY: values}
            if values == ["fixture.user_3"]:
                connection.database.execute(
                    "UPDATE ir_model_data SET model='res.partner' WHERE name='user_3'"
                )
                connection.database.commit()
            output = io.StringIO()
            with (
                self.subTest(values=values),
                patch.dict(
                    sys.modules, {"psycopg2": SimpleNamespace(connect=lambda **_: connection)}
                ),
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
                                "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
                            }
                        )
                    ),
                ),
                redirect_stdout(output),
            ):
                if values:
                    with self.assertRaises(RuntimeError):
                        exec(CLIENT_SIGNIN_USERS_PROGRAM, {})
                else:
                    exec(CLIENT_SIGNIN_USERS_PROGRAM, {})
                    self.assertEqual(json.loads(output.getvalue()), {"users": []})
            self.assertTrue(connection.readonly)

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
            {"name": False},
            {"xmlid": "fixture.unrelated"},
            {"undeclared": True},
            {"unresolved": True},
        ):
            with self.subTest(changes=changes), TemporaryDirectory() as directory:
                env, user, partner = self.native_environment(
                    **{key: value for key, value in changes.items() if key != "xmlid"}
                )
                request = {
                    "xmlid": changes.get("xmlid", "fixture.user_client"),
                    "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
                    "result_file": str(Path(directory) / "result.json"),
                }
                if changes.get("undeclared"):
                    self.manifests["fixture"] = {}
                if changes.get("unresolved"):
                    env.ref.return_value = False
                    env.ref.side_effect = None
                with self.assertRaises(RuntimeError), patch("logging.disable"):
                    exec(CLIENT_SIGNIN_ODOO_PROGRAM, {"env": env, "request": request})
                partner._generate_signup_token.assert_not_called()
                env.cr.commit.assert_not_called()
                user.write.assert_not_called()
                self.manifests["fixture"] = {CLIENT_USER_MANIFEST_KEY: ["fixture.user_client"]}

    def test_native_mint_uses_existing_partner_and_native_configured_validity_without_mail(
        self,
    ) -> None:
        with TemporaryDirectory() as directory, patch("logging.disable"):
            env, user, partner = self.native_environment()
            request = {
                "xmlid": "fixture.user_client",
                "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
                "result_file": str(Path(directory) / "result.json"),
            }
            exec(CLIENT_SIGNIN_ODOO_PROGRAM, {"env": env, "request": request})
            result = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(result["token"], "fixture-invitation")
            partner._generate_signup_token.assert_called_once_with()
            env.cr.commit.assert_called_once()
            user.write.assert_not_called()
            user.action_reset_password.assert_not_called()

    @staticmethod
    def native_environment(**changes: Any) -> tuple[Mock, Mock, Mock]:
        env, user, partner = MagicMock(), Mock(), Mock()
        env.__getitem__.return_value.sudo.return_value.search.return_value.mapped.return_value = [
            "fixture"
        ]
        user._name = "res.users"
        user.id = changes.get("id", 7)
        user.active = changes.get("active", True)
        user.login_date = changes.get("login_date", False)
        user.name = changes.get("name", "Example Client")
        user.with_context.return_value = user
        user.has_group.return_value = changes.get("admin", False)
        user.partner_id = partner
        partner.sudo.return_value = partner
        partner.user_ids = [user]
        all_partner = Mock()
        partner.with_context.return_value = all_partner
        all_partner.user_ids = [user, Mock()] if changes.get("shared_partner") else [user]
        all_partner.signup_prepare = partner.signup_prepare
        all_partner._generate_signup_token = partner._generate_signup_token
        partner.signup_type = False
        partner.signup_prepare.side_effect = lambda **values: setattr(
            partner, "signup_type", values["signup_type"]
        )

        def native_token() -> str:
            if partner.signup_type != "signup":
                raise RuntimeError("Native signup type is unprepared")
            return "fixture-invitation"

        partner._generate_signup_token.side_effect = native_token
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
        self.store.list_dokploy_target_id_records.return_value = ()
        self.store.list_physical_provider_target_records.return_value = ()
        self.enterContext(
            patch("control_plane.odoo_client_signup.published_testing_copy", return_value=None)
        )
        self.domain = self.enterContext(
            patch("control_plane.odoo_client_signup.odoo_compose_has_domain", return_value=True)
        )
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
            xmlid="fixture.user_client",
        )

    def test_list_has_no_invitation_and_click_mints_for_the_exact_current_lane_without_store_writes(
        self,
    ) -> None:
        self.native.return_value = {
            "users": [
                {
                    "xmlid": "fixture.user_client",
                    "name": "Example Client",
                    "login": "client@example.invalid",
                }
            ]
        }
        listed = self.users()
        self.assertNotIn("token", json.dumps(listed))
        self.native.return_value = {
            "xmlid": "fixture.user_client",
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

    def test_retired_profile_and_testing_target_alias_or_wrong_domain_refuse_before_native(
        self,
    ) -> None:
        for failure in ("retired", "alias", "domain"):
            with self.subTest(failure=failure):
                if failure == "retired":
                    self.profile = self.profile.model_copy(update={"lifecycle_state": "retired"})
                elif failure == "alias":
                    self.store.list_dokploy_target_id_records.return_value = (
                        DokployTargetIdRecord(
                            context="foreign",
                            instance="prod",
                            target_id="target",
                            updated_at=self.profile.updated_at,
                        ),
                    )
                else:
                    self.domain.return_value = False
                with self.assertRaises(ValueError):
                    self.users()
                self.native.assert_not_called()
                self.profile = self.profile.model_copy(update={"lifecycle_state": "active"})
                self.store.list_dokploy_target_id_records.return_value = ()

    def test_changed_runtime_and_wrong_binding_never_disclose_a_credential(self) -> None:
        with self.assertRaises(ValueError):
            self.mint("wrong-runtime")
        self.native.assert_not_called()
        self.native.return_value = {"users": []}
        binding = str(self.users()["runtime_binding"])
        self.native.return_value = {
            "xmlid": "fixture.user_client",
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
                "xmlid": "fixture.user_client",
                "name": "Example Client",
                "token": "fixture-invitation",
            }

        self.native.side_effect = mint_result
        with self.assertRaises(PermissionError):
            self.mint(binding)


class ClientSigninPreviewTests(TestCase):
    def test_actual_preview_selection_guards_current_generation_owned_target_and_protected_use(
        self,
    ) -> None:
        for failure in ("", "generation", "staff", "target"):
            with (
                self.subTest(failure=failure),
                preview_support.PreviewFixtureLaneTests().preview() as (store, adapter, slot),
            ):
                profile = store.read_product_profile_record(adapter.product).model_copy(
                    update={"owner": _profile().owner}
                )
                store.write_product_profile_record(profile)
                selection = adapter.selection()
                connection = OdooRuntimeConnection(
                    selection=selection,
                    evidence=OdooRuntimeEvidence(
                        identity=selection.identity,
                        target_id=preview_support.fixture_binding().target_id,
                        target_name=selection.identity.instance,
                        container_id="fixture",
                        observed_at=profile.updated_at,
                    ),
                    host="fixture",
                    token="fixture",
                    server_id="fixture",
                    app_name="fixture",
                    credentials={"ODOO_DB_NAME": "fixture"},
                )
                lane = ClientSigninLane(
                    profile.repository, 42, adapter.request.generation_id, "preview"
                )
                identity = _human(login="renamed-client", github_id=9001)
                with (
                    patch(
                        "control_plane.preview_fixture.reads.connect_runtime",
                        return_value=connection,
                    ),
                    patch(
                        "control_plane.odoo_client_signup.client_signin_command",
                        return_value={"users": []},
                    ) as native,
                ):
                    listed = list_client_signin_users(
                        store=store,
                        root=adapter.root,
                        profile=profile,
                        identity=identity,
                        lane=lane,
                    )
                    native.reset_mock()
                    native.return_value = {
                        "xmlid": "fixture.user_client",
                        "name": "Example Client",
                        "token": "fixture-invitation",
                    }
                    if failure == "generation":
                        lane = replace(lane, generation_id="old-generation")
                    elif failure == "staff":
                        target = DokployTargetRecord(
                            context=selection.identity.context,
                            instance=slot,
                            updated_at=profile.updated_at,
                        )
                        target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
                            reason="staff"
                        )
                        store.write_dokploy_target_record(target)
                    elif failure == "target":
                        store.write_dokploy_target_id_record(
                            DokployTargetIdRecord(
                                context="foreign",
                                instance="prod",
                                target_id=connection.evidence.target_id,
                                updated_at=profile.updated_at,
                            )
                        )

                    def mint() -> dict[str, str]:
                        return mint_client_signin(
                            store=store,
                            root=adapter.root,
                            profile=profile,
                            identity=identity,
                            lane=lane,
                            xmlid="fixture.user_client",
                            reviewed_runtime_binding=str(listed["runtime_binding"]),
                        )

                    if failure:
                        with self.assertRaises(ValueError):
                            list_client_signin_users(
                                store=store,
                                root=adapter.root,
                                profile=profile,
                                identity=identity,
                                lane=lane,
                            )
                        with self.assertRaises(ValueError):
                            mint()
                        native.assert_not_called()
                    else:
                        self.assertEqual(
                            urlsplit(mint()["sign_in_url"]).netloc,
                            urlsplit(selection.base_url).netloc,
                        )


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
            "xmlid": "fixture.user_client",
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

    async def test_retired_profile_does_not_hide_the_active_recorded_clients_profile(self) -> None:
        retired = _profile().model_copy(
            update={
                "product": "a-retired-product",
                "lifecycle_state": "retired",
                "preview": _profile().preview.model_copy(update={"enabled": False}),
            }
        )
        self.store.write_product_profile_record(retired)
        with patch(
            "control_plane.odoo_client_signup.list_client_signin_users",
            return_value={"users": [], "runtime_binding": "a" * 64},
        ) as read:
            self.assertEqual((await self.request(9001)).status_code, 200)
            self.assertEqual(read.call_args.kwargs["profile"].product, _profile().product)
