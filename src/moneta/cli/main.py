from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from moneta.cli.client import request

app = typer.Typer(no_args_is_help=True, help="moneta — personal finance, one honest number.")
import_app = typer.Typer(no_args_is_help=True)
setup_app = typer.Typer(no_args_is_help=True)
app.add_typer(import_app, name="import", help="Import external data files.")
app.add_typer(setup_app, name="setup", help="Connect data sources.")
console = Console()


def _parse_iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        console.print(f"[red]Error:[/red] invalid date {value!r} (expected YYYY-MM-DD)")
        raise typer.Exit(1) from None


@app.command()
def sync(
    full: Annotated[
        bool,
        typer.Option(
            "--full", help="Re-pull all available history (e.g. after linking a new account)."
        ),
    ] = False,
) -> None:
    """Pull latest data and run all pipelines."""
    report = request("POST", "/sync", params={"full": True} if full else None)
    console.print(
        f"Synced: [bold]{report['ingest']['new_transactions']}[/bold] new transactions, "
        f"{report['transfers']['linked']} transfers linked, "
        f"{report['recurring']['new_series']} new series, "
        f"{report['events']} events."
    )
    if report["auto_resolved"]:
        console.print(f"LLM auto-resolved {report['auto_resolved']} review item(s).")
    verify = report["verify"]
    if verify["verified"] or verify["flagged"]:
        console.print(
            f"LLM verified {verify['verified']} series; flagged {verify['flagged']} for review."
        )
    open_reviews = request("GET", "/review")
    if open_reviews:
        console.print(f"[yellow]{len(open_reviews)} items need review:[/yellow] moneta review")


@app.command()
def power() -> None:
    """Monthly spending power: income - fixed costs."""
    r = request("GET", "/power")
    table = Table(title=f"Spending power — {r['month']}", show_header=False)
    table.add_row("Income (detected)", f"${r['monthly_income']}/mo")
    for line in r["income_sources"]:
        table.add_row(
            f"  {escape(line['merchant'])} ({line['cadence']})", f"${line['monthly_amount']}"
        )
    table.add_row("Fixed costs", f"-${r['total_fixed']}/mo")
    for line in r["fixed_costs"]:
        table.add_row(
            f"  {escape(line['merchant'])} ({line['cadence']})", f"${line['monthly_amount']}"
        )
    table.add_row("[bold]Spending power[/bold]", f"[bold]${r['spending_power']}/mo[/bold]")
    table.add_row("Spent so far", f"-${r['spent_so_far']}")
    table.add_row("[bold]Remaining[/bold]", f"[bold]${r['remaining']}[/bold]")
    console.print(table)


@app.command()
def networth() -> None:
    """Net worth (vested only) with unvested shown separately."""
    r = request("GET", "/networth")
    table = Table(title="Net worth", show_header=False)
    table.add_row("Liquid", f"${r['liquid']}")
    table.add_row("Vested holdings", f"${r['vested_holdings']}")
    table.add_row("Liabilities", f"-${r['liabilities']}")
    table.add_row("[bold]Net worth[/bold]", f"[bold]${r['net_worth']}[/bold]")
    table.add_row("Unvested (potential)", f"${r['unvested_potential']}")
    console.print(table)
    if r["unknown_accounts"]:
        console.print(
            f"[yellow]{r['unknown_accounts']} account(s) have unknown type and are "
            f"excluded — fix with: moneta accounts --set-type ID TYPE[/yellow]"
        )


@app.command()
def recurring(
    events: Annotated[bool, typer.Option("--events")] = False,
    end: Annotated[int | None, typer.Option("--end")] = None,
) -> None:
    """List detected recurring series (or recent events with --events); --end ID to cancel one."""
    if end is not None:
        request("PATCH", f"/recurring/{end}", {"status": "ended"})
        console.print(f"[green]Series {end} ended.[/green]")
    if events:
        rows = request("GET", "/recurring/events")
        table = Table("When", "ID", "Merchant", "Event", "Details")
        for e in rows:
            table.add_row(
                e["occurred_on"],
                str(e["series_id"]),
                escape(e["merchant"]),
                e["kind"],
                escape(str(e["details"])),
            )
    else:
        rows = request("GET", "/recurring")
        table = Table("ID", "Merchant", "Direction", "Cadence", "Expected", "Next", "Status")
        for s in rows:
            table.add_row(
                str(s["id"]),
                escape(s["merchant"]),
                s["direction"],
                s["cadence"],
                f"${s['expected_amount']}",
                s["next_expected_on"],
                s["status"],
            )
    console.print(table)


@app.command()
def cashflow(
    start: Annotated[
        str | None, typer.Option("--start", help="YYYY-MM-DD (default: month start).")
    ] = None,
    end: Annotated[str | None, typer.Option("--end", help="YYYY-MM-DD (default: today).")] = None,
) -> None:
    """Accrual spend vs cash out for a date range (defaults to this month)."""
    params = {
        name: _parse_iso_date(value)
        for name, value in (("start", start), ("end", end))
        if value is not None
    }
    r = request("GET", "/cashflow", params=params or None)
    table = Table(title=f"Cashflow — {r['start']} to {r['end']}", show_header=False)
    table.add_row("Accrual spend", f"${r['accrual']}")
    table.add_row("Cash out", f"${r['cash_out']}")
    console.print(table)


@app.command()
def obligations() -> None:
    """Loans/financing: monthly payment, balance, months left, promo warnings."""
    rows = request("GET", "/obligations")
    table = Table("Account", "Balance", "Payment/mo", "Months left", "Payoff", "Promo ends")
    for ob in rows:
        payoff = ob["payoff_estimate"] or "?"
        warn = " [red]![/red]" if ob["deferred_interest_risk"] else ""
        table.add_row(
            escape(ob["account_name"]),
            f"${ob['balance_owed']}",
            f"${ob['monthly_payment']}" if ob["monthly_payment"] else "?",
            str(ob["months_left"] or "?"),
            f"{payoff}{warn}",
            str(ob["promo_expires_on"] or "—"),
        )
    console.print(table)
    if any(ob["deferred_interest_risk"] for ob in rows):
        console.print("[red]! payoff lands after the promo expires — deferred interest risk[/red]")


@app.command()
def accounts(
    set_type: Annotated[tuple[int, str] | None, typer.Option("--set-type")] = None,
    set_promo: Annotated[tuple[int, str] | None, typer.Option("--set-promo")] = None,
) -> None:
    """List accounts; --set-type ID TYPE, --set-promo ID YYYY-MM-DD."""
    if set_type:
        request("PATCH", f"/accounts/{set_type[0]}", {"type": set_type[1]})
    if set_promo:
        promo = _parse_iso_date(set_promo[1])
        request("PATCH", f"/accounts/{set_promo[0]}", {"promo_expires_on": promo})
    rows = request("GET", "/accounts")
    table = Table("ID", "Name", "Org", "Type", "Balance", "Promo ends")
    for a in rows:
        table.add_row(
            str(a["id"]),
            escape(a["name"]),
            escape(a["org_name"]),
            a["type"],
            f"${a['balance']}",
            str(a["promo_expires_on"] or "—"),
        )
    console.print(table)


_REVIEW_KINDS = {
    "recurring_cluster": (
        "recurring bill question",
        "your answers set the fixed costs and income behind `moneta power`",
    ),
    "transfer_pair": (
        "transfer match",
        "matching keeps card/loan payments out of your spending totals",
    ),
    "merchant": (
        "merchant name",
        "names a messy bank descriptor so it reads cleanly everywhere",
    ),
    "price_change": (
        "price change",
        "confirming updates the expected amount behind `moneta power`",
    ),
}


def _prompt_yes_no(question: str) -> bool | None:
    answer = typer.prompt(question, default="", show_default=False)
    if not answer:
        return None
    normalized = answer.strip().lower()
    if normalized in ("y", "yes"):
        return True
    if normalized in ("n", "no"):
        return False
    console.print("[red]invalid input, skipping[/red]")
    return None


def _review_one(item: dict[str, object]) -> dict[str, object] | None:
    """Prompt for one item; return the resolution, or None to skip."""
    ctx = item.get("context") or {}
    assert isinstance(ctx, dict)
    if item["kind"] == "recurring_cluster":
        for s in ctx.get("samples", []):
            console.print(f"    {s['posted_on']}  ${s['amount']}")
        if ctx.get("direction") == "inflow":
            console.print("    [dim](these are deposits — answering y counts them as income)[/dim]")
        answer = _prompt_yes_no("Recurring? [y/n]")
        return None if answer is None else {"is_recurring": answer}
    if item["kind"] == "price_change":
        for s in ctx.get("samples", []):
            console.print(f"    {s['posted_on']}  ${s['amount']}")
        console.print(
            f"    ${ctx.get('old_amount')} → ${ctx.get('new_amount')} on {ctx.get('occurred_on')}"
        )
        answer = _prompt_yes_no("Price change? [y/n]")
        return None if answer is None else {"is_price_change": answer}
    if item["kind"] == "transfer_pair":
        if outflow := ctx.get("outflow"):
            console.print(
                f"    out: ${outflow['amount']} on {outflow['posted_on']} "
                f"from {outflow['account']} — {outflow['description']!r}"
            )
        candidates = ctx.get("candidates") or []
        if candidates:
            for n, c in enumerate(candidates, 1):
                console.print(
                    f"    {n}. ${c['amount']} on {c['posted_on']} "
                    f"into {c['account']} — {c['description']!r}"
                )
            answer = typer.prompt(
                "Match number (Enter to skip, 0 = none of these)", default="", show_default=False
            )
        else:
            answer = typer.prompt(
                "Matching inflow id (Enter to skip)", default="", show_default=False
            )
        if not answer:
            return None
        try:
            pick = int(answer)
        except ValueError:
            console.print("[red]invalid input, skipping[/red]")
            return None
        if candidates:
            if pick == 0:
                return {"inflow_id": None}
            if not 1 <= pick <= len(candidates):
                console.print("[red]invalid input, skipping[/red]")
                return None
            chosen = candidates[pick - 1]
            assert isinstance(chosen, dict)
            return {"inflow_id": chosen["id"]}
        return {"inflow_id": pick}
    if item["kind"] == "merchant":
        if suggested := ctx.get("suggested"):
            console.print(f"    current guess: {suggested!r}")
        answer = typer.prompt("Merchant name (Enter to skip)", default="", show_default=False)
        return {"merchant": answer} if answer else None
    answer = typer.prompt("Answer (blank to skip)", default="", show_default=False)
    return {"note": answer} if answer else None


@app.command()
def review() -> None:
    """Resolve ambiguous classifications interactively."""
    items = request("GET", "/review")
    if not items:
        console.print("Nothing to review.")
        return
    counts: dict[str, int] = {}
    for item in items:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    console.print(f"[bold]Review queue — {len(items)} item(s)[/bold]")
    for kind, n in counts.items():
        label, why = _REVIEW_KINDS.get(kind, (kind, ""))
        console.print(f"  {n} × {label} — [dim]{why}[/dim]")
    console.print(
        "[dim]Press Enter to skip any item. Ctrl-C stops; skipped items return next time.[/dim]"
    )
    resolved = skipped = 0
    for idx, item in enumerate(items, 1):
        console.print(
            f"\n[bold cyan][{idx}/{len(items)}][/bold cyan] [bold]{item['question']}[/bold]"
        )
        resolution = _review_one(item)
        if resolution is None:
            skipped += 1
            continue
        request("POST", f"/review/{item['id']}/resolve", {"resolution": resolution})
        resolved += 1
        console.print("[green]resolved[/green]")
    console.print(f"\nResolved {resolved}, skipped {skipped}.")
    if resolved:
        # price/not-recurring answers apply immediately; new bills and transfer
        # links shape detection on the next sync
        console.print("Some answers take effect on the next sync: [bold]moneta sync[/bold]")


@import_app.command("vesting")
def import_vesting(file: Path) -> None:
    """Import vesting CSV (symbol,vested_quantity,unvested_quantity)."""
    result = request("POST", "/import/vesting", {"csv": file.read_text()})
    console.print(f"Updated {result['updated']} holding(s).")


@setup_app.command("simplefin")
def setup_simplefin(token: str) -> None:
    """Claim a SimpleFIN setup token and save the access URL."""
    import asyncio

    from moneta.aggregator.simplefin import claim_setup_token
    from moneta.config import save_config_value

    access_url = asyncio.run(claim_setup_token(token))
    save_config_value("simplefin_access_url", access_url)
    console.print("[green]SimpleFIN connected.[/green] Run: moneta sync")


@app.command()
def renormalize() -> None:
    """Re-apply improved merchant-naming rules to already-synced transactions."""
    result = request("POST", "/normalize/rerun")
    console.print(f"Updated {result['changed']} merchant name(s).")
    if result["changed"]:
        console.print("Re-run detection to pick up merged groups: [bold]moneta sync[/bold]")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8300) -> None:
    """Run the moneta API server."""
    import uvicorn

    uvicorn.run("moneta.api:build_app", host=host, port=port, factory=True)
