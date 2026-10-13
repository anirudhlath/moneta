"""Read-only projections of confirmed account and transaction relationships."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.account_metadata import account_decisions, effective_account_metadata
from moneta.models import Account, AccountType, Holding, ReviewItem, Transaction


@dataclass(frozen=True)
class LogicalAccount:
    """Root metadata with a single provider's most recent balance observation."""

    id: int
    aggregator_id: str
    name: str
    org_name: str
    type: AccountType
    currency: str
    balance_cents: int
    balance_date: date
    promo_expires_on: date | None
    financing_mode: bool
    source: str
    mask: str | None
    balance_account_id: int


@dataclass(frozen=True)
class LogicalHolding:
    """One physical holding, addressed by logical account, with effective vesting."""

    id: int
    account_id: int
    source_account_id: int
    symbol: str
    quantity: float
    market_value_cents: int
    vested_quantity: float | None
    unvested_quantity: float | None
    vesting_unresolved: bool


@dataclass(frozen=True)
class LogicalData:
    accounts: dict[int, LogicalAccount]
    account_roots: dict[int, int]
    transactions: list[Transaction]
    transaction_roots: dict[int, int]
    holdings: list[LogicalHolding]
    warnings: list[str]


def _resolve_roots(targets: dict[int, int | None], kind: str) -> dict[int, int]:
    roots: dict[int, int] = {}
    for start in targets:
        path: list[int] = []
        seen: set[int] = set()
        current = start
        while current not in roots:
            if current not in targets:
                raise ValueError(f"Dangling {kind} relationship from {start} to {current}")
            if current in seen:
                raise ValueError(f"Cyclic {kind} relationship at {current}")
            seen.add(current)
            path.append(current)
            target = targets[current]
            if target is None:
                roots[current] = current
                break
            current = target
        for node in path:
            roots[node] = roots[current]
    return roots


def resolve_account_roots(accounts: list[Account]) -> dict[int, int]:
    """Flatten confirmed aliases; reject dangling references and cycles."""
    return _resolve_roots(
        {account.id: account.canonical_account_id for account in accounts}, "account"
    )


def _observation_order(account: Account, root_id: int) -> tuple[int, bool, str, int]:
    # Ascending order puts newest observations first, then root, then stable source/id.
    return (-account.balance_date.toordinal(), account.id != root_id, account.source, account.id)


def _effective_vesting(
    observations: list[Holding], root_id: int
) -> tuple[float | None, float | None, bool]:
    vested = {row.vested_quantity for row in observations if row.vested_quantity is not None}
    unvested = {row.unvested_quantity for row in observations if row.unvested_quantity is not None}
    conflicting = len(vested) > 1 or len(unvested) > 1
    root = next((row for row in observations if row.account_id == root_id), None)
    if root is not None and (
        root.vested_quantity is not None or root.unvested_quantity is not None
    ):
        return root.vested_quantity, root.unvested_quantity, conflicting
    if conflicting:
        return None, None, True
    return next(iter(vested), None), next(iter(unvested), None), False


async def load_logical_data(session: AsyncSession) -> LogicalData:
    """Load a projection without flushing, changing, or committing physical rows.

    Survivor transactions retain their physical account IDs. Consumers use
    account_roots and transaction_roots to translate physical references.
    """
    with session.no_autoflush:
        accounts = list(await session.scalars(select(Account).order_by(Account.id)))
        transactions = list(await session.scalars(select(Transaction).order_by(Transaction.id)))
        holdings = list(await session.scalars(select(Holding).order_by(Holding.id)))
        decisions = account_decisions(list(await session.scalars(select(ReviewItem))))

    account_roots = resolve_account_roots(accounts)
    physical_accounts = {account.id: account for account in accounts}
    groups: dict[int, list[Account]] = {}
    for account in accounts:
        groups.setdefault(account_roots[account.id], []).append(account)

    logical_accounts: dict[int, LogicalAccount] = {}
    for root_id, group in sorted(groups.items()):
        root = physical_accounts[root_id]
        if any(account.currency != root.currency for account in group):
            raise ValueError(f"Conflicting currency in logical account {root_id}")
        latest = min(group, key=lambda account: _observation_order(account, root_id))
        financing, promotion = effective_account_metadata(root, group, decisions)
        logical_accounts[root_id] = LogicalAccount(
            id=root.id,
            aggregator_id=root.aggregator_id,
            name=root.name,
            org_name=root.org_name,
            type=root.type,
            currency=root.currency,
            balance_cents=latest.balance_cents,
            balance_date=latest.balance_date,
            promo_expires_on=promotion,
            financing_mode=financing,
            source=root.source,
            mask=root.mask,
            balance_account_id=latest.id,
        )

    transaction_roots = _resolve_roots(
        {txn.id: txn.duplicate_of_id for txn in transactions}, "transaction"
    )
    physical_transactions = {txn.id: txn for txn in transactions}
    survivor_sources: dict[int, set[str]] = {}
    for txn in transactions:
        if txn.account_id not in account_roots:
            raise ValueError(f"Dangling transaction account relationship at {txn.id}")
        survivor_id = transaction_roots[txn.id]
        survivor = physical_transactions[survivor_id]
        if account_roots.get(survivor.account_id) != account_roots[txn.account_id]:
            raise ValueError(f"Cross-account transaction duplicate at {txn.id}")
        source = physical_accounts[txn.account_id].source
        sources = survivor_sources.setdefault(survivor_id, set())
        if source in sources:
            raise ValueError(f"Same-provider transaction duplicates at {txn.id}")
        sources.add(source)

    holdings_by_account: dict[int, list[Holding]] = {}
    holdings_by_symbol: dict[tuple[int, str], list[Holding]] = {}
    for holding in holdings:
        if holding.account_id not in account_roots:
            raise ValueError(f"Dangling holding account relationship at {holding.id}")
        holdings_by_account.setdefault(holding.account_id, []).append(holding)
        holdings_by_symbol.setdefault(
            (account_roots[holding.account_id], holding.symbol), []
        ).append(holding)

    warnings: list[str] = []
    effective_vesting: dict[tuple[int, str], tuple[float | None, float | None, bool]] = {}
    for (root_id, symbol), observations in sorted(holdings_by_symbol.items()):
        vested, unvested, conflicting = _effective_vesting(observations, root_id)
        unresolved = conflicting and vested is None and unvested is None
        effective_vesting[root_id, symbol] = vested, unvested, unresolved
        if conflicting:
            warnings.append(
                f"Conflicting vesting annotations for account {root_id}, {symbol}; requires review"
            )

    logical_holdings: list[LogicalHolding] = []
    for root_id, group in sorted(groups.items()):
        available = [account for account in group if account.id in holdings_by_account]
        if not available:
            continue
        selected = min(available, key=lambda account: _observation_order(account, root_id))
        for holding in sorted(
            holdings_by_account[selected.id], key=lambda row: (row.symbol, row.id)
        ):
            vested, unvested, unresolved = effective_vesting[root_id, holding.symbol]
            logical_holdings.append(
                LogicalHolding(
                    id=holding.id,
                    account_id=root_id,
                    source_account_id=holding.account_id,
                    symbol=holding.symbol,
                    quantity=holding.quantity,
                    market_value_cents=holding.market_value_cents,
                    vested_quantity=vested,
                    unvested_quantity=unvested,
                    vesting_unresolved=unresolved,
                )
            )

    return LogicalData(
        accounts=logical_accounts,
        account_roots=account_roots,
        transactions=[txn for txn in transactions if transaction_roots[txn.id] == txn.id],
        transaction_roots=transaction_roots,
        holdings=logical_holdings,
        warnings=warnings,
    )
