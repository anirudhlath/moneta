from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from moneta.cli.client import request

app = typer.Typer(no_args_is_help=True, help="moneta — personal finance, one honest number.")
import_app = typer.Typer(no_args_is_help=True)
setup_app = typer.Typer(no_args_is_help=True)
app.add_typer(import_app, name="import", help="Import external data files.")
app.add_typer(setup_app, name="setup", help="Connect data sources.")
console = Console()


@app.command()
def sync() -> None:
    """Pull latest data and run all pipelines."""
    report = request("POST", "/sync")
    console.print(
        f"Synced: [bold]{report['ingest']['new_transactions']}[/bold] new transactions, "
        f"{report['transfers']['linked']} transfers linked, "
        f"{report['recurring']['new_series']} new series, "
        f"{report['events']} events."
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
    table.add_row("Fixed costs", f"-${r['total_fixed']}/mo")
    for line in r["fixed_costs"]:
        table.add_row(f"  {line['merchant']} ({line['cadence']})", f"${line['monthly_amount']}")
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
def recurring(events: Annotated[bool, typer.Option("--events")] = False) -> None:
    """List detected recurring series (or recent events with --events)."""
    if events:
        rows = request("GET", "/recurring/events")
        table = Table("When", "Series", "Event", "Details")
        for e in rows:
            table.add_row(e["occurred_on"], str(e["series_id"]), e["kind"], str(e["details"]))
    else:
        rows = request("GET", "/recurring")
        table = Table("Merchant", "Cadence", "Expected", "Next", "Status")
        for s in rows:
            table.add_row(
                s["merchant"],
                s["cadence"],
                f"${s['expected_amount']}",
                s["next_expected_on"],
                s["status"],
            )
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
            ob["account_name"],
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
        try:
            promo = date.fromisoformat(set_promo[1]).isoformat()
        except ValueError:
            console.print(f"[red]Error:[/red] invalid date {set_promo[1]!r} (expected YYYY-MM-DD)")
            raise typer.Exit(1) from None
        request("PATCH", f"/accounts/{set_promo[0]}", {"promo_expires_on": promo})
    rows = request("GET", "/accounts")
    table = Table("ID", "Name", "Org", "Type", "Balance", "Promo ends")
    for a in rows:
        table.add_row(
            str(a["id"]),
            a["name"],
            a["org_name"],
            a["type"],
            f"${a['balance']}",
            str(a["promo_expires_on"] or "—"),
        )
    console.print(table)


@app.command()
def review() -> None:
    """Resolve ambiguous classifications interactively."""
    items = request("GET", "/review")
    if not items:
        console.print("Nothing to review.")
        return
    for item in items:
        console.print(f"\n[bold]{item['question']}[/bold]  ({item['kind']})")
        answer = typer.prompt("Answer (blank to skip)", default="", show_default=False)
        if not answer:
            continue
        if item["kind"] == "merchant":
            resolution = {"merchant": answer}
        elif item["kind"] == "transfer_pair":
            try:
                resolution = {"inflow_id": int(answer)}
            except ValueError:
                console.print("[red]invalid input, skipping[/red]")
                continue
        else:
            resolution = {"note": answer}
        request("POST", f"/review/{item['id']}/resolve", {"resolution": resolution})
        console.print("[green]resolved[/green]")


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
def serve(host: str = "127.0.0.1", port: int = 8300) -> None:
    """Run the moneta API server."""
    import uvicorn

    uvicorn.run("moneta.api:build_app", host=host, port=port, factory=True)
