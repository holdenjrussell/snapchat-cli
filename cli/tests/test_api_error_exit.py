from types import SimpleNamespace

import pytest

from snapchat_ads_cli import cli
from snapchat_ads_cli.api_client import SnapApiError


class _Client:
    def close(self):
        return None


def test_run_exits_one_when_snap_api_call_fails(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_resolve_client",
        lambda _ctx, require_account=True: (object(), object(), _Client()),
    )
    ctx = SimpleNamespace(obj={"human": False})

    def fail(*_args, **_kwargs):
        raise SnapApiError("upstream rejected request", status_code=400, error_code="bad_request")

    with pytest.raises(SystemExit) as exc:
        cli._run(ctx, fail)

    assert exc.value.code == 1


def test_resolve_client_fails_closed_on_ad_account_binding_mismatch(monkeypatch):
    configured = SimpleNamespace(
        ad_account_id="configured-account",
        load_token=lambda: "unused-token",
    )
    monkeypatch.setattr(cli, "get_account", lambda _config, _key: configured)
    ctx = SimpleNamespace(obj={
        "config": object(),
        "account": "default",
        "expected_ad_account_id": "required-account",
    })

    with pytest.raises(SystemExit) as exc:
        cli._resolve_client(ctx)

    assert exc.value.code == 2
