"""Snapchat Ads CLI entry point.

JSON output by default (--human for tables). --account selects the configured
account. Exit codes: 0 success, 1 API error, 2 config/auth error.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

from . import __version__
from .api_client import SnapApiError, SnapAuthError, SnapchatApiClient
from .auth import (
    auth_status,
    build_authorize_url,
    exchange_code,
    make_refresh_callback,
    refresh_access_token,
    revoke_token,
)
from .config import (
    ACCOUNTS_FILE,
    AccountConfig,
    AppConfig,
    get_account,
    init_default_config,
    load_config,
)
from .output import emit, write_csv
from .safety import audit_log, to_micro
from .tools import _bulk
from .tools import (
    accounts as accounts_mod,
    ads as ads_mod,
    adsquads as adsquads_mod,
    audit_logs as audit_mod,
    billing as billing_mod,
    brand_lift as brand_lift_mod,
    campaigns as campaigns_mod,
    catalog as catalog_mod,
    conversions_api as capi_mod,
    creative_elements as creative_elements_mod,
    creatives as creatives_mod,
    custom_conversions as custom_conv_mod,
    estimate as estimate_mod,
    media as media_mod,
    mobile_apps as mobile_apps_mod,
    organizations as orgs_mod,
    pixels as pixels_mod,
    reports as reports_mod,
    segments as segments_mod,
    targeting as targeting_mod,
    user as user_mod,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")


def _bail(msg: str, exit_code: int = 2) -> None:
    click.echo(json.dumps({"error": {"message": msg}}, indent=2), err=True)
    sys.exit(exit_code)


def _resolve_client(
    ctx: click.Context, *, require_account: bool = True
) -> tuple[AppConfig, AccountConfig | None, SnapchatApiClient | None]:
    config: AppConfig = ctx.obj["config"]
    account_key: str = ctx.obj["account"]

    if not require_account:
        return config, None, None

    try:
        account = get_account(config, account_key)
    except ValueError as e:
        _bail(str(e), exit_code=2)
        return config, None, None

    token = account.load_token()
    if not token:
        _bail(
            f"No access token for account '{account_key}'. "
            f"Run `snapchat-ads --account {account_key} auth login` first.",
            exit_code=2,
        )
        return config, account, None

    refresh_cb = make_refresh_callback(config, account)
    client = SnapchatApiClient(token, refresh_callback=refresh_cb)
    return config, account, client


def _run(ctx: click.Context, fn, *, require_account: bool = True, **kwargs):
    """Wrap a tool call: build client, dispatch errors to stderr/exit, emit output."""
    config, account, client = _resolve_client(ctx, require_account=require_account)
    try:
        if require_account:
            assert client is not None
            try:
                result = fn(client, account, config, **kwargs)
            finally:
                client.close()
        else:
            result = fn(config, **kwargs)
    except SnapAuthError as e:
        _bail(f"Auth error: {e}. Try `snapchat-ads auth refresh` or `auth login`.", exit_code=2)
        return
    except SnapApiError as e:
        emit(e.to_dict(), human=ctx.obj.get("human", False))
        return
    emit(result, human=ctx.obj.get("human", False))


def _json_arg(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text())
    return json.loads(value)


def _csv_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--account", "-a", default="default", help="Account key from accounts.toml")
@click.option("--human", is_flag=True, default=False, help="Human-readable output (tables)")
@click.version_option(__version__, prog_name="snapchat-ads")
@click.pass_context
def cli(ctx: click.Context, account: str, human: bool) -> None:
    """Snapchat Ads CLI -- full Snap Marketing API surface."""
    ctx.ensure_object(dict)
    ctx.obj["account"] = account
    ctx.obj["human"] = human
    ctx.obj["config"] = load_config()


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


@cli.group()
def auth() -> None:
    """OAuth 2.0 token management."""


@auth.command("login")
@click.option("--scope", default=None, help="OAuth scope (defaults to snapchat-marketing-api)")
@click.option("--state", default=None, help="OAuth state value")
@click.option("--code", default=None, help="Skip URL prompt and exchange this code directly")
@click.pass_context
def auth_login(ctx: click.Context, scope: str | None, state: str | None, code: str | None) -> None:
    """Run the OAuth authorize-code flow and persist tokens."""
    config: AppConfig = ctx.obj["config"]
    account_key: str = ctx.obj["account"]

    if not ACCOUNTS_FILE.exists():
        path = init_default_config()
        click.echo(f"Created default config at {path}", err=True)
        config = load_config()
        ctx.obj["config"] = config

    try:
        account = get_account(config, account_key)
    except ValueError as e:
        _bail(str(e))
        return

    client_id = config.client_id
    client_secret = config.client_secret
    redirect_uri = config.redirect_uri
    scope = scope or config.default_scope

    missing = [name for name, val in [
        ("SNAPCHAT_CLIENT_ID", client_id),
        ("SNAPCHAT_CLIENT_SECRET", client_secret),
        ("SNAPCHAT_REDIRECT_URI", redirect_uri),
    ] if not val]
    if missing:
        _bail(f"Missing env vars: {', '.join(missing)}. See .env.example.")
        return

    assert client_id and client_secret and redirect_uri  # narrowing for mypy

    if not code:
        url = build_authorize_url(client_id, redirect_uri, scope, state)
        click.echo("\n1. Open this URL in a browser, sign in, and approve access:\n", err=True)
        click.echo(f"   {url}\n", err=True)
        click.echo(
            "2. After approval, Snap redirects to your redirect URI with `?code=...`. "
            "Paste that code value below.\n",
            err=True,
        )
        code = click.prompt("Authorization code", type=str)

    try:
        payload = exchange_code(client_id, client_secret, redirect_uri, code)
    except Exception as e:
        _bail(f"Code exchange failed: {e}", exit_code=1)
        return

    token_path = account.save_token(payload)
    audit_log(
        "auth.login",
        account_key,
        {"scope": scope},
        "ok",
        {"token_path": str(token_path)},
    )
    emit(
        {
            "auth_status": "OK",
            "account": account_key,
            "token_path": str(token_path),
            "expires_at": payload.get("expires_at"),
            "scope": payload.get("scope"),
            "has_access_token": bool(payload.get("access_token")),
            "has_refresh_token": bool(payload.get("refresh_token")),
            "token_tail": (payload.get("access_token") or "")[-6:],
        },
        ctx.obj["human"],
    )


@auth.command("exchange")
@click.argument("code")
@click.pass_context
def auth_exchange(ctx: click.Context, code: str) -> None:
    """Exchange an authorization code for tokens (non-interactive)."""
    ctx.invoke(auth_login, code=code)


@auth.command("refresh")
@click.pass_context
def auth_refresh(ctx: click.Context) -> None:
    """Rotate the access token using the stored refresh token."""
    config: AppConfig = ctx.obj["config"]
    account_key: str = ctx.obj["account"]
    try:
        account = get_account(config, account_key)
    except ValueError as e:
        _bail(str(e))
        return

    client_id = config.client_id
    client_secret = config.client_secret
    if not (client_id and client_secret):
        _bail("Missing SNAPCHAT_CLIENT_ID / SNAPCHAT_CLIENT_SECRET")
        return

    rtoken = account.load_refresh_token()
    if not rtoken:
        _bail("No refresh token stored. Run `auth login` first.")
        return

    try:
        payload = refresh_access_token(client_id, client_secret, rtoken)
    except Exception as e:
        _bail(f"Refresh failed: {e}", exit_code=1)
        return

    token_path = account.save_token(payload)
    audit_log("auth.refresh", account_key, {}, "ok", {"token_path": str(token_path)})
    emit(
        {
            "auth_status": "REFRESHED",
            "account": account_key,
            "token_path": str(token_path),
            "expires_at": payload.get("expires_at"),
            "has_access_token": bool(payload.get("access_token")),
            "has_refresh_token": bool(payload.get("refresh_token")),
            "token_tail": (payload.get("access_token") or "")[-6:],
        },
        ctx.obj["human"],
    )


@auth.command("status")
@click.pass_context
def auth_status_cmd(ctx: click.Context) -> None:
    """Report the on-disk token state."""
    config: AppConfig = ctx.obj["config"]
    account_key: str = ctx.obj["account"]
    try:
        account = get_account(config, account_key)
    except ValueError as e:
        _bail(str(e))
        return
    data = auth_status(account)
    data["account"] = account_key
    emit(data, ctx.obj["human"])


@auth.command("revoke")
@click.option("--token-type", type=click.Choice(["access", "refresh"]), default="access")
@click.pass_context
def auth_revoke_cmd(ctx: click.Context, token_type: str) -> None:
    """Revoke a token at Snap and remove it from the local file."""
    config: AppConfig = ctx.obj["config"]
    account_key: str = ctx.obj["account"]
    try:
        account = get_account(config, account_key)
    except ValueError as e:
        _bail(str(e))
        return

    meta = account.load_token_metadata()
    target = meta.get("access_token") if token_type == "access" else meta.get("refresh_token")
    if not target:
        _bail(f"No {token_type} token to revoke.")
        return

    result = revoke_token(target)
    audit_log("auth.revoke", account_key, {"token_type": token_type}, "ok", {})
    emit({"revoke_response": result, "account": account_key}, ctx.obj["human"])


# ---------------------------------------------------------------------------
# account
# ---------------------------------------------------------------------------


@cli.group()
def account() -> None:
    """Local account configuration commands."""


@account.command("list")
@click.pass_context
def account_list(ctx: click.Context) -> None:
    config: AppConfig = ctx.obj["config"]
    out: dict[str, Any] = {"count": len(config.accounts), "accounts": {}}
    for key, acct in config.accounts.items():
        out["accounts"][key] = {
            "name": acct.name,
            "organization_id": acct.organization_id,
            "ad_account_id": acct.ad_account_id,
            "pixel_id": acct.pixel_id,
            "has_token": bool(acct.load_token()),
        }
    emit(out, ctx.obj["human"])


@account.command("info")
@click.pass_context
def account_info(ctx: click.Context) -> None:
    """Show the live ad account record from the API."""

    def _fn(client, account, config):
        if not account.ad_account_id:
            return {"error": {"message": "ad_account_id not set in accounts.toml"}}
        return accounts_mod.get_ad_account(client, account.ad_account_id)

    _run(ctx, _fn)


@account.command("health-check")
@click.pass_context
def account_health(ctx: click.Context) -> None:
    def _fn(client, account, config):
        if not account.ad_account_id:
            return {"error": {"message": "ad_account_id not set in accounts.toml"}}
        return accounts_mod.health_check(client, account.ad_account_id)

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# org
# ---------------------------------------------------------------------------


@cli.group()
def org() -> None:
    """Organizations + members + roles."""


@org.command("list")
@click.option("--with-ad-accounts", is_flag=True)
@click.pass_context
def org_list(ctx: click.Context, with_ad_accounts: bool) -> None:
    _run(ctx, lambda c, a, cfg: orgs_mod.list_organizations(c, with_ad_accounts=with_ad_accounts))


@org.command("get")
@click.argument("org_id")
@click.pass_context
def org_get(ctx: click.Context, org_id: str) -> None:
    _run(ctx, lambda c, a, cfg: orgs_mod.get_organization(c, org_id))


@org.command("list-accounts")
@click.argument("org_id", required=False)
@click.pass_context
def org_list_accounts(ctx: click.Context, org_id: str | None) -> None:
    def _fn(client, account, config):
        oid = org_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORG_ID or set organization_id in accounts.toml"}}
        return orgs_mod.list_ad_accounts(client, oid)

    _run(ctx, _fn)


@org.command("members")
@click.argument("org_id")
@click.pass_context
def org_members(ctx: click.Context, org_id: str) -> None:
    _run(ctx, lambda c, a, cfg: orgs_mod.list_members(c, org_id))


@org.command("roles")
@click.argument("org_id")
@click.pass_context
def org_roles(ctx: click.Context, org_id: str) -> None:
    _run(ctx, lambda c, a, cfg: orgs_mod.list_roles(c, org_id))


@org.command("funding-sources")
@click.argument("org_id", required=False)
@click.pass_context
def org_funding_sources(ctx: click.Context, org_id: str | None) -> None:
    def _fn(client, account, config):
        oid = org_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORG_ID or set organization_id in accounts.toml"}}
        return accounts_mod.list_funding_sources(client, oid)

    _run(ctx, _fn)


@org.command("billing-centers")
@click.argument("org_id", required=False)
@click.pass_context
def org_billing_centers(ctx: click.Context, org_id: str | None) -> None:
    def _fn(client, account, config):
        oid = org_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORG_ID or set organization_id in accounts.toml"}}
        return accounts_mod.list_billing_centers(client, oid)

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# campaign
# ---------------------------------------------------------------------------


@cli.group()
def campaign() -> None:
    """Campaign CRUD."""


@campaign.command("list")
@click.option("--limit", type=int, default=100)
@click.pass_context
def campaign_list(ctx: click.Context, limit: int) -> None:
    def _fn(client, account, config):
        if not account.ad_account_id:
            return {"error": {"message": "ad_account_id not set"}}
        return campaigns_mod.list_campaigns(client, account.ad_account_id, limit=limit)

    _run(ctx, _fn)


@campaign.command("get")
@click.argument("campaign_id")
@click.pass_context
def campaign_get(ctx: click.Context, campaign_id: str) -> None:
    _run(ctx, lambda c, a, cfg: campaigns_mod.get_campaign(c, campaign_id))


@campaign.command("get-by-ids")
@click.argument("campaign_ids", nargs=-1, required=False)
@click.option("--file", "ids_file", default=None, type=click.Path(),
              help="Read IDs from JSON array, CSV, or one-per-line text")
@click.pass_context
def campaign_get_by_ids(
    ctx: click.Context, campaign_ids: tuple[str, ...], ids_file: str | None
) -> None:
    ids = _bulk.load_ids(positional=campaign_ids, file_path=ids_file)
    if not ids:
        _bail("Provide CAMPAIGN_IDS positional args or --file")
        return

    def _fn(client, account, config):
        return _bulk.bulk_get_by_ids(
            client,
            path=f"adaccounts/{account.ad_account_id}/get_campaigns_by_ids",
            ids=ids,
            id_array_key="campaign_ids",
            response_array_key="campaigns",
            inner_singular="campaign",
        )

    _run(ctx, _fn)


@campaign.command("create")
@click.option("--name", required=True)
@click.option("--objective", default=None, help="Legacy objective (sunsetting; prefer --objective-v2-type)")
@click.option("--objective-v2-type", default=None, help="objective_v2_properties.objective_v2_type")
@click.option("--promotion-type", default=None, help="objective_v2_properties.promotion_type")
@click.option("--buy-model", default=None, type=click.Choice(["AUCTION", "RESERVED"]))
@click.option("--reserved-type", default=None, help="e.g. REACH_AND_FREQUENCY")
@click.option("--status", default="PAUSED", type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--daily-budget", default=None, help="Dollars (e.g. 50 or '50.00') or 'micro:50000000'")
@click.option("--lifetime-spend-cap", default=None, help="Dollars or micro:value")
@click.option("--start-time", default=None)
@click.option("--end-time", default=None)
@click.option("--reach-frequency-spec-json", default=None, help="JSON for R&F campaigns")
@click.option("--extra-json", default=None, help="Raw JSON merged into the payload")
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_create(
    ctx: click.Context,
    name: str,
    objective: str | None,
    objective_v2_type: str | None,
    promotion_type: str | None,
    buy_model: str | None,
    reserved_type: str | None,
    status: str,
    daily_budget: str | None,
    lifetime_spend_cap: str | None,
    start_time: str | None,
    end_time: str | None,
    reach_frequency_spec_json: str | None,
    extra_json: str | None,
    execute: bool,
) -> None:
    extra = json.loads(extra_json) if extra_json else None
    rf_spec = json.loads(reach_frequency_spec_json) if reach_frequency_spec_json else None
    daily_micro = to_micro(daily_budget) if daily_budget else None
    lifetime_micro = to_micro(lifetime_spend_cap) if lifetime_spend_cap else None

    if not (objective or objective_v2_type):
        _bail("Provide --objective or --objective-v2-type")
        return

    def _fn(client, account, config):
        result = campaigns_mod.create_campaign(
            client,
            account.ad_account_id,
            ctx.obj["account"],
            name=name,
            objective=objective,
            objective_v2_type=objective_v2_type,
            promotion_type=promotion_type,
            buy_model=buy_model,
            reserved_type=reserved_type,
            status=status,
            daily_budget_micro=daily_micro,
            lifetime_spend_cap_micro=lifetime_micro,
            start_time=start_time,
            end_time=end_time,
            reach_frequency_spec=rf_spec,
            extra=extra,
            execute=execute,
        )
        if execute:
            audit_log("campaign.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@campaign.command("update")
@click.argument("campaign_id")
@click.option("--name", default=None)
@click.option("--status", default=None, type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--daily-budget", default=None)
@click.option("--lifetime-spend-cap", default=None)
@click.option("--fields-json", default=None, help="Raw JSON of fields to PUT")
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_update(
    ctx: click.Context,
    campaign_id: str,
    name: str | None,
    status: str | None,
    daily_budget: str | None,
    lifetime_spend_cap: str | None,
    fields_json: str | None,
    execute: bool,
) -> None:
    fields: dict[str, Any] = json.loads(fields_json) if fields_json else {}
    if name is not None:
        fields["name"] = name
    if status is not None:
        fields["status"] = status
    if daily_budget is not None:
        fields["daily_budget_micro"] = to_micro(daily_budget)
    if lifetime_spend_cap is not None:
        fields["lifetime_spend_cap_micro"] = to_micro(lifetime_spend_cap)

    def _fn(client, account, config):
        result = campaigns_mod.update_campaign(
            client,
            account.ad_account_id,
            campaign_id,
            ctx.obj["account"],
            fields=fields,
            execute=execute,
        )
        if execute:
            audit_log("campaign.update", ctx.obj["account"], {"id": campaign_id, "fields": fields}, "ok")
        return result

    _run(ctx, _fn)


@campaign.command("pause")
@click.argument("campaign_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_pause(ctx: click.Context, campaign_id: str, execute: bool) -> None:
    ctx.invoke(campaign_update, campaign_id=campaign_id, status="PAUSED", execute=execute)


@campaign.command("launch")
@click.argument("campaign_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_launch(ctx: click.Context, campaign_id: str, execute: bool) -> None:
    ctx.invoke(campaign_update, campaign_id=campaign_id, status="ACTIVE", execute=execute)


@campaign.command("delete")
@click.argument("campaign_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_delete(ctx: click.Context, campaign_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = campaigns_mod.delete_campaign(
            client, campaign_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("campaign.delete", ctx.obj["account"], {"id": campaign_id}, "ok")
        return result

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# adsquad
# ---------------------------------------------------------------------------


@cli.group()
def adsquad() -> None:
    """Ad Squad CRUD + spend guidance."""


@adsquad.command("list")
@click.option("--campaign-id", default=None)
@click.option("--limit", type=int, default=100)
@click.pass_context
def adsquad_list(ctx: click.Context, campaign_id: str | None, limit: int) -> None:
    def _fn(client, account, config):
        return adsquads_mod.list_ad_squads(
            client,
            ad_account_id=account.ad_account_id if not campaign_id else None,
            campaign_id=campaign_id,
            limit=limit,
        )

    _run(ctx, _fn)


@adsquad.command("get")
@click.argument("ad_squad_id")
@click.pass_context
def adsquad_get(ctx: click.Context, ad_squad_id: str) -> None:
    _run(ctx, lambda c, a, cfg: adsquads_mod.get_ad_squad(c, ad_squad_id))


@adsquad.command("create")
@click.argument("campaign_id")
@click.option("--payload-json", required=True, help="Full ad-squad payload JSON")
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_create(ctx: click.Context, campaign_id: str, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)

    def _fn(client, account, config):
        result = adsquads_mod.create_ad_squad(
            client, campaign_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("adsquad.create", ctx.obj["account"], {"campaign_id": campaign_id}, "ok")
        return result

    _run(ctx, _fn)


@adsquad.command("update")
@click.argument("ad_squad_id")
@click.option("--name", default=None)
@click.option("--status", default=None, type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--daily-budget", default=None)
@click.option("--fields-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_update(
    ctx: click.Context,
    ad_squad_id: str,
    name: str | None,
    status: str | None,
    daily_budget: str | None,
    fields_json: str | None,
    execute: bool,
) -> None:
    fields: dict[str, Any] = json.loads(fields_json) if fields_json else {}
    if name is not None:
        fields["name"] = name
    if status is not None:
        fields["status"] = status
    if daily_budget is not None:
        fields["daily_budget_micro"] = to_micro(daily_budget)

    def _fn(client, account, config):
        result = adsquads_mod.update_ad_squad(
            client,
            account.ad_account_id,
            ad_squad_id,
            ctx.obj["account"],
            fields=fields,
            execute=execute,
        )
        if execute:
            audit_log("adsquad.update", ctx.obj["account"], {"id": ad_squad_id, "fields": fields}, "ok")
        return result

    _run(ctx, _fn)


@adsquad.command("pause")
@click.argument("ad_squad_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_pause(ctx: click.Context, ad_squad_id: str, execute: bool) -> None:
    ctx.invoke(adsquad_update, ad_squad_id=ad_squad_id, status="PAUSED", execute=execute)


@adsquad.command("launch")
@click.argument("ad_squad_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_launch(ctx: click.Context, ad_squad_id: str, execute: bool) -> None:
    ctx.invoke(adsquad_update, ad_squad_id=ad_squad_id, status="ACTIVE", execute=execute)


@adsquad.command("delete")
@click.argument("ad_squad_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_delete(ctx: click.Context, ad_squad_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = adsquads_mod.delete_ad_squad(
            client, ad_squad_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("adsquad.delete", ctx.obj["account"], {"id": ad_squad_id}, "ok")
        return result

    _run(ctx, _fn)


@adsquad.command("spend-guidance")
@click.option("--signal-type", default="PIXEL")
@click.option("--signal-id", default=None)
@click.option("--optimization-goal", default=None)
@click.pass_context
def adsquad_spend_guidance(
    ctx: click.Context,
    signal_type: str,
    signal_id: str | None,
    optimization_goal: str | None,
) -> None:
    def _fn(client, account, config):
        return adsquads_mod.spend_guidance(
            client,
            account.ad_account_id,
            signal_type=signal_type,
            signal_id=signal_id,
            optimization_goal=optimization_goal,
        )

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# ad
# ---------------------------------------------------------------------------


@cli.group()
def ad() -> None:
    """Ad CRUD + bulk."""


@ad.command("list")
@click.option("--campaign-id", default=None)
@click.option("--ad-squad-id", default=None)
@click.option("--limit", type=int, default=100)
@click.pass_context
def ad_list(ctx: click.Context, campaign_id: str | None, ad_squad_id: str | None, limit: int) -> None:
    def _fn(client, account, config):
        return ads_mod.list_ads(
            client,
            ad_account_id=account.ad_account_id if not (campaign_id or ad_squad_id) else None,
            campaign_id=campaign_id,
            ad_squad_id=ad_squad_id,
            limit=limit,
        )

    _run(ctx, _fn)


@ad.command("get")
@click.argument("ad_id")
@click.pass_context
def ad_get(ctx: click.Context, ad_id: str) -> None:
    _run(ctx, lambda c, a, cfg: ads_mod.get_ad(c, ad_id))


@ad.command("get-by-ids")
@click.argument("ad_ids", nargs=-1, required=False)
@click.option("--file", "ids_file", default=None, type=click.Path())
@click.pass_context
def ad_get_by_ids(
    ctx: click.Context, ad_ids: tuple[str, ...], ids_file: str | None
) -> None:
    ids = _bulk.load_ids(positional=ad_ids, file_path=ids_file)
    if not ids:
        _bail("Provide AD_IDS positional args or --file")
        return

    def _fn(client, account, config):
        return _bulk.bulk_get_by_ids(
            client,
            path=f"adaccounts/{account.ad_account_id}/get_ads_by_ids",
            ids=ids,
            id_array_key="ad_ids",
            response_array_key="ads",
            inner_singular="ad",
        )

    _run(ctx, _fn)


@ad.command("create")
@click.argument("ad_squad_id")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_create(ctx: click.Context, ad_squad_id: str, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)

    def _fn(client, account, config):
        result = ads_mod.create_ad(
            client, ad_squad_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("ad.create", ctx.obj["account"], {"ad_squad_id": ad_squad_id}, "ok")
        return result

    _run(ctx, _fn)


@ad.command("update")
@click.argument("ad_squad_id")
@click.argument("ad_id")
@click.option("--name", default=None)
@click.option("--status", default=None, type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--fields-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_update(
    ctx: click.Context,
    ad_squad_id: str,
    ad_id: str,
    name: str | None,
    status: str | None,
    fields_json: str | None,
    execute: bool,
) -> None:
    fields: dict[str, Any] = json.loads(fields_json) if fields_json else {}
    if name is not None:
        fields["name"] = name
    if status is not None:
        fields["status"] = status

    def _fn(client, account, config):
        result = ads_mod.update_ad(
            client, ad_squad_id, ad_id, ctx.obj["account"], fields=fields, execute=execute
        )
        if execute:
            audit_log("ad.update", ctx.obj["account"], {"id": ad_id, "fields": fields}, "ok")
        return result

    _run(ctx, _fn)


@ad.command("pause")
@click.argument("ad_squad_id")
@click.argument("ad_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_pause(ctx: click.Context, ad_squad_id: str, ad_id: str, execute: bool) -> None:
    ctx.invoke(ad_update, ad_squad_id=ad_squad_id, ad_id=ad_id, status="PAUSED", execute=execute)


@ad.command("launch")
@click.argument("ad_squad_id")
@click.argument("ad_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_launch(ctx: click.Context, ad_squad_id: str, ad_id: str, execute: bool) -> None:
    ctx.invoke(ad_update, ad_squad_id=ad_squad_id, ad_id=ad_id, status="ACTIVE", execute=execute)


@ad.command("delete")
@click.argument("ad_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_delete(ctx: click.Context, ad_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = ads_mod.delete_ad(client, ad_id, ctx.obj["account"], execute=execute)
        if execute:
            audit_log("ad.delete", ctx.obj["account"], {"id": ad_id}, "ok")
        return result

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# manage (bulk operations)
# ---------------------------------------------------------------------------


@cli.group()
def manage() -> None:
    """Bulk operations across many entities."""


@manage.command("bulk-pause")
@click.argument("ad_ids", nargs=-1, required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def bulk_pause(ctx: click.Context, ad_ids: tuple[str, ...], execute: bool) -> None:
    def _fn(client, account, config):
        result = ads_mod.bulk_set_status(
            client,
            account.ad_account_id,
            ctx.obj["account"],
            ad_ids=list(ad_ids),
            status="PAUSED",
            execute=execute,
        )
        if execute:
            audit_log("ad.bulk_pause", ctx.obj["account"], {"ad_ids": list(ad_ids)}, "ok")
        return result

    _run(ctx, _fn)


@manage.command("bulk-launch")
@click.argument("ad_ids", nargs=-1, required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def bulk_launch(ctx: click.Context, ad_ids: tuple[str, ...], execute: bool) -> None:
    def _fn(client, account, config):
        result = ads_mod.bulk_set_status(
            client,
            account.ad_account_id,
            ctx.obj["account"],
            ad_ids=list(ad_ids),
            status="ACTIVE",
            execute=execute,
        )
        if execute:
            audit_log("ad.bulk_launch", ctx.obj["account"], {"ad_ids": list(ad_ids)}, "ok")
        return result

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# creative
# ---------------------------------------------------------------------------


@cli.group()
def creative() -> None:
    """Creative CRUD + preview."""


@creative.command("list")
@click.option("--limit", type=int, default=100)
@click.pass_context
def creative_list(ctx: click.Context, limit: int) -> None:
    def _fn(client, account, config):
        return creatives_mod.list_creatives(client, account.ad_account_id, limit=limit)

    _run(ctx, _fn)


@creative.command("get")
@click.argument("creative_id")
@click.pass_context
def creative_get(ctx: click.Context, creative_id: str) -> None:
    _run(ctx, lambda c, a, cfg: creatives_mod.get_creative(c, creative_id))


@creative.command("create")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def creative_create(ctx: click.Context, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)

    def _fn(client, account, config):
        result = creatives_mod.create_creative(
            client, account.ad_account_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("creative.create", ctx.obj["account"], {"name": payload.get("name")}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("update")
@click.argument("creative_id")
@click.option("--fields-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def creative_update(ctx: click.Context, creative_id: str, fields_json: str, execute: bool) -> None:
    fields = json.loads(fields_json)

    def _fn(client, account, config):
        result = creatives_mod.update_creative(
            client, account.ad_account_id, creative_id, ctx.obj["account"], fields=fields, execute=execute
        )
        if execute:
            audit_log("creative.update", ctx.obj["account"], {"id": creative_id}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("preview")
@click.argument("creative_id")
@click.pass_context
def creative_preview(ctx: click.Context, creative_id: str) -> None:
    _run(ctx, lambda c, a, cfg: creatives_mod.preview_creative(c, creative_id))


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------


@cli.group()
def media() -> None:
    """Media upload + status."""


@media.command("list")
@click.option("--limit", type=int, default=100)
@click.pass_context
def media_list(ctx: click.Context, limit: int) -> None:
    def _fn(client, account, config):
        return media_mod.list_media(client, account.ad_account_id, limit=limit)

    _run(ctx, _fn)


@media.command("get")
@click.argument("media_id")
@click.pass_context
def media_get(ctx: click.Context, media_id: str) -> None:
    _run(ctx, lambda c, a, cfg: media_mod.get_media(c, media_id))


@media.command("status")
@click.argument("media_id")
@click.pass_context
def media_status_cmd(ctx: click.Context, media_id: str) -> None:
    _run(ctx, lambda c, a, cfg: media_mod.media_status(c, media_id))


@media.command("upload")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option(
    "--type",
    "media_type",
    default="VIDEO",
    type=click.Choice(["VIDEO", "IMAGE", "PLAYABLE", "LENS_PACKAGE"]),
)
@click.option("--name", default=None)
@click.option("--no-poll", is_flag=True, help="Skip status polling after upload")
@click.option("--poll-timeout", type=float, default=300.0)
@click.option("--execute", is_flag=True)
@click.pass_context
def media_upload(
    ctx: click.Context,
    file_path: str,
    media_type: str,
    name: str | None,
    no_poll: bool,
    poll_timeout: float,
    execute: bool,
) -> None:
    def _fn(client, account, config):
        result = media_mod.upload_media(
            client,
            account.ad_account_id,
            ctx.obj["account"],
            file_path=file_path,
            media_type=media_type,
            name=name,
            poll_until_ready=not no_poll,
            poll_timeout=poll_timeout,
            execute=execute,
        )
        if execute:
            audit_log("media.upload", ctx.obj["account"], {"file": file_path, "type": media_type}, "ok")
        return result

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# segment
# ---------------------------------------------------------------------------


@cli.group()
def segment() -> None:
    """Audience segment CRUD + user upload (SHA256)."""


@segment.command("list")
@click.option("--limit", type=int, default=100)
@click.pass_context
def segment_list(ctx: click.Context, limit: int) -> None:
    def _fn(client, account, config):
        return segments_mod.list_segments(client, account.ad_account_id, limit=limit)

    _run(ctx, _fn)


@segment.command("get")
@click.argument("segment_id")
@click.pass_context
def segment_get(ctx: click.Context, segment_id: str) -> None:
    _run(ctx, lambda c, a, cfg: segments_mod.get_segment(c, segment_id))


@segment.command("create")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_create(ctx: click.Context, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)

    def _fn(client, account, config):
        result = segments_mod.create_segment(
            client, account.ad_account_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("segment.create", ctx.obj["account"], {"name": payload.get("name")}, "ok")
        return result

    _run(ctx, _fn)


@segment.command("update")
@click.argument("segment_id")
@click.option("--fields-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_update(ctx: click.Context, segment_id: str, fields_json: str, execute: bool) -> None:
    fields = json.loads(fields_json)

    def _fn(client, account, config):
        result = segments_mod.update_segment(
            client, account.ad_account_id, segment_id, ctx.obj["account"], fields=fields, execute=execute
        )
        if execute:
            audit_log("segment.update", ctx.obj["account"], {"id": segment_id}, "ok")
        return result

    _run(ctx, _fn)


@segment.command("delete")
@click.argument("segment_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_delete(ctx: click.Context, segment_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = segments_mod.delete_segment(
            client, segment_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("segment.delete", ctx.obj["account"], {"id": segment_id}, "ok")
        return result

    _run(ctx, _fn)


@segment.command("clear")
@click.argument("segment_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_clear(ctx: click.Context, segment_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = segments_mod.clear_segment_users(
            client, segment_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("segment.clear", ctx.obj["account"], {"id": segment_id}, "ok")
        return result

    _run(ctx, _fn)


@segment.command("add-users")
@click.argument("segment_id")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--schema", required=True, type=click.Choice(["EMAIL_SHA256", "PHONE_SHA256", "MOBILE_AD_ID_SHA256"]))
@click.option("--pre-hashed", is_flag=True, help="Skip local SHA256 hashing")
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_add_users(
    ctx: click.Context,
    segment_id: str,
    file_path: str,
    schema: str,
    pre_hashed: bool,
    execute: bool,
) -> None:
    def _fn(client, account, config):
        result = segments_mod.add_users(
            client,
            segment_id,
            ctx.obj["account"],
            file_path=file_path,
            schema=schema,
            pre_hashed=pre_hashed,
            execute=execute,
        )
        if execute:
            audit_log("segment.add_users", ctx.obj["account"], {"id": segment_id, "schema": schema}, "ok")
        return result

    _run(ctx, _fn)


@segment.command("remove-users")
@click.argument("segment_id")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--schema", required=True, type=click.Choice(["EMAIL_SHA256", "PHONE_SHA256", "MOBILE_AD_ID_SHA256"]))
@click.option("--pre-hashed", is_flag=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_remove_users(
    ctx: click.Context,
    segment_id: str,
    file_path: str,
    schema: str,
    pre_hashed: bool,
    execute: bool,
) -> None:
    def _fn(client, account, config):
        result = segments_mod.remove_users(
            client,
            segment_id,
            ctx.obj["account"],
            file_path=file_path,
            schema=schema,
            pre_hashed=pre_hashed,
            execute=execute,
        )
        if execute:
            audit_log("segment.remove_users", ctx.obj["account"], {"id": segment_id, "schema": schema}, "ok")
        return result

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# pixel
# ---------------------------------------------------------------------------


@cli.group()
def pixel() -> None:
    """Snap Pixel CRUD + domain stats."""


@pixel.command("list")
@click.pass_context
def pixel_list(ctx: click.Context) -> None:
    def _fn(client, account, config):
        return pixels_mod.list_pixels(client, account.ad_account_id)

    _run(ctx, _fn)


@pixel.command("get")
@click.argument("pixel_id")
@click.pass_context
def pixel_get(ctx: click.Context, pixel_id: str) -> None:
    _run(ctx, lambda c, a, cfg: pixels_mod.get_pixel(c, pixel_id))


@pixel.command("create")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def pixel_create(ctx: click.Context, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)

    def _fn(client, account, config):
        result = pixels_mod.create_pixel(
            client, account.ad_account_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("pixel.create", ctx.obj["account"], {"name": payload.get("name")}, "ok")
        return result

    _run(ctx, _fn)


@pixel.command("update")
@click.argument("pixel_id")
@click.option("--fields-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def pixel_update(ctx: click.Context, pixel_id: str, fields_json: str, execute: bool) -> None:
    fields = json.loads(fields_json)

    def _fn(client, account, config):
        result = pixels_mod.update_pixel(
            client, account.ad_account_id, pixel_id, ctx.obj["account"], fields=fields, execute=execute
        )
        if execute:
            audit_log("pixel.update", ctx.obj["account"], {"id": pixel_id}, "ok")
        return result

    _run(ctx, _fn)


@pixel.command("domain-stats")
@click.argument("pixel_id", required=False)
@click.option("--granularity", default="DAY", type=click.Choice(["HOUR", "DAY"]))
@click.pass_context
def pixel_domain_stats(ctx: click.Context, pixel_id: str | None, granularity: str) -> None:
    def _fn(client, account, config):
        pid = pixel_id or account.pixel_id
        if not pid:
            return {"error": {"message": "Provide PIXEL_ID or set pixel_id in accounts.toml"}}
        return pixels_mod.domain_stats(client, pid, granularity=granularity)

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# targeting
# ---------------------------------------------------------------------------


@cli.group()
def targeting() -> None:
    """Targeting insights + reference enumerations."""


@targeting.command("insights")
@click.option("--spec-json", required=True, help="Targeting spec JSON")
@click.pass_context
def targeting_insights_cmd(ctx: click.Context, spec_json: str) -> None:
    spec = json.loads(spec_json)

    def _fn(client, account, config):
        return targeting_mod.targeting_insights(client, account.ad_account_id, spec=spec)

    _run(ctx, _fn)


@targeting.command("geo-search")
@click.option("--country", "country_code", required=True)
@click.option(
    "--type",
    "location_type",
    required=True,
    type=click.Choice(["region", "metro", "dma", "postal_code", "circle", "country"]),
)
@click.option("--query", default=None)
@click.pass_context
def targeting_geo_search(
    ctx: click.Context, country_code: str, location_type: str, query: str | None
) -> None:
    def _fn(client, account, config):
        return targeting_mod.geo_search(
            client, country_code=country_code, location_type=location_type, query=query
        )

    _run(ctx, _fn)


@targeting.command("demo-reference")
@click.pass_context
def targeting_demo(ctx: click.Context) -> None:
    emit(targeting_mod.static_reference("demo"), ctx.obj["human"])


@targeting.command("geo-reference")
@click.pass_context
def targeting_geo_ref(ctx: click.Context) -> None:
    emit(targeting_mod.static_reference("geo"), ctx.obj["human"])


@targeting.command("interests-reference")
@click.pass_context
def targeting_interests(ctx: click.Context) -> None:
    emit(targeting_mod.static_reference("interests"), ctx.obj["human"])


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@cli.group()
def report() -> None:
    """Sync stats + async report jobs."""


@report.command("stats")
@click.option(
    "--entity",
    "entity_type",
    required=True,
    type=click.Choice(["ad", "ad_squad", "campaign", "ad_account", "creative", "pixel"]),
)
@click.option("--id", "entity_id", default=None)
@click.option("--granularity", default="TOTAL", type=click.Choice(["TOTAL", "DAY", "HOUR", "LIFETIME"]))
@click.option("--start-time", default=None)
@click.option("--end-time", default=None)
@click.option("--fields", default=None, help="Comma-separated metric names")
@click.option("--breakdown", default=None)
@click.option("--swipe-up-attribution-window", default=None)
@click.option("--view-attribution-window", default=None)
@click.option("--omit-empty/--include-empty", default=None)
@click.option("--report-dimension", default=None)
@click.option("--position-stats/--no-position-stats", default=None)
@click.option("--platform-stats/--no-platform-stats", default=None)
@click.option("--params-json", default=None, help="Extra stats query params as JSON or @file")
@click.option("--csv-out", default=None, help="Write rows as CSV to this path")
@click.pass_context
def report_stats(
    ctx: click.Context,
    entity_type: str,
    entity_id: str | None,
    granularity: str,
    start_time: str | None,
    end_time: str | None,
    fields: str | None,
    breakdown: str | None,
    swipe_up_attribution_window: str | None,
    view_attribution_window: str | None,
    omit_empty: bool | None,
    report_dimension: str | None,
    position_stats: bool | None,
    platform_stats: bool | None,
    params_json: str | None,
    csv_out: str | None,
) -> None:
    extra_params = _json_arg(params_json)

    def _fn(client, account, config):
        eid = entity_id
        if not eid and entity_type == "ad_account":
            eid = account.ad_account_id
        if not eid:
            return {"error": {"message": "Provide --id"}}
        body = reports_mod.sync_stats(
            client,
            entity_type=entity_type,
            entity_id=eid,
            granularity=granularity,
            start_time=start_time,
            end_time=end_time,
            fields=_csv_list(fields),
            breakdown=breakdown,
            swipe_up_attribution_window=swipe_up_attribution_window,
            view_attribution_window=view_attribution_window,
            omit_empty=omit_empty,
            report_dimension=report_dimension,
            position_stats=position_stats,
            platform_stats=platform_stats,
            extra_params=extra_params,
        )
        if csv_out:
            rows = reports_mod._flatten_stats(body)
            write_csv(rows, out_path=csv_out)
            return {"csv_out": csv_out, "rows": len(rows)}
        return body

    _run(ctx, _fn)


@report.command("daily")
@click.option("--days", type=int, default=7)
@click.option("--fields", default=None)
@click.pass_context
def report_daily(ctx: click.Context, days: int, fields: str | None) -> None:
    def _fn(client, account, config):
        return reports_mod.daily_report(
            client,
            account.ad_account_id,
            days=days,
            fields=_csv_list(fields),
        )

    _run(ctx, _fn)


@report.command("hourly")
@click.option("--hours", type=int, default=24)
@click.option("--fields", default=None)
@click.pass_context
def report_hourly(ctx: click.Context, hours: int, fields: str | None) -> None:
    def _fn(client, account, config):
        return reports_mod.hourly_report(
            client,
            account.ad_account_id,
            hours=hours,
            fields=_csv_list(fields),
        )

    _run(ctx, _fn)


@report.command("top-ads")
@click.option("--days", type=int, default=7)
@click.option("--by", "sort_by", default="spend")
@click.option("--limit", type=int, default=25)
@click.pass_context
def report_top_ads(ctx: click.Context, days: int, sort_by: str, limit: int) -> None:
    def _fn(client, account, config):
        return reports_mod.top_ads(
            client, account.ad_account_id, days=days, sort_by=sort_by, limit=limit
        )

    _run(ctx, _fn)


@report.command("video")
@click.option("--days", type=int, default=7)
@click.pass_context
def report_video(ctx: click.Context, days: int) -> None:
    def _fn(client, account, config):
        return reports_mod.video_report(client, account.ad_account_id, days=days)

    _run(ctx, _fn)


@report.command("async-submit")
@click.option(
    "--entity",
    "entity_type",
    default="ad_account",
    type=click.Choice(["ad", "ad_squad", "campaign", "ad_account", "creative", "pixel"]),
)
@click.option("--id", "entity_id", default=None)
@click.option("--granularity", default="TOTAL", type=click.Choice(["TOTAL", "DAY", "HOUR", "LIFETIME"]))
@click.option("--start-time", default=None)
@click.option("--end-time", default=None)
@click.option("--fields", default=None, help="Comma-separated metric names")
@click.option("--breakdown", default=None)
@click.option("--swipe-up-attribution-window", default=None)
@click.option("--view-attribution-window", default=None)
@click.option("--omit-empty/--include-empty", default=None)
@click.option("--report-dimension", default=None)
@click.option("--position-stats/--no-position-stats", default=None)
@click.option("--platform-stats/--no-platform-stats", default=None)
@click.option("--async-format", default="csv", type=click.Choice(["csv", "json"]))
@click.option("--params-json", default=None, help="Extra stats query params as JSON or @file")
@click.option("--payload-json", default=None, help="Deprecated alias for --params-json")
@click.pass_context
def report_async_submit(
    ctx: click.Context,
    entity_type: str,
    entity_id: str | None,
    granularity: str,
    start_time: str | None,
    end_time: str | None,
    fields: str | None,
    breakdown: str | None,
    swipe_up_attribution_window: str | None,
    view_attribution_window: str | None,
    omit_empty: bool | None,
    report_dimension: str | None,
    position_stats: bool | None,
    platform_stats: bool | None,
    async_format: str,
    params_json: str | None,
    payload_json: str | None,
) -> None:
    extra_params = _json_arg(params_json or payload_json)

    def _fn(client, account, config):
        eid = entity_id or (account.ad_account_id if entity_type == "ad_account" else None)
        if not eid:
            return {"error": {"message": "Provide --id for non-ad-account async reports"}}
        return reports_mod.async_submit(
            client,
            entity_type=entity_type,
            entity_id=eid,
            granularity=granularity,
            start_time=start_time,
            end_time=end_time,
            fields=_csv_list(fields),
            breakdown=breakdown,
            swipe_up_attribution_window=swipe_up_attribution_window,
            view_attribution_window=view_attribution_window,
            omit_empty=omit_empty,
            report_dimension=report_dimension,
            position_stats=position_stats,
            platform_stats=platform_stats,
            async_format=async_format,
            extra_params=extra_params,
        )

    _run(ctx, _fn)


@report.command("async-status")
@click.argument("report_run_id")
@click.option(
    "--entity",
    "entity_type",
    default="ad_account",
    type=click.Choice(["ad", "ad_squad", "campaign", "ad_account", "creative", "pixel"]),
)
@click.option("--id", "entity_id", default=None)
@click.pass_context
def report_async_status(
    ctx: click.Context,
    report_run_id: str,
    entity_type: str,
    entity_id: str | None,
) -> None:
    def _fn(client, account, config):
        eid = entity_id or (account.ad_account_id if entity_type == "ad_account" else None)
        if not eid:
            return {"error": {"message": "Provide --id for non-ad-account async reports"}}
        return reports_mod.async_status(
            client,
            entity_type=entity_type,
            entity_id=eid,
            report_run_id=report_run_id,
        )
    _run(ctx, _fn)


@report.command("async-download")
@click.argument("report_run_id")
@click.option(
    "--entity",
    "entity_type",
    default="ad_account",
    type=click.Choice(["ad", "ad_squad", "campaign", "ad_account", "creative", "pixel"]),
)
@click.option("--id", "entity_id", default=None)
@click.option("--out", "out_path", default=None)
@click.pass_context
def report_async_download(
    ctx: click.Context,
    report_run_id: str,
    entity_type: str,
    entity_id: str | None,
    out_path: str | None,
) -> None:
    def _fn(client, account, config):
        eid = entity_id or (account.ad_account_id if entity_type == "ad_account" else None)
        if not eid:
            return {"error": {"message": "Provide --id for non-ad-account async reports"}}
        return reports_mod.async_download(
            client,
            report_run_id,
            entity_type=entity_type,
            entity_id=eid,
            out_path=out_path,
        )
    _run(ctx, _fn)


@report.command("lead-gen-submit")
@click.option("--start-time", required=True)
@click.option("--end-time", required=True)
@click.option("--async-format", default="csv", type=click.Choice(["csv"]))
@click.pass_context
def report_lead_gen_submit(
    ctx: click.Context,
    start_time: str,
    end_time: str,
    async_format: str,
) -> None:
    def _fn(client, account, config):
        return reports_mod.lead_gen_submit(
            client,
            ad_account_id=account.ad_account_id,
            start_time=start_time,
            end_time=end_time,
            async_format=async_format,
        )
    _run(ctx, _fn)


@report.command("lead-gen-status")
@click.argument("report_run_id")
@click.pass_context
def report_lead_gen_status(ctx: click.Context, report_run_id: str) -> None:
    def _fn(client, account, config):
        return reports_mod.lead_gen_status(
            client,
            ad_account_id=account.ad_account_id,
            report_run_id=report_run_id,
        )
    _run(ctx, _fn)


@report.command("lead-gen-download")
@click.argument("report_run_id")
@click.option("--out", "out_path", default=None)
@click.pass_context
def report_lead_gen_download(
    ctx: click.Context,
    report_run_id: str,
    out_path: str | None,
) -> None:
    def _fn(client, account, config):
        return reports_mod.lead_gen_download(
            client,
            ad_account_id=account.ad_account_id,
            report_run_id=report_run_id,
            out_path=out_path,
        )
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


@cli.group()
def catalog() -> None:
    """Product catalog + product sets (Collection Ads)."""


@catalog.command("list")
@click.argument("org_id", required=False)
@click.pass_context
def catalog_list(ctx: click.Context, org_id: str | None) -> None:
    def _fn(client, account, config):
        oid = org_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORG_ID or set organization_id in accounts.toml"}}
        return catalog_mod.list_catalogs(client, oid)

    _run(ctx, _fn)


@catalog.command("get")
@click.argument("catalog_id")
@click.pass_context
def catalog_get(ctx: click.Context, catalog_id: str) -> None:
    _run(ctx, lambda c, a, cfg: catalog_mod.get_catalog(c, catalog_id))


@catalog.command("product-sets")
@click.argument("catalog_id")
@click.pass_context
def catalog_product_sets(ctx: click.Context, catalog_id: str) -> None:
    _run(ctx, lambda c, a, cfg: catalog_mod.list_product_sets(c, catalog_id))


@catalog.command("product-set-get")
@click.argument("product_set_id")
@click.pass_context
def catalog_product_set_get(ctx: click.Context, product_set_id: str) -> None:
    _run(ctx, lambda c, a, cfg: catalog_mod.get_product_set(c, product_set_id))


# ---------------------------------------------------------------------------
# init (config bootstrap)
# ---------------------------------------------------------------------------


@cli.command("init")
@click.pass_context
def init_cmd(ctx: click.Context) -> None:
    """Create the default accounts.toml if it does not exist."""
    path = init_default_config()
    emit(
        {"status": "ok", "config_path": str(path), "exists": path.exists()},
        ctx.obj["human"],
    )


# ---------------------------------------------------------------------------
# user (/me)
# ---------------------------------------------------------------------------


@cli.command("me")
@click.pass_context
def user_me(ctx: click.Context) -> None:
    """Get the authenticated user (GET /me)."""
    _run(ctx, lambda c, a, cfg: user_mod.me(c))


# ---------------------------------------------------------------------------
# creative-element + interaction-zone
# ---------------------------------------------------------------------------


@cli.group("creative-element")
def creative_element() -> None:
    """Creative element CRUD (Collection / DPA)."""


@creative_element.command("list")
@click.pass_context
def ce_list(ctx: click.Context) -> None:
    def _fn(client, account, config):
        return creative_elements_mod.list_creative_elements(client, account.ad_account_id)
    _run(ctx, _fn)


@creative_element.command("get")
@click.argument("element_id")
@click.pass_context
def ce_get(ctx: click.Context, element_id: str) -> None:
    _run(ctx, lambda c, a, cfg: creative_elements_mod.get_creative_element(c, element_id))


@creative_element.command("create")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def ce_create(ctx: click.Context, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        result = creative_elements_mod.create_creative_element(
            client, account.ad_account_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("creative_element.create", ctx.obj["account"], {}, "ok")
        return result
    _run(ctx, _fn)


@creative_element.command("update")
@click.argument("element_id")
@click.option("--fields-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def ce_update(ctx: click.Context, element_id: str, fields_json: str, execute: bool) -> None:
    fields = json.loads(fields_json)
    def _fn(client, account, config):
        result = creative_elements_mod.update_creative_element(
            client, account.ad_account_id, element_id, ctx.obj["account"], fields=fields, execute=execute
        )
        if execute:
            audit_log("creative_element.update", ctx.obj["account"], {"id": element_id}, "ok")
        return result
    _run(ctx, _fn)


@cli.group("interaction-zone")
def interaction_zone() -> None:
    """Interaction zone CRUD."""


@interaction_zone.command("list")
@click.pass_context
def iz_list(ctx: click.Context) -> None:
    def _fn(client, account, config):
        return creative_elements_mod.list_interaction_zones(client, account.ad_account_id)
    _run(ctx, _fn)


@interaction_zone.command("get")
@click.argument("zone_id")
@click.pass_context
def iz_get(ctx: click.Context, zone_id: str) -> None:
    _run(ctx, lambda c, a, cfg: creative_elements_mod.get_interaction_zone(c, zone_id))


@interaction_zone.command("create")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def iz_create(ctx: click.Context, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        result = creative_elements_mod.create_interaction_zone(
            client, account.ad_account_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("interaction_zone.create", ctx.obj["account"], {}, "ok")
        return result
    _run(ctx, _fn)


@interaction_zone.command("update")
@click.argument("zone_id")
@click.option("--fields-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def iz_update(ctx: click.Context, zone_id: str, fields_json: str, execute: bool) -> None:
    fields = json.loads(fields_json)
    def _fn(client, account, config):
        result = creative_elements_mod.update_interaction_zone(
            client, account.ad_account_id, zone_id, ctx.obj["account"], fields=fields, execute=execute
        )
        if execute:
            audit_log("interaction_zone.update", ctx.obj["account"], {"id": zone_id}, "ok")
        return result
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# pixel custom-conversions (extends pixel group)
# ---------------------------------------------------------------------------


@pixel.group("custom-conversions")
def pixel_custom_conv() -> None:
    """Pixel custom conversion CRUD."""


@pixel_custom_conv.command("list")
@click.argument("pixel_id", required=False)
@click.pass_context
def pcc_list(ctx: click.Context, pixel_id: str | None) -> None:
    def _fn(client, account, config):
        pid = pixel_id or account.pixel_id
        if not pid:
            return {"error": {"message": "Provide PIXEL_ID or set pixel_id in accounts.toml"}}
        return custom_conv_mod.list_pixel_custom_conversions(client, pid)
    _run(ctx, _fn)


@pixel_custom_conv.command("create")
@click.option("--pixel-id", default=None)
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def pcc_create(ctx: click.Context, pixel_id: str | None, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        pid = pixel_id or account.pixel_id
        if not pid:
            return {"error": {"message": "Provide --pixel-id or set pixel_id in accounts.toml"}}
        result = custom_conv_mod.create_pixel_custom_conversion(
            client, pid, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("pixel.custom_conversion.create", ctx.obj["account"], {"pixel_id": pid}, "ok")
        return result
    _run(ctx, _fn)


@pixel_custom_conv.command("get")
@click.argument("conversion_id")
@click.pass_context
def pcc_get(ctx: click.Context, conversion_id: str) -> None:
    _run(ctx, lambda c, a, cfg: custom_conv_mod.get_custom_conversion(c, conversion_id))


@pixel_custom_conv.command("delete")
@click.argument("conversion_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def pcc_delete(ctx: click.Context, conversion_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = custom_conv_mod.delete_custom_conversion(
            client, conversion_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("custom_conversion.delete", ctx.obj["account"], {"id": conversion_id}, "ok")
        return result
    _run(ctx, _fn)


@pixel.command("stats")
@click.argument("pixel_id", required=False)
@click.option("--granularity", default="DAY", type=click.Choice(["HOUR", "DAY", "TOTAL", "LIFETIME"]))
@click.option("--start-time", default=None)
@click.option("--end-time", default=None)
@click.option("--fields", default=None)
@click.pass_context
def pixel_stats_cmd(
    ctx: click.Context,
    pixel_id: str | None,
    granularity: str,
    start_time: str | None,
    end_time: str | None,
    fields: str | None,
) -> None:
    def _fn(client, account, config):
        pid = pixel_id or account.pixel_id
        if not pid:
            return {"error": {"message": "Provide PIXEL_ID or set pixel_id in accounts.toml"}}
        return pixels_mod.pixel_stats(
            client,
            pid,
            granularity=granularity,
            start_time=start_time,
            end_time=end_time,
            fields=[f.strip() for f in fields.split(",")] if fields else None,
        )
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# mobileapp
# ---------------------------------------------------------------------------


@cli.group("mobileapp")
def mobileapp() -> None:
    """Snap App ID management."""


@mobileapp.command("list")
@click.option("--organization-id", default=None)
@click.pass_context
def ma_list(ctx: click.Context, organization_id: str | None) -> None:
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        return mobile_apps_mod.list_mobile_apps(client, organization_id=oid or None)
    _run(ctx, _fn)


@mobileapp.command("get")
@click.argument("mobile_app_id")
@click.pass_context
def ma_get(ctx: click.Context, mobile_app_id: str) -> None:
    _run(ctx, lambda c, a, cfg: mobile_apps_mod.get_mobile_app(c, mobile_app_id))


@mobileapp.command("create")
@click.option("--organization-id", default=None)
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def ma_create(
    ctx: click.Context, organization_id: str | None, payload_json: str, execute: bool
) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide --organization-id or set organization_id in accounts.toml"}}
        result = mobile_apps_mod.create_mobile_app(
            client, oid, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("mobile_app.create", ctx.obj["account"], {"name": payload.get("name")}, "ok")
        return result
    _run(ctx, _fn)


@mobileapp.command("ecid-status")
@click.argument("snap_app_id")
@click.pass_context
def ma_ecid(ctx: click.Context, snap_app_id: str) -> None:
    _run(ctx, lambda c, a, cfg: mobile_apps_mod.ecid_status(c, snap_app_id))


@mobileapp.group("custom-conversions")
def mobileapp_custom_conv() -> None:
    """App-based custom conversions."""


@mobileapp_custom_conv.command("list")
@click.argument("mobile_app_id")
@click.pass_context
def mcc_list(ctx: click.Context, mobile_app_id: str) -> None:
    _run(ctx, lambda c, a, cfg: custom_conv_mod.list_mobile_app_custom_conversions(c, mobile_app_id))


@mobileapp_custom_conv.command("create")
@click.argument("mobile_app_id")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def mcc_create(ctx: click.Context, mobile_app_id: str, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        result = custom_conv_mod.create_mobile_app_custom_conversion(
            client, mobile_app_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("mobile_app.custom_conversion.create", ctx.obj["account"], {"mobile_app_id": mobile_app_id}, "ok")
        return result
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# capi (server-side conversions)
# ---------------------------------------------------------------------------


@cli.group("capi")
def capi() -> None:
    """Server-side Conversions API (CAPI v3)."""


@capi.command("send")
@click.option("--pixel-id", default=None)
@click.option("--snap-app-id", default=None)
@click.option("--events-file", "events_file", required=True, type=click.Path())
@click.option("--no-hash", is_flag=True, help="Skip auto-SHA256 of identifiers")
@click.option("--test-event-code", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def capi_send(
    ctx: click.Context,
    pixel_id: str | None,
    snap_app_id: str | None,
    events_file: str,
    no_hash: bool,
    test_event_code: str | None,
    execute: bool,
) -> None:
    """Send events to https://tr.snapchat.com/v3/conversion."""
    config: AppConfig = ctx.obj["config"]
    account_key: str = ctx.obj["account"]
    try:
        account = get_account(config, account_key)
    except ValueError as e:
        _bail(str(e))
        return

    token = account.load_token()
    if not token:
        _bail(f"No access token for '{account_key}'. Run `auth login`.")
        return

    pid = pixel_id or account.pixel_id
    events = list(capi_mod.load_events_from_file(events_file))
    if not events:
        _bail("No events parsed from file.")
        return

    if not execute:
        emit(
            capi_mod.send_events_preview(
                pixel_id=pid,
                snap_app_id=snap_app_id,
                events=events,
                auto_hash=not no_hash,
                account_label=account_key,
                test_event_code=test_event_code,
            ),
            ctx.obj["human"],
        )
        return

    result = capi_mod.send_events(
        access_token=token,
        pixel_id=pid,
        snap_app_id=snap_app_id,
        events=events,
        test_event_code=test_event_code,
        auto_hash=not no_hash,
    )
    audit_log(
        "capi.send",
        account_key,
        {"pixel_id": pid, "snap_app_id": snap_app_id, "count": len(events)},
        "ok",
    )
    emit(result, ctx.obj["human"])


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@cli.group("audit")
def audit_group() -> None:
    """External changelogs (audit trail)."""


@audit_group.command("changelog")
@click.option(
    "--entity",
    "entity_type",
    required=True,
    type=click.Choice(["campaign", "ad_squad", "ad", "creative", "dynamic_template"]),
)
@click.argument("entity_id")
@click.option("--limit", type=int, default=100)
@click.pass_context
def audit_changelog(ctx: click.Context, entity_type: str, entity_id: str, limit: int) -> None:
    _run(
        ctx,
        lambda c, a, cfg: audit_mod.changelog(
            c, entity_type=entity_type, entity_id=entity_id, limit=limit
        ),
    )


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------


@cli.group("estimate")
def estimate_group() -> None:
    """Bid / audience-size / reach-frequency / outcome forecasts."""


@estimate_group.command("bid")
@click.option("--payload-json", required=True)
@click.pass_context
def est_bid(ctx: click.Context, payload_json: str) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        return estimate_mod.bid_estimate(client, account.ad_account_id, payload=payload)
    _run(ctx, _fn)


@estimate_group.command("audience-size")
@click.option("--payload-json", required=True)
@click.pass_context
def est_audience(ctx: click.Context, payload_json: str) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        return estimate_mod.audience_size(client, account.ad_account_id, payload=payload)
    _run(ctx, _fn)


@estimate_group.command("reach-frequency")
@click.option("--payload-json", required=True)
@click.pass_context
def est_rf(ctx: click.Context, payload_json: str) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        return estimate_mod.reach_frequency_schedule(client, account.ad_account_id, payload=payload)
    _run(ctx, _fn)


@estimate_group.command("adsquad-outcomes")
@click.option("--payload-json", required=True)
@click.pass_context
def est_outcomes(ctx: click.Context, payload_json: str) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        return estimate_mod.adsquad_outcomes(client, account.ad_account_id, payload=payload)
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# billing (invoices + transactions, read-only)
# ---------------------------------------------------------------------------


@cli.group("billing")
def billing_group() -> None:
    """Invoices + transactions (read-only)."""


@billing_group.command("invoices")
@click.argument("organization_id", required=False)
@click.pass_context
def bill_invoices(ctx: click.Context, organization_id: str | None) -> None:
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORGANIZATION_ID or set organization_id in accounts.toml"}}
        return billing_mod.list_invoices(client, oid)
    _run(ctx, _fn)


@billing_group.command("invoice")
@click.argument("invoice_id")
@click.pass_context
def bill_invoice_get(ctx: click.Context, invoice_id: str) -> None:
    _run(ctx, lambda c, a, cfg: billing_mod.get_invoice(c, invoice_id))


@billing_group.command("transactions")
@click.argument("organization_id", required=False)
@click.pass_context
def bill_transactions(ctx: click.Context, organization_id: str | None) -> None:
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORGANIZATION_ID or set organization_id in accounts.toml"}}
        return billing_mod.list_transactions(client, oid)
    _run(ctx, _fn)


@billing_group.command("transaction")
@click.argument("transaction_id")
@click.pass_context
def bill_transaction_get(ctx: click.Context, transaction_id: str) -> None:
    _run(ctx, lambda c, a, cfg: billing_mod.get_transaction(c, transaction_id))


# ---------------------------------------------------------------------------
# Extensions to existing groups: ad-account CRUD, member-roles, media batch ops,
# catalog write ops, targeting device names + options-by-country.
# ---------------------------------------------------------------------------


@org.command("create-account")
@click.argument("organization_id", required=False)
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def org_create_account(
    ctx: click.Context, organization_id: str | None, payload_json: str, execute: bool
) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORGANIZATION_ID or set organization_id in accounts.toml"}}
        result = accounts_mod.create_ad_account(
            client, oid, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("ad_account.create", ctx.obj["account"], {"organization_id": oid}, "ok")
        return result
    _run(ctx, _fn)


@org.command("update-account")
@click.argument("ad_account_id")
@click.option("--organization-id", default=None)
@click.option("--fields-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def org_update_account(
    ctx: click.Context,
    ad_account_id: str,
    organization_id: str | None,
    fields_json: str,
    execute: bool,
) -> None:
    fields = json.loads(fields_json)
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide --organization-id or set organization_id in accounts.toml"}}
        result = accounts_mod.update_ad_account(
            client, oid, ad_account_id, ctx.obj["account"], fields=fields, execute=execute
        )
        if execute:
            audit_log("ad_account.update", ctx.obj["account"], {"id": ad_account_id}, "ok")
        return result
    _run(ctx, _fn)


@org.command("assign-role")
@click.argument("organization_id", required=False)
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def org_assign_role(
    ctx: click.Context, organization_id: str | None, payload_json: str, execute: bool
) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORGANIZATION_ID or set organization_id in accounts.toml"}}
        result = orgs_mod.assign_org_role(
            client, oid, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("org.role.assign", ctx.obj["account"], {"organization_id": oid}, "ok")
        return result
    _run(ctx, _fn)


@org.command("member-roles")
@click.argument("member_id")
@click.pass_context
def org_member_roles_cmd(ctx: click.Context, member_id: str) -> None:
    _run(ctx, lambda c, a, cfg: orgs_mod.member_roles(c, member_id))


@account.command("phone-numbers")
@click.pass_context
def account_phone_numbers(ctx: click.Context) -> None:
    def _fn(client, account, config):
        if not account.ad_account_id:
            return {"error": {"message": "ad_account_id not set in accounts.toml"}}
        return accounts_mod.list_phone_numbers(client, account.ad_account_id)
    _run(ctx, _fn)


@account.command("assign-role")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def account_assign_role(ctx: click.Context, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        if not account.ad_account_id:
            return {"error": {"message": "ad_account_id not set in accounts.toml"}}
        result = accounts_mod.assign_ad_account_role(
            client, account.ad_account_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("account.role.assign", ctx.obj["account"], {"ad_account_id": account.ad_account_id}, "ok")
        return result
    _run(ctx, _fn)


@account.command("remove-role")
@click.argument("role_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def account_remove_role(ctx: click.Context, role_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = accounts_mod.remove_role(
            client, role_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("account.role.remove", ctx.obj["account"], {"role_id": role_id}, "ok")
        return result
    _run(ctx, _fn)


@media.command("get-by-ids")
@click.argument("media_ids", nargs=-1, required=False)
@click.option("--file", "ids_file", default=None, type=click.Path())
@click.pass_context
def media_get_by_ids(
    ctx: click.Context, media_ids: tuple[str, ...], ids_file: str | None
) -> None:
    ids = _bulk.load_ids(positional=media_ids, file_path=ids_file)
    if not ids:
        _bail("Provide MEDIA_IDS positional args or --file")
        return

    def _fn(client, account, config):
        return _bulk.bulk_get_by_ids(
            client,
            path=f"adaccounts/{account.ad_account_id}/get_media_by_ids",
            ids=ids,
            id_array_key="media_ids",
            response_array_key="media",
            inner_singular="media",
        )
    _run(ctx, _fn)


@media.command("preview")
@click.argument("media_id")
@click.pass_context
def media_preview_cmd(ctx: click.Context, media_id: str) -> None:
    _run(ctx, lambda c, a, cfg: media_mod.media_preview(c, media_id))


@media.command("thumbnail")
@click.argument("media_id")
@click.pass_context
def media_thumbnail_cmd(ctx: click.Context, media_id: str) -> None:
    _run(ctx, lambda c, a, cfg: media_mod.media_thumbnail(c, media_id))


@media.command("lens-preview")
@click.argument("media_id")
@click.pass_context
def media_lens_preview_cmd(ctx: click.Context, media_id: str) -> None:
    _run(ctx, lambda c, a, cfg: media_mod.lens_preview(c, media_id))


@media.command("copy")
@click.argument("media_ids", nargs=-1, required=True)
@click.option("--dest-ad-account-id", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def media_copy_cmd(
    ctx: click.Context,
    media_ids: tuple[str, ...],
    dest_ad_account_id: str | None,
    execute: bool,
) -> None:
    def _fn(client, account, config):
        dest = dest_ad_account_id or account.ad_account_id
        if not dest:
            return {"error": {"message": "Provide --dest-ad-account-id or set ad_account_id in accounts.toml"}}
        result = media_mod.copy_media(
            client, dest, ctx.obj["account"], media_ids=list(media_ids), execute=execute
        )
        if execute:
            audit_log("media.copy", ctx.obj["account"], {"dest": dest, "count": len(media_ids)}, "ok")
        return result
    _run(ctx, _fn)


@media.command("claim")
@click.argument("snap_reference")
@click.option("--execute", is_flag=True)
@click.pass_context
def media_claim_cmd(ctx: click.Context, snap_reference: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = media_mod.claim_media(
            client,
            account.ad_account_id,
            ctx.obj["account"],
            snap_reference=snap_reference,
            execute=execute,
        )
        if execute:
            audit_log("media.claim", ctx.obj["account"], {"snap_reference": snap_reference}, "ok")
        return result
    _run(ctx, _fn)


@catalog.command("create")
@click.argument("organization_id", required=False)
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def catalog_create(
    ctx: click.Context, organization_id: str | None, payload_json: str, execute: bool
) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide ORGANIZATION_ID or set organization_id in accounts.toml"}}
        result = catalog_mod.create_catalog(
            client, oid, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("catalog.create", ctx.obj["account"], {"organization_id": oid}, "ok")
        return result
    _run(ctx, _fn)


@catalog.command("create-product-set")
@click.argument("catalog_id")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def catalog_create_ps(
    ctx: click.Context, catalog_id: str, payload_json: str, execute: bool
) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        result = catalog_mod.create_product_set(
            client, catalog_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("catalog.product_set.create", ctx.obj["account"], {"catalog_id": catalog_id}, "ok")
        return result
    _run(ctx, _fn)


@catalog.command("update-product-set")
@click.argument("catalog_id")
@click.argument("product_set_id")
@click.option("--fields-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def catalog_update_ps(
    ctx: click.Context,
    catalog_id: str,
    product_set_id: str,
    fields_json: str,
    execute: bool,
) -> None:
    fields = json.loads(fields_json)
    def _fn(client, account, config):
        result = catalog_mod.update_product_set(
            client,
            catalog_id,
            product_set_id,
            ctx.obj["account"],
            fields=fields,
            execute=execute,
        )
        if execute:
            audit_log(
                "catalog.product_set.update",
                ctx.obj["account"],
                {"id": product_set_id},
                "ok",
            )
        return result
    _run(ctx, _fn)


@catalog.command("dynamic-templates")
@click.argument("catalog_id")
@click.pass_context
def catalog_dynamic_templates(ctx: click.Context, catalog_id: str) -> None:
    _run(ctx, lambda c, a, cfg: catalog_mod.list_dynamic_templates(c, catalog_id))


@catalog.command("dynamic-template-get")
@click.argument("dynamic_template_id")
@click.pass_context
def catalog_dynamic_template_get(ctx: click.Context, dynamic_template_id: str) -> None:
    _run(ctx, lambda c, a, cfg: catalog_mod.get_dynamic_template(c, dynamic_template_id))


@targeting.command("device-names")
@click.option("--os-type", default=None, type=click.Choice(["iOS", "ANDROID"]))
@click.pass_context
def targeting_device_names(ctx: click.Context, os_type: str | None) -> None:
    _run(
        ctx,
        lambda c, a, cfg: targeting_mod.device_marketing_names(c, os_type=os_type),
    )


@targeting.command("options-by-country")
@click.option("--country", "country_code", required=True)
@click.pass_context
def targeting_options_country(ctx: click.Context, country_code: str) -> None:
    _run(
        ctx,
        lambda c, a, cfg: targeting_mod.options_by_country(c, country_code),
    )


# ---------------------------------------------------------------------------
# adsquad smart-create (uses build_ad_squad_payload helper)
# ---------------------------------------------------------------------------


@adsquad.command("smart-create")
@click.argument("campaign_id")
@click.option("--name", required=True)
@click.option("--type", "type_", default="SNAP_ADS", type=click.Choice(["SNAP_ADS", "STORY", "AD_TO_LENS"]))
@click.option("--placement-v2-json", default=None, help='e.g. {"config":"AUTOMATIC"}')
@click.option(
    "--optimization-goal",
    default=None,
    type=click.Choice([
        "IMPRESSIONS", "SWIPES", "APP_INSTALLS", "VIDEO_VIEWS", "VIDEO_VIEWS_15_SEC",
        "PIXEL_PURCHASE", "PIXEL_SIGNUP", "PIXEL_ADD_CART", "PIXEL_PAGE_VIEW",
        "USES", "STORY_OPENS", "LENS_OPEN", "LENS_PLAY",
        "CATALOG_PURCHASES", "CATALOG_ADD_CART", "CATALOG_VIEW_CONTENT",
    ]),
)
@click.option(
    "--bid-strategy",
    default=None,
    type=click.Choice(["AUTO_BID", "LOWEST_COST_WITH_MAX_BID", "TARGET_COST", "MIN_ROAS"]),
)
@click.option("--bid", default=None, help="Dollars (max bid) or micro:value")
@click.option("--target-cost", default=None, help="Dollars or micro:value")
@click.option("--min-roas", type=float, default=None)
@click.option("--daily-budget", default=None, help="Dollars or micro:value")
@click.option("--lifetime-budget", default=None, help="Dollars or micro:value")
@click.option(
    "--billing-event",
    default=None,
    type=click.Choice(["IMPRESSION", "SWIPE", "VIEW", "USES", "INSTALL", "AUCTION"]),
)
@click.option(
    "--conversion-window",
    default=None,
    type=click.Choice(["SWIPE_1DAY", "SWIPE_7DAY", "SWIPE_28DAY",
                       "SWIPE_28DAY_VIEW_1DAY", "SWIPE_7DAY_VIEW_1DAY"]),
)
@click.option("--pixel-id", default=None)
@click.option("--snap-pixel-id", default=None)
@click.option("--targeting-json", default=None)
@click.option("--start-time", default=None)
@click.option("--end-time", default=None)
@click.option(
    "--skadnetwork-status",
    default=None,
    type=click.Choice(["ENROLLED", "NEVER_ENROLLED", "WITHDRAWN"]),
)
@click.option("--attribution-settings-json", default=None)
@click.option("--status", default="PAUSED", type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--extra-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_smart_create(
    ctx: click.Context,
    campaign_id: str,
    name: str,
    type_: str,
    placement_v2_json: str | None,
    optimization_goal: str | None,
    bid_strategy: str | None,
    bid: str | None,
    target_cost: str | None,
    min_roas: float | None,
    daily_budget: str | None,
    lifetime_budget: str | None,
    billing_event: str | None,
    conversion_window: str | None,
    pixel_id: str | None,
    snap_pixel_id: str | None,
    targeting_json: str | None,
    start_time: str | None,
    end_time: str | None,
    skadnetwork_status: str | None,
    attribution_settings_json: str | None,
    status: str,
    extra_json: str | None,
    execute: bool,
) -> None:
    """Compose an ad squad from first-class flags (no hand-rolled JSON needed)."""
    payload = adsquads_mod.build_ad_squad_payload(
        name=name,
        campaign_id=campaign_id,
        type=type_,
        placement_v2=json.loads(placement_v2_json) if placement_v2_json else None,
        optimization_goal=optimization_goal,
        bid_strategy=bid_strategy,
        bid_micro=to_micro(bid) if bid else None,
        target_cost_micro=to_micro(target_cost) if target_cost else None,
        min_roas=min_roas,
        daily_budget_micro=to_micro(daily_budget) if daily_budget else None,
        lifetime_budget_micro=to_micro(lifetime_budget) if lifetime_budget else None,
        billing_event=billing_event,
        conversion_window=conversion_window,
        pixel_id=pixel_id,
        snap_pixel_id=snap_pixel_id,
        targeting=json.loads(targeting_json) if targeting_json else None,
        start_time=start_time,
        end_time=end_time,
        skadnetwork_status=skadnetwork_status,
        attribution_settings=json.loads(attribution_settings_json) if attribution_settings_json else None,
        status=status,
        extra=json.loads(extra_json) if extra_json else None,
    )

    def _fn(client, account, config):
        result = adsquads_mod.create_ad_squad(
            client, campaign_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("adsquad.smart_create", ctx.obj["account"], {"campaign_id": campaign_id, "name": name}, "ok")
        return result

    _run(ctx, _fn)


@adsquad.command("restrictions")
@click.argument("ad_squad_id")
@click.pass_context
def adsquad_restrictions(ctx: click.Context, ad_squad_id: str) -> None:
    _run(ctx, lambda c, a, cfg: adsquads_mod.ad_squad_ad_restrictions(c, ad_squad_id))


# ---------------------------------------------------------------------------
# Creative type-specific subcommands
# ---------------------------------------------------------------------------


def _shared_creative_opts(f):
    f = click.option("--name", required=True)(f)
    f = click.option("--headline", required=True)(f)
    f = click.option("--brand-name", required=True)(f)
    f = click.option("--top-snap-media-id", required=True)(f)
    f = click.option("--shareable/--not-shareable", default=True)(f)
    f = click.option("--profile-id", default=None, help="Snap Public Profile ID")(f)
    f = click.option("--call-to-action", default=None)(f)
    f = click.option("--cta-color-display-mode", default=None)(f)
    f = click.option("--chat-properties-json", default=None)(f)
    f = click.option("--extra-json", default=None)(f)
    f = click.option("--execute", is_flag=True)(f)
    return f


@creative.command("create-app-install")
@_shared_creative_opts
@click.option("--app-name", required=True)
@click.option("--icon-media-id", required=True)
@click.option("--ios-app-id", default=None)
@click.option("--android-app-url", default=None)
@click.option("--end-card-media-id", default=None)
@click.pass_context
def creative_app_install(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None,
    android_app_url: str | None,
    end_card_media_id: str | None,
) -> None:
    extra = _json_arg(extra_json)
    chat_properties = _json_arg(chat_properties_json)

    def _fn(client, account, config):
        result = creatives_mod.create_app_install(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id,
            app_name=app_name, icon_media_id=icon_media_id,
            ios_app_id=ios_app_id, android_app_url=android_app_url,
            end_card_media_id=end_card_media_id, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            chat_properties=chat_properties,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.app_install.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("create-web-view")
@_shared_creative_opts
@click.option("--url", required=True)
@click.option("--allow-snap-js-sdk", is_flag=True)
@click.option("--immersive", is_flag=True)
@click.option("--block-preload", is_flag=True)
@click.option("--deep-link-urls", default=None, help="Comma-separated list")
@click.pass_context
def creative_web_view(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    url: str,
    allow_snap_js_sdk: bool,
    immersive: bool,
    block_preload: bool,
    deep_link_urls: str | None,
) -> None:
    extra = _json_arg(extra_json)
    chat_properties = _json_arg(chat_properties_json)
    dl_list = _csv_list(deep_link_urls)

    def _fn(client, account, config):
        result = creatives_mod.create_web_view(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, url=url,
            allow_snap_javascript_sdk=allow_snap_js_sdk,
            use_immersive_mode=immersive, block_preload=block_preload,
            deep_link_urls=dl_list, shareable=shareable,
            call_to_action=call_to_action, profile_id=profile_id,
            cta_color_display_mode=cta_color_display_mode,
            chat_properties=chat_properties, extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.web_view.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("create-deep-link")
@_shared_creative_opts
@click.option("--deep-link-uri", required=True)
@click.option("--app-name", required=True)
@click.option("--icon-media-id", required=True)
@click.option("--ios-app-id", default=None)
@click.option("--android-app-url", default=None)
@click.option("--fallback-type", default="WEB_VIEW")
@click.option("--fallback-url", default=None)
@click.pass_context
def creative_deep_link(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    deep_link_uri: str,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None,
    android_app_url: str | None,
    fallback_type: str,
    fallback_url: str | None,
) -> None:
    extra = _json_arg(extra_json)

    def _fn(client, account, config):
        result = creatives_mod.create_deep_link(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, deep_link_uri=deep_link_uri,
            app_name=app_name, icon_media_id=icon_media_id,
            ios_app_id=ios_app_id, android_app_url=android_app_url,
            fallback_type=fallback_type, fallback_url=fallback_url,
            shareable=shareable, profile_id=profile_id,
            call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.deep_link.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("create-ad-to-lens")
@_shared_creative_opts
@click.option("--lens-id", required=True)
@click.option("--icon-media-id", default=None)
@click.pass_context
def creative_ad_to_lens(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    lens_id: str,
    icon_media_id: str | None,
) -> None:
    extra = _json_arg(extra_json)

    def _fn(client, account, config):
        result = creatives_mod.create_ad_to_lens(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, lens_id=lens_id,
            icon_media_id=icon_media_id, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.ad_to_lens.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("create-collection")
@_shared_creative_opts
@click.option("--interaction-zone-id", required=True)
@click.option("--default-fallback-type", default="WEB_VIEW")
@click.option("--fallback-url", default=None)
@click.pass_context
def creative_collection(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    interaction_zone_id: str,
    default_fallback_type: str,
    fallback_url: str | None,
) -> None:
    extra = _json_arg(extra_json)

    def _fn(client, account, config):
        result = creatives_mod.create_collection(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id,
            interaction_zone_id=interaction_zone_id,
            default_fallback_interaction_type=default_fallback_type,
            fallback_url=fallback_url, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.collection.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("create-longform-video")
@_shared_creative_opts
@click.option("--long-form-video-media-id", required=True)
@click.option("--icon-media-id", default=None)
@click.pass_context
def creative_longform(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    long_form_video_media_id: str,
    icon_media_id: str | None,
) -> None:
    extra = _json_arg(extra_json)

    def _fn(client, account, config):
        result = creatives_mod.create_longform_video(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id,
            long_form_video_media_id=long_form_video_media_id,
            icon_media_id=icon_media_id, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.longform.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("create-lead-generation")
@_shared_creative_opts
@click.option("--lead-form-id", required=True)
@click.pass_context
def creative_lead_gen(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    lead_form_id: str,
) -> None:
    extra = _json_arg(extra_json)

    def _fn(client, account, config):
        result = creatives_mod.create_lead_generation(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, lead_form_id=lead_form_id,
            shareable=shareable, profile_id=profile_id,
            call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.lead_gen.create", ctx.obj["account"], {"name": name}, "ok")
        return result

    _run(ctx, _fn)


@creative.command("create-snap-ad")
@_shared_creative_opts
@click.pass_context
def creative_snap_ad(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_snap_ad(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.snap_ad.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-ad-to-call")
@_shared_creative_opts
@click.option("--phone-number-id", required=True)
@click.pass_context
def creative_ad_to_call(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    phone_number_id: str,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_ad_to_call(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, phone_number_id=phone_number_id,
            shareable=shareable, profile_id=profile_id,
            call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.ad_to_call.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-ad-to-message")
@_shared_creative_opts
@click.option("--phone-number-id", required=True)
@click.option("--message-text", default=None)
@click.pass_context
def creative_ad_to_message(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    phone_number_id: str,
    message_text: str | None,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_ad_to_message(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, phone_number_id=phone_number_id,
            message_text=message_text, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.ad_to_message.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-reminder")
@_shared_creative_opts
@click.option("--event-detail-id", required=True)
@click.pass_context
def creative_reminder(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    event_detail_id: str,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_reminder(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, event_detail_id=event_detail_id,
            shareable=shareable, profile_id=profile_id,
            call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.reminder.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-story-preview")
@click.option("--name", required=True)
@click.option("--preview-media-id", required=True)
@click.option("--logo-media-id", default=None)
@click.option("--preview-headline", default=None)
@click.option("--shareable/--not-shareable", default=True)
@click.option("--extra-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def creative_story_preview(
    ctx: click.Context,
    name: str,
    preview_media_id: str,
    logo_media_id: str | None,
    preview_headline: str | None,
    shareable: bool,
    extra_json: str | None,
    execute: bool,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_story_preview(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, preview_media_id=preview_media_id,
            logo_media_id=logo_media_id, preview_headline=preview_headline,
            shareable=shareable, extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.story_preview.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-story-composite")
@click.option("--name", required=True)
@click.option("--headline", required=True)
@click.option("--brand-name", required=True)
@click.option("--preview-creative-id", required=True)
@click.option("--creative-ids", required=True, help="Comma-separated snap creative IDs")
@click.option("--profile-id", default=None)
@click.option("--chat-properties-json", default=None)
@click.option("--shareable/--not-shareable", default=True)
@click.option("--extra-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def creative_story_composite(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    preview_creative_id: str,
    creative_ids: str,
    profile_id: str | None,
    chat_properties_json: str | None,
    shareable: bool,
    extra_json: str | None,
    execute: bool,
) -> None:
    extra = _json_arg(extra_json)
    chat_properties = _json_arg(chat_properties_json)
    def _fn(client, account, config):
        result = creatives_mod.create_story_composite(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            preview_creative_id=preview_creative_id,
            creative_ids=_csv_list(creative_ids) or [],
            profile_id=profile_id,
            chat_properties=chat_properties,
            shareable=shareable,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.story_composite.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-lens")
@_shared_creative_opts
@click.pass_context
def creative_lens(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_lens(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.lens.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-lens-web-view")
@_shared_creative_opts
@click.option("--url", required=True)
@click.pass_context
def creative_lens_web_view(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    url: str,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_lens_web_view(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, url=url, shareable=shareable,
            profile_id=profile_id, call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.lens_web_view.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-lens-app-install")
@_shared_creative_opts
@click.option("--app-name", required=True)
@click.option("--icon-media-id", required=True)
@click.option("--ios-app-id", default=None)
@click.option("--android-app-url", default=None)
@click.pass_context
def creative_lens_app_install(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None,
    android_app_url: str | None,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_lens_app_install(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id,
            app_name=app_name, icon_media_id=icon_media_id,
            ios_app_id=ios_app_id, android_app_url=android_app_url,
            shareable=shareable, profile_id=profile_id,
            call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.lens_app_install.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


@creative.command("create-lens-deep-link")
@_shared_creative_opts
@click.option("--deep-link-uri", required=True)
@click.option("--app-name", required=True)
@click.option("--icon-media-id", required=True)
@click.option("--ios-app-id", default=None)
@click.option("--android-app-url", default=None)
@click.option("--fallback-type", default="WEB_VIEW")
@click.option("--fallback-url", default=None)
@click.pass_context
def creative_lens_deep_link(
    ctx: click.Context,
    name: str,
    headline: str,
    brand_name: str,
    top_snap_media_id: str,
    shareable: bool,
    profile_id: str | None,
    call_to_action: str | None,
    cta_color_display_mode: str | None,
    chat_properties_json: str | None,
    extra_json: str | None,
    execute: bool,
    deep_link_uri: str,
    app_name: str,
    icon_media_id: str,
    ios_app_id: str | None,
    android_app_url: str | None,
    fallback_type: str,
    fallback_url: str | None,
) -> None:
    extra = _json_arg(extra_json)
    def _fn(client, account, config):
        result = creatives_mod.create_lens_deep_link(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, headline=headline, brand_name=brand_name,
            top_snap_media_id=top_snap_media_id, deep_link_uri=deep_link_uri,
            app_name=app_name, icon_media_id=icon_media_id,
            ios_app_id=ios_app_id, android_app_url=android_app_url,
            fallback_type=fallback_type, fallback_url=fallback_url,
            shareable=shareable, profile_id=profile_id,
            call_to_action=call_to_action,
            cta_color_display_mode=cta_color_display_mode,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("creative.lens_deep_link.create", ctx.obj["account"], {"name": name}, "ok")
        return result
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# segment lookalike + delete extras
# ---------------------------------------------------------------------------


@segment.command("lookalike-create")
@click.option("--name", required=True)
@click.option("--seed-segment-id", required=True)
@click.option("--countries", required=True, help="Comma-separated ISO codes (e.g. US,CA)")
@click.option("--type", "lookalike_type", default="BALANCE",
              type=click.Choice(["BALANCE", "SIMILARITY", "REACH"]))
@click.option("--retention-days", type=int, default=180)
@click.option("--description", default=None)
@click.option("--extra-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_lookalike_create(
    ctx: click.Context,
    name: str,
    seed_segment_id: str,
    countries: str,
    lookalike_type: str,
    retention_days: int,
    description: str | None,
    extra_json: str | None,
    execute: bool,
) -> None:
    countries_list = [c.strip() for c in countries.split(",") if c.strip()]
    extra = json.loads(extra_json) if extra_json else None

    def _fn(client, account, config):
        result = segments_mod.create_lookalike_segment(
            client, account.ad_account_id, ctx.obj["account"],
            name=name, seed_segment_id=seed_segment_id,
            countries=countries_list, lookalike_type=lookalike_type,
            retention_in_days=retention_days, description=description,
            extra=extra, execute=execute,
        )
        if execute:
            audit_log("segment.lookalike.create", ctx.obj["account"], {"name": name, "seed": seed_segment_id}, "ok")
        return result

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# creative-element / interaction-zone deletes
# ---------------------------------------------------------------------------


@creative_element.command("delete")
@click.argument("element_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def ce_delete(ctx: click.Context, element_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = creative_elements_mod.delete_creative_element(
            client, element_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("creative_element.delete", ctx.obj["account"], {"id": element_id}, "ok")
        return result
    _run(ctx, _fn)


@interaction_zone.command("delete")
@click.argument("zone_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def iz_delete(ctx: click.Context, zone_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = creative_elements_mod.delete_interaction_zone(
            client, zone_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("interaction_zone.delete", ctx.obj["account"], {"id": zone_id}, "ok")
        return result
    _run(ctx, _fn)


@mobileapp_custom_conv.command("delete")
@click.argument("conversion_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def mcc_delete(ctx: click.Context, conversion_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = custom_conv_mod.delete_mobile_app_custom_conversion(
            client, conversion_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("mobile_app.custom_conversion.delete", ctx.obj["account"], {"id": conversion_id}, "ok")
        return result
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# org invite / revoke
# ---------------------------------------------------------------------------


@org.command("invite-member")
@click.option("--email", required=True)
@click.option("--display-name", default=None)
@click.option("--member-role", default=None)
@click.option("--organization-id", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def org_invite_member(
    ctx: click.Context,
    email: str,
    display_name: str | None,
    member_role: str | None,
    organization_id: str | None,
    execute: bool,
) -> None:
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide --organization-id or set organization_id in accounts.toml"}}
        result = orgs_mod.invite_member(
            client, oid, ctx.obj["account"],
            email=email, display_name=display_name,
            member_role=member_role, execute=execute,
        )
        if execute:
            audit_log("org.member.invite", ctx.obj["account"], {"email": email, "organization_id": oid}, "ok")
        return result
    _run(ctx, _fn)


@org.command("revoke-member")
@click.argument("member_id")
@click.option("--organization-id", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def org_revoke_member(
    ctx: click.Context,
    member_id: str,
    organization_id: str | None,
    execute: bool,
) -> None:
    def _fn(client, account, config):
        oid = organization_id or account.organization_id
        if not oid:
            return {"error": {"message": "Provide --organization-id or set organization_id in accounts.toml"}}
        result = orgs_mod.revoke_member(
            client, oid, member_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("org.member.revoke", ctx.obj["account"], {"member_id": member_id, "organization_id": oid}, "ok")
        return result
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# catalog product-feed CRUD
# ---------------------------------------------------------------------------


@catalog.command("feeds")
@click.argument("catalog_id")
@click.pass_context
def catalog_feeds_list(ctx: click.Context, catalog_id: str) -> None:
    _run(ctx, lambda c, a, cfg: catalog_mod.list_product_feeds(c, catalog_id))


@catalog.command("feed-get")
@click.argument("feed_id")
@click.pass_context
def catalog_feed_get(ctx: click.Context, feed_id: str) -> None:
    _run(ctx, lambda c, a, cfg: catalog_mod.get_product_feed(c, feed_id))


@catalog.command("create-feed")
@click.argument("catalog_id")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def catalog_create_feed(
    ctx: click.Context, catalog_id: str, payload_json: str, execute: bool
) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        result = catalog_mod.create_product_feed(
            client, catalog_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("catalog.feed.create", ctx.obj["account"], {"catalog_id": catalog_id}, "ok")
        return result
    _run(ctx, _fn)


@catalog.command("delete-feed")
@click.argument("feed_id")
@click.option("--execute", is_flag=True)
@click.pass_context
def catalog_delete_feed(ctx: click.Context, feed_id: str, execute: bool) -> None:
    def _fn(client, account, config):
        result = catalog_mod.delete_product_feed(
            client, feed_id, ctx.obj["account"], execute=execute
        )
        if execute:
            audit_log("catalog.feed.delete", ctx.obj["account"], {"id": feed_id}, "ok")
        return result
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# brand-lift / conversion-lift studies
# ---------------------------------------------------------------------------


@cli.group("study")
def study_group() -> None:
    """Brand-lift + conversion-lift studies (allowlist required)."""


@study_group.command("list")
@click.pass_context
def study_list(ctx: click.Context) -> None:
    def _fn(client, account, config):
        return brand_lift_mod.list_studies(client, account.ad_account_id)
    _run(ctx, _fn)


@study_group.command("get")
@click.argument("study_id")
@click.pass_context
def study_get(ctx: click.Context, study_id: str) -> None:
    _run(ctx, lambda c, a, cfg: brand_lift_mod.get_study(c, study_id))


@study_group.command("create")
@click.option("--payload-json", required=True)
@click.option("--execute", is_flag=True)
@click.pass_context
def study_create(ctx: click.Context, payload_json: str, execute: bool) -> None:
    payload = json.loads(payload_json)
    def _fn(client, account, config):
        result = brand_lift_mod.create_study(
            client, account.ad_account_id, ctx.obj["account"], payload=payload, execute=execute
        )
        if execute:
            audit_log("study.create", ctx.obj["account"], {"name": payload.get("name")}, "ok")
        return result
    _run(ctx, _fn)


@study_group.command("results")
@click.argument("study_id")
@click.pass_context
def study_results_cmd(ctx: click.Context, study_id: str) -> None:
    _run(ctx, lambda c, a, cfg: brand_lift_mod.study_results(c, study_id))


@study_group.command("conversion-lift-list")
@click.pass_context
def study_clift_list(ctx: click.Context) -> None:
    def _fn(client, account, config):
        return brand_lift_mod.list_conversion_lift_studies(client, account.ad_account_id)
    _run(ctx, _fn)


@study_group.command("conversion-lift-get")
@click.argument("study_id")
@click.pass_context
def study_clift_get(ctx: click.Context, study_id: str) -> None:
    _run(ctx, lambda c, a, cfg: brand_lift_mod.get_conversion_lift_study(c, study_id))


# ---------------------------------------------------------------------------
# targeting insights breakdown override
# ---------------------------------------------------------------------------


@targeting.command("insights-breakdown")
@click.option("--spec-json", required=True)
@click.option(
    "--breakdown",
    required=True,
    type=click.Choice([
        "GENDER", "AGE", "AGE_GENDER", "COUNTRY", "REGION", "DMA",
        "OS", "PLATFORM", "PLACEMENT", "HOUR_OF_DAY", "DAY_OF_WEEK",
    ]),
)
@click.pass_context
def targeting_insights_breakdown(ctx: click.Context, spec_json: str, breakdown: str) -> None:
    spec = json.loads(spec_json)
    def _fn(client, account, config):
        return targeting_mod.targeting_insights(
            client, account.ad_account_id, spec=spec, breakdown=breakdown
        )
    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# pixel events alias (sends via CAPI)
# ---------------------------------------------------------------------------


@pixel.command("send-events")
@click.argument("pixel_id", required=False)
@click.option("--events-file", required=True, type=click.Path())
@click.option("--no-hash", is_flag=True)
@click.option("--test-event-code", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def pixel_send_events(
    ctx: click.Context,
    pixel_id: str | None,
    events_file: str,
    no_hash: bool,
    test_event_code: str | None,
    execute: bool,
) -> None:
    """Alias for `capi send` scoped to a pixel."""
    config: AppConfig = ctx.obj["config"]
    account_key: str = ctx.obj["account"]
    try:
        account = get_account(config, account_key)
    except ValueError as e:
        _bail(str(e))
        return
    pid = pixel_id or account.pixel_id
    if not pid:
        _bail("Provide PIXEL_ID or set pixel_id in accounts.toml")
        return
    token = account.load_token()
    if not token:
        _bail(f"No access token for '{account_key}'. Run `auth login`.")
        return
    events = list(capi_mod.load_events_from_file(events_file))
    if not events:
        _bail("No events parsed from file.")
        return
    if not execute:
        emit(
            capi_mod.send_events_preview(
                pixel_id=pid, snap_app_id=None, events=events,
                auto_hash=not no_hash, account_label=account_key,
                test_event_code=test_event_code,
            ),
            ctx.obj["human"],
        )
        return
    result = capi_mod.send_events(
        access_token=token, pixel_id=pid, snap_app_id=None,
        events=events, test_event_code=test_event_code, auto_hash=not no_hash,
    )
    audit_log("pixel.send_events", account_key, {"pixel_id": pid, "count": len(events)}, "ok")
    emit(result, ctx.obj["human"])


# ---------------------------------------------------------------------------
# Bulk create / update commands (one per CRUD module)
# ---------------------------------------------------------------------------


def _bulk_cmd_runner(
    ctx: click.Context, file_path: str, fn_name: str, mod, **extra
):
    """Shared wrapper for bulk-create/bulk-update CLI commands."""
    items = _bulk.load_json_array(file_path)
    if not items:
        _bail(f"No items loaded from {file_path}")
        return

    execute = extra.pop("execute", False)
    chunk_size = extra.pop("chunk_size", None)

    def _fn(client, account, config):
        kwargs = {"items": items, "execute": execute, **extra}
        if chunk_size is not None:
            kwargs["chunk_size"] = chunk_size
        result = getattr(mod, fn_name)(
            client, account.ad_account_id, ctx.obj["account"], **kwargs
        )
        if execute:
            audit_log(
                f"{mod.__name__.split('.')[-1]}.{fn_name}",
                ctx.obj["account"],
                {"file": file_path, "count": len(items)},
                "ok",
            )
        return result

    _run(ctx, _fn)


@campaign.command("bulk-create")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_bulk_create(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_create_campaigns", campaigns_mod,
                     chunk_size=chunk_size, execute=execute)


@campaign.command("bulk-update")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_bulk_update(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_update_campaigns", campaigns_mod,
                     chunk_size=chunk_size, execute=execute)


@adsquad.command("bulk-create")
@click.argument("campaign_id")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_bulk_create(
    ctx: click.Context, campaign_id: str, file_path: str, chunk_size: int, execute: bool
) -> None:
    items = _bulk.load_json_array(file_path)
    if not items:
        _bail(f"No items loaded from {file_path}")
        return

    def _fn(client, account, config):
        result = adsquads_mod.bulk_create_ad_squads(
            client, campaign_id, ctx.obj["account"],
            items=items, chunk_size=chunk_size, execute=execute,
        )
        if execute:
            audit_log("adsquad.bulk_create", ctx.obj["account"],
                      {"campaign_id": campaign_id, "count": len(items)}, "ok")
        return result

    _run(ctx, _fn)


@adsquad.command("bulk-update")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_bulk_update(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_update_ad_squads", adsquads_mod,
                     chunk_size=chunk_size, execute=execute)


@ad.command("bulk-create")
@click.argument("ad_squad_id")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_bulk_create(
    ctx: click.Context, ad_squad_id: str, file_path: str, chunk_size: int, execute: bool
) -> None:
    items = _bulk.load_json_array(file_path)
    if not items:
        _bail(f"No items loaded from {file_path}")
        return

    def _fn(client, account, config):
        result = ads_mod.bulk_create_ads(
            client, ad_squad_id, ctx.obj["account"],
            items=items, chunk_size=chunk_size, execute=execute,
        )
        if execute:
            audit_log("ad.bulk_create", ctx.obj["account"],
                      {"ad_squad_id": ad_squad_id, "count": len(items)}, "ok")
        return result

    _run(ctx, _fn)


@ad.command("bulk-update")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=100)
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_bulk_update(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_update_ads", ads_mod,
                     chunk_size=chunk_size, execute=execute)


@creative.command("bulk-create")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def creative_bulk_create(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_create_creatives", creatives_mod,
                     chunk_size=chunk_size, execute=execute)


@creative.command("bulk-update")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def creative_bulk_update(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_update_creatives", creatives_mod,
                     chunk_size=chunk_size, execute=execute)


@segment.command("bulk-create")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_bulk_create(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_create_segments", segments_mod,
                     chunk_size=chunk_size, execute=execute)


@segment.command("bulk-update")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def segment_bulk_update(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_update_segments", segments_mod,
                     chunk_size=chunk_size, execute=execute)


@pixel.command("bulk-create")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def pixel_bulk_create(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_create_pixels", pixels_mod,
                     chunk_size=chunk_size, execute=execute)


@pixel.command("bulk-update")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def pixel_bulk_update(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_update_pixels", pixels_mod,
                     chunk_size=chunk_size, execute=execute)


@creative_element.command("bulk-create")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def ce_bulk_create(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_create_creative_elements", creative_elements_mod,
                     chunk_size=chunk_size, execute=execute)


@interaction_zone.command("bulk-create")
@click.option("--file", "file_path", required=True, type=click.Path())
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def iz_bulk_create(ctx: click.Context, file_path: str, chunk_size: int, execute: bool) -> None:
    _bulk_cmd_runner(ctx, file_path, "bulk_create_interaction_zones", creative_elements_mod,
                     chunk_size=chunk_size, execute=execute)


# ---------------------------------------------------------------------------
# manage extensions: bulk-archive + bulk-budget + generic bulk-update
# ---------------------------------------------------------------------------


@manage.command("bulk-archive")
@click.argument("ad_ids", nargs=-1, required=False)
@click.option("--file", "ids_file", default=None, type=click.Path())
@click.option("--execute", is_flag=True)
@click.pass_context
def manage_bulk_archive(
    ctx: click.Context, ad_ids: tuple[str, ...], ids_file: str | None, execute: bool
) -> None:
    """Archive (delete-equivalent) many ads via PUT status=ARCHIVED."""
    ids = _bulk.load_ids(positional=ad_ids, file_path=ids_file)
    if not ids:
        _bail("Provide AD_IDS positional args or --file")
        return

    def _fn(client, account, config):
        result = ads_mod.bulk_set_status(
            client, account.ad_account_id, ctx.obj["account"],
            ad_ids=ids, status="ARCHIVED", execute=execute,
        )
        if execute:
            audit_log("ad.bulk_archive", ctx.obj["account"], {"count": len(ids)}, "ok")
        return result
    _run(ctx, _fn)


@manage.command("bulk-budget")
@click.option(
    "--entity",
    "entity_type",
    required=True,
    type=click.Choice(["campaign", "ad_squad"]),
)
@click.option(
    "--file",
    "file_path",
    required=True,
    type=click.Path(),
    help='JSON array of {"id": "...", "daily_budget": "<dollars|micro:N>" } items',
)
@click.option("--chunk-size", type=int, default=10)
@click.option("--execute", is_flag=True)
@click.pass_context
def manage_bulk_budget(
    ctx: click.Context,
    entity_type: str,
    file_path: str,
    chunk_size: int,
    execute: bool,
) -> None:
    """Update daily_budget on many campaigns or ad squads at once."""
    raw = _bulk.load_json_array(file_path)
    items: list[dict[str, Any]] = []
    for r in raw:
        if not isinstance(r, dict) or not r.get("id"):
            _bail("Every row must be an object with an 'id' field")
            return
        out = {"id": r["id"]}
        if "daily_budget" in r:
            out["daily_budget_micro"] = to_micro(r["daily_budget"])
        if "daily_budget_micro" in r:
            out["daily_budget_micro"] = int(r["daily_budget_micro"])
        if "lifetime_spend_cap" in r:
            out["lifetime_spend_cap_micro"] = to_micro(r["lifetime_spend_cap"])
        if "lifetime_budget" in r:
            out["lifetime_budget_micro"] = to_micro(r["lifetime_budget"])
        items.append(out)

    def _fn(client, account, config):
        if entity_type == "campaign":
            result = campaigns_mod.bulk_update_campaigns(
                client, account.ad_account_id, ctx.obj["account"],
                items=items, chunk_size=chunk_size, execute=execute,
            )
            tag = "campaign.bulk_budget"
        else:
            result = adsquads_mod.bulk_update_ad_squads(
                client, account.ad_account_id, ctx.obj["account"],
                items=items, chunk_size=chunk_size, execute=execute,
            )
            tag = "adsquad.bulk_budget"
        if execute:
            audit_log(tag, ctx.obj["account"], {"count": len(items)}, "ok")
        return result

    _run(ctx, _fn)


# ---------------------------------------------------------------------------
# duplicate convenience commands (read-then-create)
# ---------------------------------------------------------------------------


@campaign.command("duplicate")
@click.argument("source_campaign_id")
@click.option("--new-name", default=None)
@click.option("--status", default="PAUSED", type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--overrides-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def campaign_duplicate(
    ctx: click.Context,
    source_campaign_id: str,
    new_name: str | None,
    status: str,
    overrides_json: str | None,
    execute: bool,
) -> None:
    overrides = json.loads(overrides_json) if overrides_json else None

    def _fn(client, account, config):
        result = campaigns_mod.duplicate_campaign(
            client, account.ad_account_id, source_campaign_id, ctx.obj["account"],
            new_name=new_name, status=status, overrides=overrides, execute=execute,
        )
        if execute:
            audit_log("campaign.duplicate", ctx.obj["account"],
                      {"source": source_campaign_id, "name": new_name}, "ok")
        return result
    _run(ctx, _fn)


@adsquad.command("duplicate")
@click.argument("source_ad_squad_id")
@click.option("--target-campaign-id", default=None)
@click.option("--new-name", default=None)
@click.option("--status", default="PAUSED", type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--overrides-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def adsquad_duplicate(
    ctx: click.Context,
    source_ad_squad_id: str,
    target_campaign_id: str | None,
    new_name: str | None,
    status: str,
    overrides_json: str | None,
    execute: bool,
) -> None:
    overrides = json.loads(overrides_json) if overrides_json else None

    def _fn(client, account, config):
        result = adsquads_mod.duplicate_ad_squad(
            client, source_ad_squad_id, ctx.obj["account"],
            target_campaign_id=target_campaign_id,
            new_name=new_name, status=status, overrides=overrides, execute=execute,
        )
        if execute:
            audit_log("adsquad.duplicate", ctx.obj["account"],
                      {"source": source_ad_squad_id, "target_campaign": target_campaign_id}, "ok")
        return result
    _run(ctx, _fn)


@ad.command("duplicate")
@click.argument("source_ad_id")
@click.option("--target-ad-squad-id", default=None)
@click.option("--new-name", default=None)
@click.option("--status", default="PAUSED", type=click.Choice(["ACTIVE", "PAUSED"]))
@click.option("--overrides-json", default=None)
@click.option("--execute", is_flag=True)
@click.pass_context
def ad_duplicate(
    ctx: click.Context,
    source_ad_id: str,
    target_ad_squad_id: str | None,
    new_name: str | None,
    status: str,
    overrides_json: str | None,
    execute: bool,
) -> None:
    overrides = json.loads(overrides_json) if overrides_json else None

    def _fn(client, account, config):
        result = ads_mod.duplicate_ad(
            client, source_ad_id, ctx.obj["account"],
            target_ad_squad_id=target_ad_squad_id,
            new_name=new_name, status=status, overrides=overrides, execute=execute,
        )
        if execute:
            audit_log("ad.duplicate", ctx.obj["account"],
                      {"source": source_ad_id, "target_squad": target_ad_squad_id}, "ok")
        return result
    _run(ctx, _fn)


if __name__ == "__main__":
    cli()
