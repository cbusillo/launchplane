from typing import Any

RESTORED_LOGINS_PARAMETER = "odoo_devkit.preserve_restored_logins"


def restored_logins_preserved(cursor: Any) -> bool:
    cursor.execute("SELECT value FROM ir_config_parameter WHERE key = %s", (RESTORED_LOGINS_PARAMETER,))
    return cursor.fetchone() == ("true",)


def validate_admin_password(value: str) -> None:
    if "\x00" in value or (value and value.splitlines() != [value]):
        raise ValueError(
            "ODOO_ADMIN_PASSWORD must not contain line separators or NUL bytes. "
            "Correct the supplied environment value to the intended single-line password; spaces and tabs are literal."
        )


def set_admin_password_if_changed(admin_user: Any, password: str) -> bool:
    from odoo.exceptions import AccessDenied

    try:
        admin_user.with_user(admin_user)._check_credentials({"type": "password", "password": password}, {"interactive": True})
    except AccessDenied:
        admin_user.with_context(no_reset_password=True).sudo().write({"password": password})
        return True
    return False


def assert_default_admin_password_inactive(env: Any, configured_login: str) -> None:
    from odoo.exceptions import AccessDenied

    users = env["res.users"].sudo()
    for login in dict.fromkeys(("admin", configured_login)):
        if not users.with_context(active_test=False).search([("login", "=", login)], limit=1):
            continue
        try:
            authenticated = bool(
                users.authenticate({"type": "password", "login": login, "password": "admin"}, {"interactive": False})
            )
        except AccessDenied:
            authenticated = False
        if authenticated:
            raise ValueError(f"Insecure configuration: active password for {login} is 'admin'.")
