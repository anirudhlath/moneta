"""Transaction drill-down: the trust view. Every row shown, never silently hidden;
`counted_in_spend` replicates `views/power.py`'s spent-so-far rule exactly, and
`excluded_because` explains why a row isn't counted when it isn't (design 2026-07-16 §2).
"""

from datetime import date

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    SPEND_ACCOUNT_TYPES,
    Account,
    AccountType,
    RecurringSeries,
    SeriesStatus,
    Transaction,
)
from moneta.queries import ClassifiedLink, classified_links, primary_currency


class TxnRow(BaseModel):
    id: int
    posted_on: date
    account: str
    account_type: str
    merchant: str | None
    description: str
    amount_cents: int  # signed
    series: str | None  # owning series' merchant label
    series_status: str | None  # active|ended
    series_discretionary: bool | None
    link: str | None  # None | "internal" | "loan_payment" | "cc_payment"
    counted_in_spend: bool
    excluded_because: str | None  # human-readable single reason when not counted


def _link_field(
    txn_id: int, by_outflow: dict[int, ClassifiedLink], inflow_ids: set[int]
) -> str | None:
    outflow_link = by_outflow.get(txn_id)
    if outflow_link is not None:
        if outflow_link.inflow_is_loan_like:
            return "loan_payment"
        if outflow_link.inflow_account_type == AccountType.credit:
            return "cc_payment"
        return "internal"
    if txn_id in inflow_ids:
        return "internal"
    return None


def _excluded_because(
    txn: Transaction,
    account: Account,
    series: RecurringSeries | None,
    link: str | None,
    primary: str,
) -> str | None:
    """First matching reason, in spec §2's precedence order. `counted_in_spend` is
    `excluded_because is None` by construction — never a separately maintained bool."""
    if txn.amount_cents >= 0:
        return "inflow"
    if link == "loan_payment":
        return "loan payment"
    if link == "cc_payment":
        return "credit-card payment"
    if link == "internal":
        return "transfer"
    if series is not None and series.status == SeriesStatus.active and not series.discretionary:
        return f"fixed cost (series {series.merchant})"
    if account.type not in SPEND_ACCOUNT_TYPES:
        return "non-spend account"
    if account.currency != primary:
        return "foreign currency"
    return None


async def transactions_report(
    session: AsyncSession,
    start: date,
    end: date,
    account_id: int | None = None,
    merchant: str | None = None,
) -> list[TxnRow]:
    primary = await primary_currency(session)
    links = await classified_links(session)
    by_outflow = {link.outflow_id: link for link in links}
    inflow_ids = {link.inflow_id for link in links}

    conditions = [Transaction.posted_on >= start, Transaction.posted_on <= end]
    if account_id is not None:
        conditions.append(Transaction.account_id == account_id)
    if merchant is not None:
        conditions.append(Transaction.merchant.ilike(f"%{merchant}%"))

    rows = (
        await session.execute(
            select(Transaction, Account, RecurringSeries)
            .join(Account, Transaction.account_id == Account.id)
            .outerjoin(RecurringSeries, Transaction.series_id == RecurringSeries.id)
            .where(*conditions)
            .order_by(Transaction.posted_on.desc(), Transaction.id.desc())
        )
    ).all()

    result: list[TxnRow] = []
    for txn, account, series in rows:
        link = _link_field(txn.id, by_outflow, inflow_ids)
        reason = _excluded_because(txn, account, series, link, primary)
        result.append(
            TxnRow(
                id=txn.id,
                posted_on=txn.posted_on,
                account=account.name,
                account_type=account.type,
                merchant=txn.merchant,
                description=txn.description,
                amount_cents=txn.amount_cents,
                series=series.merchant if series is not None else None,
                series_status=series.status if series is not None else None,
                series_discretionary=series.discretionary if series is not None else None,
                link=link,
                counted_in_spend=reason is None,
                excluded_because=reason,
            )
        )
    return result
