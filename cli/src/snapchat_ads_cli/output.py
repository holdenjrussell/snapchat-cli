"""Output formatting: JSON (default), --human tables, CSV writer for reports.

Hand-rolled column formatting -- no rich/tabulate dependency, matches
meta-ads-cli.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from typing import Any, Iterable

import click

from .safety import format_micro


def emit(data: dict[str, Any], human: bool = False) -> None:
    """Print data as JSON (default) or human-readable format.

    On {"error": ...} payloads, writes to stderr and exits 1.
    On {"status": "preview", ...} payloads, prints normally with a banner.
    """
    if isinstance(data, dict) and "error" in data:
        click.echo(json.dumps(data, indent=2), err=True)
        sys.exit(1)

    if not human:
        click.echo(json.dumps(data, indent=2, default=str))
        return

    _human_format(data)


def fmt_money(v: float | int | None) -> str:
    if v is None:
        return "-"
    return f"${float(v):,.2f}"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{float(v):.2f}%"


def fmt_int(v: int | float | None) -> str:
    if v is None:
        return "-"
    return f"{int(v):,}"


def fmt_micro(v: int | str | None) -> str:
    return format_micro(v)


def col(text: Any, width: int, align: str = "<") -> str:
    """Truncate + pad a column value (no Unicode ellipsis -- ASCII '...')."""
    s = str(text) if text is not None else "-"
    if len(s) > width:
        s = s[: max(0, width - 3)] + "..."
    return format(s, f"{align}{width}")


def write_csv(
    rows: Iterable[dict[str, Any]],
    columns: list[str] | None = None,
    out_path: str | None = None,
) -> str | None:
    """Write rows as CSV. If out_path is None, returns the CSV string.

    `columns` controls column order. If None, columns are inferred from the
    first row's keys (stable insertion order on Python 3.7+).
    """
    rows_list = list(rows)
    if not rows_list:
        return "" if out_path is None else None

    if columns is None:
        columns = list(rows_list[0].keys())

    if out_path:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows_list)
        return None

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows_list)
    return buf.getvalue()


def _human_format(data: dict[str, Any]) -> None:  # noqa: C901
    """Human-readable output dispatched by response shape."""

    if data.get("status") == "preview":
        click.echo(f"PREVIEW: {data.get('action', '')}  ({data.get('account', '')})")
        click.echo(f"  {data.get('message', '')}")
        if data.get("details"):
            click.echo(f"  Details: {data['details']}")
        if data.get("current"):
            click.echo("\n  Current:")
            click.echo(_indent(json.dumps(data["current"], indent=2, default=str), "    "))
        if data.get("proposed"):
            click.echo("\n  Proposed:")
            click.echo(_indent(json.dumps(data["proposed"], indent=2, default=str), "    "))
        return

    if "accounts" in data and "count" in data and "campaigns" not in data:
        accounts = data["accounts"]
        click.echo(f"Configured accounts ({data['count']})\n")
        for key, acct in accounts.items():
            token_flag = "token" if acct.get("has_token") else "NO TOKEN"
            click.echo(
                f"  - {key}: {acct.get('name', '')}  "
                f"[{acct.get('ad_account_id', '') or '(no ad account)'}]  ({token_flag})"
            )
        return

    if "auth_status" in data:
        click.echo(f"Auth status: {data['auth_status']}  ({data.get('account', '')})")
        click.echo(f"  Token file:  {data.get('token_path', '-')}")
        click.echo(f"  Has access:  {'yes' if data.get('has_access_token') else 'NO'}")
        click.echo(f"  Has refresh: {'yes' if data.get('has_refresh_token') else 'NO'}")
        if data.get("expires_at"):
            click.echo(f"  Expires at:  {data['expires_at']}")
            click.echo(f"  Expires in:  {data.get('expires_in_human', '-')}")
        if data.get("scope"):
            click.echo(f"  Scope:       {data['scope']}")
        if data.get("token_tail"):
            click.echo(f"  Tail:        ...{data['token_tail']}")
        return

    if "organizations" in data:
        orgs = data["organizations"]
        click.echo(f"Organizations ({len(orgs)})\n")
        click.echo(f"  {col('ID', 36)}  {col('NAME', 32)}  {col('TYPE', 12)}")
        click.echo("  " + "-" * 84)
        for o in orgs:
            click.echo(
                f"  {col(o.get('id', '-'), 36)}  "
                f"{col(o.get('name', '-'), 32)}  "
                f"{col(o.get('type', '-'), 12)}"
            )
        return

    if "ad_accounts" in data:
        accs = data["ad_accounts"]
        click.echo(f"Ad accounts ({len(accs)})\n")
        click.echo(
            f"  {col('ID', 36)}  {col('NAME', 28)}  {col('STATUS', 10)}  {col('CURRENCY', 8)}"
        )
        click.echo("  " + "-" * 88)
        for a in accs:
            click.echo(
                f"  {col(a.get('id', '-'), 36)}  "
                f"{col(a.get('name', '-'), 28)}  "
                f"{col(a.get('status', '-'), 10)}  "
                f"{col(a.get('currency', '-'), 8)}"
            )
        return

    if "campaigns" in data:
        rows = data["campaigns"]
        click.echo(f"Campaigns ({len(rows)})\n")
        click.echo(
            f"  {col('ID', 36)}  {col('NAME', 28)}  {col('OBJECTIVE', 16)}  "
            f"{col('STATUS', 8)}  {col('DAILY $', 10, '>')}"
        )
        click.echo("  " + "-" * 110)
        for c in rows:
            daily = c.get("daily_budget_micro")
            click.echo(
                f"  {col(c.get('id', '-'), 36)}  "
                f"{col(c.get('name', '-'), 28)}  "
                f"{col(c.get('objective', '-'), 16)}  "
                f"{col(c.get('status', '-'), 8)}  "
                f"{col(fmt_micro(daily) if daily else '-', 10, '>')}"
            )
        return

    if "ad_squads" in data:
        rows = data["ad_squads"]
        click.echo(f"Ad squads ({len(rows)})\n")
        click.echo(
            f"  {col('ID', 36)}  {col('NAME', 24)}  {col('OPT GOAL', 16)}  "
            f"{col('STATUS', 8)}  {col('DAILY $', 10, '>')}"
        )
        click.echo("  " + "-" * 108)
        for s in rows:
            daily = s.get("daily_budget_micro")
            click.echo(
                f"  {col(s.get('id', '-'), 36)}  "
                f"{col(s.get('name', '-'), 24)}  "
                f"{col(s.get('optimization_goal', '-'), 16)}  "
                f"{col(s.get('status', '-'), 8)}  "
                f"{col(fmt_micro(daily) if daily else '-', 10, '>')}"
            )
        return

    if "ads" in data:
        rows = data["ads"]
        click.echo(f"Ads ({len(rows)})\n")
        click.echo(
            f"  {col('ID', 36)}  {col('NAME', 28)}  {col('TYPE', 12)}  {col('STATUS', 8)}"
        )
        click.echo("  " + "-" * 92)
        for a in rows:
            click.echo(
                f"  {col(a.get('id', '-'), 36)}  "
                f"{col(a.get('name', '-'), 28)}  "
                f"{col(a.get('type', '-'), 12)}  "
                f"{col(a.get('status', '-'), 8)}"
            )
        return

    if "creatives" in data:
        rows = data["creatives"]
        click.echo(f"Creatives ({len(rows)})\n")
        click.echo(
            f"  {col('ID', 36)}  {col('NAME', 32)}  {col('TYPE', 16)}"
        )
        click.echo("  " + "-" * 92)
        for c in rows:
            click.echo(
                f"  {col(c.get('id', '-'), 36)}  "
                f"{col(c.get('name', '-'), 32)}  "
                f"{col(c.get('type', '-'), 16)}"
            )
        return

    if "media" in data:
        rows = data["media"]
        click.echo(f"Media ({len(rows)})\n")
        click.echo(
            f"  {col('ID', 36)}  {col('NAME', 28)}  {col('TYPE', 10)}  {col('STATUS', 14)}"
        )
        click.echo("  " + "-" * 92)
        for m in rows:
            click.echo(
                f"  {col(m.get('id', '-'), 36)}  "
                f"{col(m.get('name', '-'), 28)}  "
                f"{col(m.get('type', '-'), 10)}  "
                f"{col(m.get('media_status', '-'), 14)}"
            )
        return

    if "segments" in data:
        rows = data["segments"]
        click.echo(f"Segments ({len(rows)})\n")
        click.echo(
            f"  {col('ID', 36)}  {col('NAME', 32)}  {col('SOURCE', 14)}  "
            f"{col('SIZE', 10, '>')}"
        )
        click.echo("  " + "-" * 100)
        for s in rows:
            click.echo(
                f"  {col(s.get('id', '-'), 36)}  "
                f"{col(s.get('name', '-'), 32)}  "
                f"{col(s.get('source_type', '-'), 14)}  "
                f"{col(fmt_int(s.get('approximate_number_users')), 10, '>')}"
            )
        return

    if "stats" in data:
        rows = data["stats"]
        if isinstance(rows, list) and rows:
            keys = sorted({k for r in rows for k in r.keys()})
            click.echo(f"Stats ({len(rows)} rows)\n")
            header = "  ".join(col(k, 14) for k in keys)
            click.echo("  " + header)
            click.echo("  " + "-" * (len(header)))
            for r in rows:
                line = "  ".join(col(r.get(k, "-"), 14) for k in keys)
                click.echo("  " + line)
            return

    click.echo(json.dumps(data, indent=2, default=str))


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
