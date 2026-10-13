"""One-to-one observation matching. Only the coordinator commits.

Every run derives the duplicate graph afresh, so corrections and account unlinks
restore observations. Explicit decisions live in the immutable review ledger.
"""

from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.identity import compatible_descriptors
from moneta.logical import _resolve_roots, resolve_account_roots
from moneta.models import (
    Account,
    AliasSource,
    LinkMethod,
    MerchantAlias,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.normalize import rule_normalize


def _physical_match(
    left: Transaction,
    right: Transaction,
    accounts: dict[int, Account],
    roots: dict[int, int],
) -> bool:
    a, b = accounts[left.account_id], accounts[right.account_id]
    return bool(
        a.source
        and b.source
        and a.source != b.source
        and roots[a.id] == roots[b.id]
        and a.currency == b.currency
        and left.amount_cents == right.amount_cents
        and abs((left.posted_on - right.posted_on).days) <= 4
    )


def _named(txn: Transaction) -> bool:
    name = rule_normalize(txn.merchant or txn.description).casefold()
    generic = {
        "payment",
        "transfer",
        "deposit",
        "withdrawal",
        "purchase",
        "ach",
        "debit",
        "credit",
        "unknown",
    }
    return bool(any(char.isalpha() for char in name) and name not in generic)


def _observation(txn: Transaction) -> dict[str, object]:
    return {
        "account_id": txn.account_id,
        "posted_on": txn.posted_on.isoformat(),
        "amount_cents": txn.amount_cents,
        "description": txn.description,
    }


async def set_transaction_duplicate(
    session: AsyncSession,
    transaction_id: int,
    duplicate_of_id: int | None,
) -> None:
    """Record an explicit, validated choice; caller owns commit/rollback.

    A null choice rejects the currently plausible pairs, without forbidding future
    unrelated observations. Human identity choices may override descriptor differences.
    """
    accounts = list(await session.scalars(select(Account)))
    roots = resolve_account_roots(accounts)
    by_account = {a.id: a for a in accounts}
    txns = list(await session.scalars(select(Transaction).order_by(Transaction.id)))
    by_id = {t.id: t for t in txns}
    if type(transaction_id) is not int or transaction_id not in by_id:
        raise ValueError("Unknown transaction")
    txn = by_id[transaction_id]
    targets = {t.id: t.duplicate_of_id for t in txns}
    old_roots = _resolve_roots(targets, "transaction")
    target: int | None = None
    if duplicate_of_id is not None:
        if type(duplicate_of_id) is not int or duplicate_of_id not in by_id:
            raise ValueError("Unknown duplicate transaction")
        if duplicate_of_id == transaction_id or old_roots[duplicate_of_id] == transaction_id:
            raise ValueError("Transaction duplicate would create a cycle")
        target = old_roots[duplicate_of_id]
    targets[transaction_id] = target
    proposed = _resolve_roots(targets, "transaction")
    group = [t for t in txns if proposed[t.id] == proposed[transaction_id]]
    for left, right in combinations(group, 2):
        if not _physical_match(left, right, by_account, roots):
            raise ValueError(
                "Duplicates require different providers, one account, equal signed cents "
                "and dates within four days"
            )
    candidates = [
        t.id
        for t in txns
        if t.id != txn.id
        and (
            old_roots[t.id] == old_roots[txn.id]
            or (_physical_match(txn, t, by_account, roots) and compatible_descriptors(txn, t))
        )
    ]
    if target is not None:
        candidates = [target]
    history = list(
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == ReviewKind.transaction_overlap,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    )
    for item in history:
        if item.payload.get("transaction_id") == transaction_id:
            item.status = ReviewStatus.resolved
            item.resolution = {"duplicate_of_id": target, "resolved_by": "manual"}
    txn.duplicate_of_id = target
    session.add(
        ReviewItem(
            kind=ReviewKind.transaction_overlap,
            question="Explicit transaction identity decision",
            payload={"transaction_id": txn.id, "candidates": candidates},
            status=ReviewStatus.resolved,
            resolution={
                "duplicate_of_id": target,
                "resolved_by": "manual",
                "observations": {
                    str(row_id): _observation(by_id[row_id])
                    for row_id in (transaction_id, target)
                    if row_id is not None
                },
            },
        )
    )
    await session.flush()


@dataclass(frozen=True)
class _TransferChoice:
    transaction_id: int
    physical_account_id: int
    logical_account_id: int
    amount_cents: int
    posted_on: date
    descriptor: str

    def equivalent(self, other: "_TransferChoice") -> bool:
        if self.transaction_id == other.transaction_id:
            return True
        return (
            self.physical_account_id != other.physical_account_id
            and self.logical_account_id == other.logical_account_id
            and self.amount_cents == other.amount_cents
            and self.posted_on == other.posted_on
            and self.descriptor == other.descriptor
        )


def _decisions(
    txns: list[Transaction],
    aliases: list[MerchantAlias],
    items: list[ReviewItem],
    links: list[TransferLink],
    account_roots: dict[int, int],
) -> dict[int, dict[str, object]]:
    """Only persisted provenance counts as reviewed; tags alone are derived."""
    decisions: dict[int, dict[str, object]] = {t.id: {} for t in txns}
    by_id = {t.id: t for t in txns}
    manual = {a.raw_descriptor: a.merchant for a in aliases if a.source == AliasSource.manual}

    def endpoint(txn_id: int) -> object:
        row = by_id.get(txn_id)
        if row is None:
            return ("missing", txn_id)
        return _TransferChoice(
            row.id,
            row.account_id,
            account_roots[row.account_id],
            row.amount_cents,
            row.posted_on,
            rule_normalize(row.description).casefold(),
        )

    for txn in txns:
        if txn.description in manual:
            decisions[txn.id]["merchant"] = manual[txn.description]
        direction = "outflow" if txn.amount_cents < 0 else "inflow"
        for item in items:
            if item.status != ReviewStatus.resolved or not item.resolution:
                continue
            if item.resolution.get("resolved_by") == "reconciliation":
                continue
            if (
                item.kind == ReviewKind.recurring_cluster
                and item.payload.get("merchant") == txn.merchant
                and item.payload.get("direction") == direction
                and isinstance(item.resolution.get("is_recurring"), bool)
            ):
                decisions[txn.id]["recurring"] = (
                    item.resolution["is_recurring"],
                    bool(item.resolution.get("discretionary")),
                )
            if (
                item.kind == ReviewKind.transfer_pair
                and item.payload.get("outflow_id") == txn.id
                and "inflow_id" in item.resolution
            ):
                inflow = item.resolution["inflow_id"]
                decisions[txn.id]["transfer"] = endpoint(inflow) if type(inflow) is int else None
    for link in links:
        if link.method != LinkMethod.rule:
            if link.outflow_id in decisions:
                decisions[link.outflow_id]["transfer"] = endpoint(link.inflow_id)
            if link.inflow_id in decisions:
                decisions[link.inflow_id]["transfer"] = endpoint(link.outflow_id)
    return decisions


def _conflicts(left: dict[str, object], right: dict[str, object]) -> bool:
    for key in left.keys() & right.keys():
        a, b = left[key], right[key]
        if isinstance(a, _TransferChoice) and isinstance(b, _TransferChoice):
            if not a.equivalent(b):
                return True
        elif a != b:
            return True
    return False


async def reconcile_transactions(session: AsyncSession) -> tuple[int, int]:
    """Recompute flat duplicate flags and refresh questions; return suppressed/restored."""
    accounts = list(await session.scalars(select(Account).order_by(Account.id)))
    account_roots = resolve_account_roots(accounts)
    by_account = {a.id: a for a in accounts}
    txns = list(await session.scalars(select(Transaction).order_by(Transaction.id)))
    by_id = {t.id: t for t in txns}
    before = {t.id: t.duplicate_of_id for t in txns}
    items = list(await session.scalars(select(ReviewItem).order_by(ReviewItem.id)))
    aliases = list(await session.scalars(select(MerchantAlias)))
    links = list(await session.scalars(select(TransferLink)))
    decisions = _decisions(txns, aliases, items, links, account_roots)
    rejected: set[frozenset[int]] = set()
    chosen: dict[int, int] = {}
    for item in items:
        if (
            item.kind != ReviewKind.transaction_overlap
            or item.status != ReviewStatus.resolved
            or not item.resolution
            or "duplicate_of_id" not in item.resolution
            or item.resolution.get("resolved_by") in ("rule", "reconciliation")
        ):
            continue
        row_id = item.payload.get("transaction_id")
        if type(row_id) is not int or row_id not in by_id:
            continue
        target = item.resolution["duplicate_of_id"]
        chosen.pop(row_id, None)
        if target is None:
            for candidate in item.payload.get("candidates", []):
                if type(candidate) is int:
                    rejected.add(frozenset((row_id, candidate)))
                    if chosen.get(candidate) == row_id:
                        chosen.pop(candidate)
        elif type(target) is int and target in by_id:
            rejected.discard(frozenset((row_id, target)))
            evidence = item.resolution.get("observations")
            if isinstance(evidence, dict):
                valid = all(
                    evidence.get(str(i)) == _observation(by_id[i]) for i in (row_id, target)
                )
            else:
                # Legacy review records lack snapshots, so require present identity evidence.
                valid = compatible_descriptors(by_id[row_id], by_id[target])
            if valid:
                chosen[row_id] = target

    groups = {t.id: {t.id} for t in txns}
    roots = {t.id: t.id for t in txns}

    def order(row_id: int) -> tuple[int, str, str, int]:
        row = by_id[row_id]
        return (
            -len(decisions[row_id]),
            by_account[row.account_id].source,
            row.aggregator_id,
            row.id,
        )

    def merge(left_id: int, right_id: int, manual: bool = False) -> bool:
        left_root, right_root = roots[left_id], roots[right_id]
        if left_root == right_root:
            return True
        members = groups[left_root] | groups[right_root]
        for x, y in combinations(sorted(members), 2):
            if not _physical_match(by_id[x], by_id[y], by_account, account_roots):
                return False
            if frozenset((x, y)) in rejected:
                return False
            if not manual and _conflicts(decisions[x], decisions[y]):
                return False
        survivor = right_root if manual else min(members, key=order)
        for member in members:
            roots[member] = survivor
        groups.pop(left_root)
        groups.pop(right_root)
        groups[survivor] = members
        return True

    for row_id, target in chosen.items():
        merge(row_id, target, manual=True)
    # Bucket on immutable monetary/account identity before comparing descriptors.
    buckets: dict[tuple[int, int], list[Transaction]] = {}
    for txn in txns:
        buckets.setdefault((account_roots[txn.account_id], txn.amount_cents), []).append(txn)
    edges: list[tuple[int, int]] = []
    for bucket in buckets.values():
        for left, right in combinations(bucket, 2):
            if (
                _physical_match(left, right, by_account, account_roots)
                and compatible_descriptors(left, right)
                and frozenset((left.id, right.id)) not in rejected
            ):
                edges.append((left.id, right.id))

    exact = [(a, b) for a, b in edges if by_id[a].posted_on == by_id[b].posted_on]
    neighbors: dict[int, set[int]] = {t.id: set() for t in txns}
    for a, b in exact:
        neighbors[a].add(b)
        neighbors[b].add(a)

    def equivalent(ids: set[int]) -> bool:
        # Equal repeated purchases are interchangeable; conflicting review choices are not.
        return all(
            compatible_descriptors(by_id[a], by_id[b]) and decisions[a] == decisions[b]
            for a, b in combinations(sorted(ids), 2)
        )

    for a, b in sorted(exact, key=lambda pair: (order(pair[0]), order(pair[1]))):
        if equivalent(neighbors[a]) and equivalent(neighbors[b]):
            merge(a, b)
    offsets: dict[int, set[int]] = {t.id: set() for t in txns}
    for a, b in edges:
        if roots[a] != a or roots[b] != b or len(groups[a]) > 1 or len(groups[b]) > 1:
            continue
        if by_id[a].posted_on != by_id[b].posted_on and _named(by_id[a]) and _named(by_id[b]):
            offsets[a].add(b)
            offsets[b].add(a)
    for a, candidates in offsets.items():
        if len(candidates) == 1:
            b = next(iter(candidates))
            if offsets[b] == {a}:
                merge(a, b)

    unresolved: dict[int, set[int]] = {}
    for a, b in edges:
        if roots[a] == roots[b]:
            continue
        # Already matched source observations cannot consume another purchase.
        sources_a = {by_account[by_id[i].account_id].source for i in groups[roots[a]]}
        sources_b = {by_account[by_id[i].account_id].source for i in groups[roots[b]]}
        if sources_a & sources_b:
            continue
        unresolved.setdefault(roots[a], set()).add(roots[b])
        unresolved.setdefault(roots[b], set()).add(roots[a])
    open_items: dict[int, ReviewItem] = {}
    for item in items:
        if item.kind == ReviewKind.transaction_overlap and item.status == ReviewStatus.open:
            row_id = item.payload.get("transaction_id")
            if type(row_id) is int:
                open_items[row_id] = item
    for row_id, candidates in unresolved.items():
        payload: dict[str, Any] = {"transaction_id": row_id, "candidates": sorted(candidates)}
        if row_id in open_items:
            open_items[row_id].payload = payload
        else:
            session.add(
                ReviewItem(
                    kind=ReviewKind.transaction_overlap,
                    question="Do these observations represent the same transaction?",
                    payload=payload,
                )
            )
    for row_id, item in open_items.items():
        if row_id not in unresolved:
            item.status = ReviewStatus.resolved
            item.resolution = {
                "resolved_by": "reconciliation",
                "superseded_reason": "Observation matched or candidates no longer compatible",
            }
    for txn in txns:
        txn.duplicate_of_id = roots[txn.id] if roots[txn.id] != txn.id else None
    await session.flush()
    return (
        sum(before[t.id] is None and t.duplicate_of_id is not None for t in txns),
        sum(before[t.id] is not None and t.duplicate_of_id is None for t in txns),
    )
