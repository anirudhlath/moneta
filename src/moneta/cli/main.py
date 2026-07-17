from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from moneta.cli.client import request

if TYPE_CHECKING:
    from moneta.aggregator.plaid import PlaidClient
    from moneta.config import Settings

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


def fmt_money(cents: int) -> str:
    """Integer cents -> display dollars; negatives are -$X.YY (sign before the $)."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole}.{frac:02d}"


def fmt_outflow(magnitude_cents: int) -> str:
    """Renders an unsigned outflow/liability magnitude with its display minus."""
    return fmt_money(-magnitude_cents)


_CADENCE_PHRASE = {"weekly": "every week", "biweekly": "every 2 weeks", "annual": "every year"}


def _series_line_amount(line: dict[str, Any]) -> str:
    """Amount cell for a power-table series row (design 2026-07-16 §3): non-monthly
    cadences show the per-cycle amount alongside the monthly equivalent; monthly
    rows stay a bare amount."""
    phrase = _CADENCE_PHRASE.get(line["cadence"])
    if phrase is None:
        return fmt_money(line["monthly_cents"])
    return f"{fmt_money(line['expected_cents'])} {phrase} ≈ {fmt_money(line['monthly_cents'])}/mo"


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
    if report["ingest"].get("updated_transactions"):
        console.print(
            f"{report['ingest']['updated_transactions']} transaction(s) corrected upstream."
        )
    open_reviews = request("GET", "/review")
    if open_reviews:
        console.print(f"[yellow]{len(open_reviews)} items need review:[/yellow] moneta review")


@app.command()
def status() -> None:
    """Show the most recent sync run and its outcome."""
    r = request("GET", "/sync/last")
    if not r:
        console.print("No sync has run yet. Run: [bold]moneta sync[/bold]")
        return
    outcome = {
        "ok": "[green]ok[/green]",
        "failed": f"[red]failed[/red] — {r['error']}",
        "incomplete": "[yellow]incomplete[/yellow] — still running, or the process died mid-sync",
    }[r["status"]]
    console.print(f"Last sync: {r['started_at']} → {outcome}")
    if r["report"]:
        rep = r["report"]
        console.print(
            f"  {rep['ingest']['new_transactions']} new txns, "
            f"{rep['recurring']['new_series']} new series, {rep['events']} events"
        )


@app.command()
def power() -> None:
    """Monthly spending power: income - fixed costs."""
    r = request("GET", "/power")
    table = Table(title=f"Spending power — {r['month']}", show_header=False)
    table.add_row("Income (detected)", f"{fmt_money(r['monthly_income_cents'])}/mo")
    for line in r["income_sources"]:
        table.add_row(f"  {escape(line['merchant'])}", _series_line_amount(line))
    table.add_row("Fixed costs", f"{fmt_outflow(r['total_fixed_cents'])}/mo")
    for line in r["fixed_costs"]:
        table.add_row(f"  {escape(line['merchant'])}", _series_line_amount(line))
    table.add_row(
        "[bold]Spending power[/bold]", f"[bold]{fmt_money(r['spending_power_cents'])}/mo[/bold]"
    )
    table.add_row("Spent so far", fmt_outflow(r["spent_so_far_cents"]))
    table.add_row("[bold]Remaining[/bold]", f"[bold]{fmt_money(r['remaining_cents'])}[/bold]")
    table.add_row(f"Per day ({r['days_left']} days left)", fmt_money(r["per_day_remaining_cents"]))
    console.print(table)


@app.command()
def networth() -> None:
    """Net worth (vested only) with unvested shown separately."""
    r = request("GET", "/networth")
    table = Table(title="Net worth", show_header=False)
    table.add_row("Liquid", fmt_money(r["liquid_cents"]))
    table.add_row("Vested holdings", fmt_money(r["vested_holdings_cents"]))
    table.add_row("Liabilities", fmt_outflow(r["liabilities_cents"]))
    table.add_row("[bold]Net worth[/bold]", f"[bold]{fmt_money(r['net_worth_cents'])}[/bold]")
    table.add_row("Unvested (potential)", fmt_money(r["unvested_potential_cents"]))
    console.print(table)
    if r["unknown_accounts"]:
        console.print(
            f"[yellow]{r['unknown_accounts']} account(s) have unknown type and are "
            f"excluded — fix with: moneta accounts --set-type ID TYPE[/yellow]"
        )
    if r.get("foreign_accounts"):  # .get: tolerate an older server in remote mode
        console.print(
            f"[yellow]{r['foreign_accounts']} account(s) in a non-primary currency are "
            f"excluded from these totals[/yellow]"
        )


@app.command()
def recurring(
    events: Annotated[bool, typer.Option("--events")] = False,
    end: Annotated[int | None, typer.Option("--end", help="Cancel a series.")] = None,
    not_a_bill: Annotated[
        int | None,
        typer.Option(
            "--not-a-bill", help="Not recurring: ends the series and suppresses it forever."
        ),
    ] = None,
    habit: Annotated[
        int | None,
        typer.Option(
            "--habit", help="Discretionary habit, not a bill; reactivates the series if ended."
        ),
    ] = None,
    re_review: Annotated[
        int | None,
        typer.Option("--re-review", help="Reopen the series' bill/habit review question."),
    ] = None,
) -> None:
    """List detected recurring series (or recent events with --events).

    --end ID cancels a series. --not-a-bill / --habit / --re-review ID overrule
    detection instead; all four are mutually exclusive with each other.
    """
    overrules = [v for v in (end, not_a_bill, habit, re_review) if v is not None]
    if len(overrules) > 1:
        console.print(
            "[red]Error:[/red] --end, --not-a-bill, --habit, and --re-review "
            "are mutually exclusive."
        )
        raise typer.Exit(1)
    if end is not None:
        request("PATCH", f"/recurring/{end}", {"status": "ended"})
        console.print(f"[green]Series {end} ended.[/green]")
    if not_a_bill is not None:
        request("POST", f"/recurring/{not_a_bill}/not-a-bill")
        console.print(
            f"[green]Series {not_a_bill} marked not-a-bill — "
            "suppressed from future detection.[/green]"
        )
    if habit is not None:
        request("POST", f"/recurring/{habit}/habit")
        console.print(
            f"[green]Series {habit} marked habit — discretionary, not a fixed cost.[/green]"
        )
    if re_review is not None:
        request("POST", f"/recurring/{re_review}/re-review")
        console.print(f"[green]Series {re_review} reopened for review.[/green]")
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
                fmt_money(abs(s["expected_cents"])),
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
    table.add_row("Accrual spend", fmt_money(r["accrual_cents"]))
    table.add_row("Cash out", fmt_money(r["cash_out_cents"]))
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
            fmt_money(ob["balance_owed_cents"]),
            fmt_money(ob["monthly_payment_cents"]) if ob["monthly_payment_cents"] else "?",
            str(ob["months_left"] or "?"),
            f"{payoff}{warn}",
            str(ob["promo_expires_on"] or "—"),
        )
    console.print(table)
    if any(ob["deferred_interest_risk"] for ob in rows):
        console.print("[red]! payoff lands after the promo expires — deferred interest risk[/red]")


def _parse_bool_flag(value: str) -> bool:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    console.print(f"[red]Error:[/red] invalid value {value!r} (expected true|false)")
    raise typer.Exit(1)


@app.command()
def accounts(
    set_type: Annotated[tuple[int, str] | None, typer.Option("--set-type")] = None,
    set_promo: Annotated[tuple[int, str] | None, typer.Option("--set-promo")] = None,
    set_financing: Annotated[tuple[int, str] | None, typer.Option("--set-financing")] = None,
) -> None:
    """List accounts. Flags: --set-type ID TYPE, --set-promo ID YYYY-MM-DD,
    --set-financing ID true|false."""
    if set_type:
        request("PATCH", f"/accounts/{set_type[0]}", {"type": set_type[1]})
    if set_promo:
        promo = _parse_iso_date(set_promo[1])
        request("PATCH", f"/accounts/{set_promo[0]}", {"promo_expires_on": promo})
    if set_financing:
        financing_mode = _parse_bool_flag(set_financing[1])
        request("PATCH", f"/accounts/{set_financing[0]}", {"financing_mode": financing_mode})
    rows = request("GET", "/accounts")
    table = Table("ID", "Name", "Org", "Type", "Balance", "Promo ends")
    for a in rows:
        type_cell = f"{a['type']} (financing)" if a.get("financing_mode") else a["type"]
        table.add_row(
            str(a["id"]),
            escape(a["name"]),
            escape(a["org_name"]),
            type_cell,
            fmt_money(a["balance_cents"]),
            str(a["promo_expires_on"] or "—"),
        )
    console.print(table)


_REVIEW_KINDS = {
    "recurring_cluster": (
        "bill or habit question",
        "bills become fixed costs; habits stay discretionary spending in moneta power",
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
    "financing_account": (
        "financing check",
        "confirming counts this card's payments as fixed costs in moneta power",
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
            console.print(f"    {s['posted_on']}  {fmt_money(abs(s['amount_cents']))}")
        if ctx.get("direction") == "inflow":
            console.print("    [dim](these are deposits — answering b counts them as income)[/dim]")
        payload = item.get("payload") or {}
        assert isinstance(payload, dict)
        if leaning := payload.get("llm_leaning"):
            console.print(f"    [dim](LLM leaned: {leaning})[/dim]")
        answer = typer.prompt(
            "Bill, habit, or not recurring? [b/h/n]", default="", show_default=False
        )
        normalized = answer.strip().lower()
        if not normalized:
            return None
        if normalized in ("b", "bill", "y", "yes"):
            return {"is_recurring": True}
        if normalized in ("h", "habit"):
            return {"is_recurring": True, "discretionary": True}
        if normalized in ("n", "no", "not"):
            return {"is_recurring": False}
        console.print("[red]invalid input, skipping[/red]")
        return None
    if item["kind"] == "price_change":
        for s in ctx.get("samples", []):
            console.print(f"    {s['posted_on']}  {fmt_money(abs(s['amount_cents']))}")
        old, new = ctx.get("old_amount_cents"), ctx.get("new_amount_cents")
        console.print(
            f"    {fmt_money(abs(old)) if isinstance(old, int) else '?'} → "
            f"{fmt_money(abs(new)) if isinstance(new, int) else '?'} on {ctx.get('occurred_on')}"
        )
        answer = _prompt_yes_no("Price change? [y/n]")
        return None if answer is None else {"is_price_change": answer}
    if item["kind"] == "transfer_pair":
        if outflow := ctx.get("outflow"):
            console.print(
                f"    out: {fmt_money(abs(outflow['amount_cents']))} on {outflow['posted_on']} "
                f"from {outflow['account']} — {outflow['description']!r}"
            )
        candidates = ctx.get("candidates") or []
        if candidates:
            for n, c in enumerate(candidates, 1):
                console.print(
                    f"    {n}. {fmt_money(abs(c['amount_cents']))} on {c['posted_on']} "
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
    if item["kind"] == "financing_account":
        answer = _prompt_yes_no("Treat as financing? [y/n]")
        return None if answer is None else {"financing": answer}
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


def _plaid_client() -> tuple["PlaidClient", "Settings"]:
    from moneta.aggregator.plaid import PlaidClient
    from moneta.config import load_settings

    settings = load_settings()
    if not (settings.plaid_client_id and settings.plaid_secret):
        console.print(
            "[red]Error:[/red] Plaid credentials not set. "
            "Run: moneta setup plaid <client_id> <secret>"
        )
        raise typer.Exit(1)
    return (
        PlaidClient(settings.plaid_client_id, settings.plaid_secret, settings.plaid_env),
        settings,
    )


@setup_app.command("plaid")
def setup_plaid(
    client_id: str,
    secret: str,
    env: Annotated[str, typer.Option("--env", help="production or sandbox")] = "production",
) -> None:
    """Save Plaid API credentials (get them at https://dashboard.plaid.com)."""
    from moneta.aggregator.plaid import PLAID_ENVS
    from moneta.config import save_config_value

    if env not in PLAID_ENVS:
        console.print(
            f"[red]Error:[/red] --env must be one of {', '.join(sorted(PLAID_ENVS))}, got {env!r}"
        )
        raise typer.Exit(1)
    save_config_value("plaid_client_id", client_id)
    save_config_value("plaid_secret", secret)
    save_config_value("plaid_env", env)
    console.print("[green]Plaid credentials saved.[/green] Link a bank: moneta setup plaid-link")


@setup_app.command("plaid-link")
def setup_plaid_link(
    product: Annotated[
        list[str] | None,
        typer.Option("--product", help="Repeat to add products (default: transactions)."),
    ] = None,
) -> None:
    """Link a bank via Plaid Hosted Link: prints a URL, waits for you to finish."""
    import asyncio

    from moneta.aggregator import plaid

    client, settings = _plaid_client()
    products = product or list(plaid.DEFAULT_PRODUCTS)

    async def _link() -> str:
        link_token, url = await plaid.create_hosted_link(client, products)
        console.print(f"Open this link in your browser to connect your bank:\n[bold]{url}[/bold]")
        console.print("Waiting for you to finish (Ctrl-C aborts)…")
        public_token, institution = await plaid.poll_link_result(client, link_token)
        access_token, item_id = await plaid.exchange_public_token(client, public_token)
        path = plaid.items_path(settings.config_dir)
        items = plaid.load_items(path)
        items.append(
            plaid.PlaidItem(
                item_id=item_id,
                access_token=access_token,
                institution_name=institution,
                products=products,
            )
        )
        plaid.save_items(path, items)
        return institution or item_id

    name = asyncio.run(_link())
    console.print(f"[green]Linked {name}.[/green] Run: moneta sync")


@setup_app.command("plaid-list")
def setup_plaid_list() -> None:
    """List linked Plaid institutions."""
    from moneta.aggregator.plaid import items_path, load_items
    from moneta.config import load_settings

    items = load_items(items_path(load_settings().config_dir))
    if not items:
        console.print("No Plaid items linked. Run: moneta setup plaid-link")
        return
    table = Table("Institution", "Item ID", "Products")
    for it in items:
        table.add_row(it.institution_name or "?", it.item_id, ", ".join(it.products))
    console.print(table)


@setup_app.command("plaid-unlink")
def setup_plaid_unlink(item_id: str) -> None:
    """Unlink a Plaid item (stops Plaid billing for it); synced data stays in the db."""
    import asyncio

    from moneta.aggregator.plaid import PlaidError, items_path, load_items, remove_item, save_items

    client, settings = _plaid_client()
    path = items_path(settings.config_dir)
    items = load_items(path)
    match = next((it for it in items if it.item_id == item_id), None)
    if match is None:
        console.print(
            f"[red]Error:[/red] no linked item {item_id!r} (see: moneta setup plaid-list)"
        )
        raise typer.Exit(1)
    try:
        asyncio.run(remove_item(client, match.access_token))
    except PlaidError as exc:
        # the local store is moneta's source of truth; a dead item (e.g. already
        # removed on Plaid's side) must still be removable locally
        console.print(f"[yellow]Plaid /item/remove failed ({exc}); removing locally.[/yellow]")
    save_items(path, [it for it in items if it.item_id != item_id])
    console.print(f"[green]Unlinked {match.institution_name or item_id}.[/green]")


@app.command()
def backup(
    dest: Annotated[str | None, typer.Argument(help="Destination file (server-side path).")] = None,
) -> None:
    """Snapshot the database with SQLite VACUUM INTO (safe while running)."""
    r = request("POST", "/backup", {"dest": dest} if dest else {})
    console.print(f"Backup written to [bold]{r['path']}[/bold]")


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

    from moneta.config import load_settings

    if host not in ("127.0.0.1", "::1", "localhost") and not load_settings().api_token:
        console.print(
            "[red]Error:[/red] refusing to bind a non-loopback host without an API token. "
            "Set MONETA_API_TOKEN or api_token in config.toml."
        )
        raise typer.Exit(1)
    uvicorn.run("moneta.api:build_app", host=host, port=port, factory=True)
