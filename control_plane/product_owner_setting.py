from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal
from urllib.error import HTTPError

import click
from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOwnerProfile,
)


ProductOwnerSettingMode = Literal["dry-run", "apply"]
ProductOwnerSettingOperation = Literal["set", "clear", "unchanged"]
PRODUCT_OWNER_SETTING_SOURCE: Literal["service:product-owner"] = "service:product-owner"
GitHubApiRequest = Callable[..., object]

_GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class ProductOwnerLoginNotFoundError(ValueError):
    pass


class ProductOwnerLoginNotUserError(ValueError):
    pass


class ProductOwnerLookupUnavailableError(RuntimeError):
    pass


class ProductOwnerApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: ProductOwnerSettingMode = "dry-run"
    github_login: str = ""
    clear: bool = False
    reason: str

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductOwnerApplyRequest":
        self.github_login = self.github_login.strip().removeprefix("@")
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Product owner request requires reason.")
        if self.clear and self.github_login:
            raise ValueError("Product owner request cannot both clear and name an Owner.")
        if not self.clear and not _GITHUB_LOGIN_PATTERN.fullmatch(self.github_login):
            raise ValueError("Product owner request requires a valid GitHub login.")
        return self


class ProductOwnerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_login: str = ""
    github_id: str = ""


class ProductOwnerSettingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductOwnerSettingMode
    product: str
    operation: ProductOwnerSettingOperation
    resolved_github_login: str = ""
    resolved_github_id: str = ""
    owner_before: ProductOwnerIdentity
    owner_after: ProductOwnerIdentity
    changed: bool
    applied: bool = False
    reason: str
    source_label: Literal["service:product-owner"] = PRODUCT_OWNER_SETTING_SOURCE
    profile_updated_at_before: str
    profile_updated_at_after: str = ""


def resolve_github_user_owner(
    *,
    login: str,
    token: str,
    api_request: GitHubApiRequest,
) -> ProductOwnerIdentity:
    """Resolve a typed login to GitHub's canonical login and immutable numeric id."""

    try:
        payload = api_request(path=f"/users/{login}", token=token)
    except click.ClickException as error:
        cause = error.__cause__
        if isinstance(cause, HTTPError) and cause.code == 404:
            raise ProductOwnerLoginNotFoundError(
                f"GitHub has no account with the login {login!r}."
            ) from error
        raise ProductOwnerLookupUnavailableError(
            "GitHub could not be reached to look up the Owner login."
        ) from error
    if not isinstance(payload, dict):
        raise ProductOwnerLookupUnavailableError("GitHub user lookup returned an invalid response.")
    account_type = payload.get("type")
    resolved_login = payload.get("login")
    resolved_id = payload.get("id")
    if account_type != "User":
        raise ProductOwnerLoginNotUserError(
            f"GitHub login {login!r} is not a person's user account."
        )
    if (
        not isinstance(resolved_login, str)
        or not resolved_login.strip()
        or isinstance(resolved_id, bool)
        or not isinstance(resolved_id, int)
        or resolved_id <= 0
    ):
        raise ProductOwnerLookupUnavailableError("GitHub user lookup returned an invalid response.")
    return ProductOwnerIdentity(github_login=resolved_login.strip(), github_id=str(resolved_id))


def build_product_owner_setting_plan(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductOwnerApplyRequest,
    resolved_owner: ProductOwnerIdentity,
) -> ProductOwnerSettingPlan:
    owner_before = ProductOwnerIdentity(
        github_login=profile.owner.github_login,
        github_id=profile.owner.github_id,
    )
    changed = owner_before != resolved_owner
    operation: ProductOwnerSettingOperation = "unchanged"
    if changed:
        operation = "clear" if request.clear else "set"
    return ProductOwnerSettingPlan(
        mode=request.mode,
        product=profile.product,
        operation=operation,
        resolved_github_login=resolved_owner.github_login,
        resolved_github_id=resolved_owner.github_id,
        owner_before=owner_before,
        owner_after=resolved_owner,
        changed=changed,
        reason=request.reason,
        profile_updated_at_before=profile.updated_at,
    )


def updated_product_owner_profile(
    *,
    profile: LaunchplaneProductProfileRecord,
    resolved_owner: ProductOwnerIdentity,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    updated_owner = ProductOwnerProfile(
        github_login=resolved_owner.github_login,
        github_id=resolved_owner.github_id,
        review_label=profile.owner.review_label,
    )
    updated_profile = profile.model_copy(
        update={
            "owner": updated_owner,
            "updated_at": updated_at,
            "source": PRODUCT_OWNER_SETTING_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(updated_profile.model_dump(mode="json"))
