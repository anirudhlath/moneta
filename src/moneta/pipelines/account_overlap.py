"""Conservative account identity proposals and durable, reversible human decisions.

Both entry points flush only: the reconciliation coordinator owns the transaction.
Physical observations and root metadata are never rewritten.
"""

import re
from itertools import combinations, product

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.identity import normalize_institution
from moneta.logical import resolve_account_roots
from moneta.models import Account, AccountType, ReviewItem, ReviewKind, ReviewStatus


def _product(account: Account) -> str:
    name = re.sub(r"\([0-9]{4}\)\s*$", "", account.name.casefold())
    institution = normalize_institution(account.org_name)
    # Remove only an explicit institution prefix and generic account vocabulary.
    for prefix in (account.org_name.casefold(), institution):
        if prefix and name.startswith(prefix + " "):
            name = name[len(prefix) :].strip()
    generic = {"account", "credit", "card", "checking", "savings", "brokerage", "loan"}
    return " ".join(word for word in re.findall(r"\w+", name) if word not in generic)


def _plausible(left: Account, right: Account) -> bool:
    institution = normalize_institution(left.org_name)
    return bool(
        left.source
        and right.source
        and left.source != right.source
        and institution
        and institution == normalize_institution(right.org_name)
        and left.currency == right.currency
        and (not left.mask or not right.mask or left.mask == right.mask)
    )


def _corroborated(left: Account, right: Account) -> bool:
    left_product, right_product = _product(left), _product(right)
    return bool(
        _plausible(left, right)
        and left.mask
        and re.fullmatch(r"[0-9]{4}", left.mask)
        and left.mask == right.mask
        and left.type != AccountType.unknown
        and left.type == right.type
        and (not left_product or not right_product or left_product == right_product)
    )


async def _history(session: AsyncSession) -> list[ReviewItem]:
    return list(
        await session.scalars(
            select(ReviewItem)
            .where(ReviewItem.kind == ReviewKind.account_overlap)
            .order_by(ReviewItem.id)
        )
    )


async def _financing_decisions(session: AsyncSession) -> dict[int, bool]:
    """Recover explicit negatives, which otherwise look like the default False."""
    decisions: dict[int, bool] = {}
    items = await session.scalars(
        select(ReviewItem)
        .where(
            ReviewItem.kind == ReviewKind.financing_account,
            ReviewItem.status == ReviewStatus.resolved,
        )
        .order_by(ReviewItem.id)
    )
    for item in items:
        account_id = item.payload.get("account_id")
        financing = (item.resolution or {}).get("financing")
        if type(account_id) is int and isinstance(financing, bool):
            decisions[account_id] = financing
    return decisions


def _metadata_conflicts(accounts: list[Account], financing: dict[int, bool]) -> list[str]:
    """Compare saved values across every member, without treating defaults as decisions."""
    conflicts: list[str] = []
    if len({a.type for a in accounts if a.type != AccountType.unknown}) > 1:
        conflicts.append("type")
    if len({a.promo_expires_on for a in accounts if a.promo_expires_on is not None}) > 1:
        conflicts.append("promo_expires_on")
    modes = {a.financing_mode for a in accounts if a.financing_mode or financing.get(a.id) is False}
    if len(modes) > 1:
        conflicts.append("financing_mode")
    return conflicts


async def set_account_link(
    session: AsyncSession,
    source_account_id: int,
    canonical_account_id: int | None,
) -> None:
    """Validate before mutation, then append a manual decision to the review ledger.

    Targets may be aliases; store their root. Unlink detaches the source and its
    descendants. Self-links and links to descendants are errors, even if a root
    lookup would otherwise hide the cycle. The caller commits or rolls back.
    """
    accounts = list(await session.scalars(select(Account).order_by(Account.id)))
    by_id = {account.id: account for account in accounts}
    if type(source_account_id) is not int or source_account_id not in by_id:
        raise ValueError("Unknown source account")
    roots = resolve_account_roots(accounts)
    target: int | None = None
    if canonical_account_id is not None:
        if type(canonical_account_id) is not int or canonical_account_id not in by_id:
            raise ValueError("Unknown canonical account")
        if canonical_account_id == source_account_id:
            raise ValueError("An account cannot link to itself")
        current: int | None = canonical_account_id
        while current is not None:
            if current == source_account_id:
                raise ValueError("Account link would create a cycle")
            current = by_id[current].canonical_account_id
        target = roots[canonical_account_id]

    # Detached graph copies ensure validation cannot dirty a partially changed graph.
    proposed = [
        Account(
            id=account.id,
            canonical_account_id=(
                target if account.id == source_account_id else account.canonical_account_id
            ),
        )
        for account in accounts
    ]
    proposed_roots = resolve_account_roots(proposed)
    affected_root = proposed_roots[source_account_id]
    currencies = {
        account.currency for account in accounts if proposed_roots[account.id] == affected_root
    }
    if len(currencies) != 1:
        raise ValueError("Cannot group accounts with conflicting currencies")
    affected_accounts = [
        account for account in accounts if proposed_roots[account.id] == affected_root
    ]
    conflicts = _metadata_conflicts(affected_accounts, await _financing_decisions(session))
    if conflicts:
        raise ValueError(f"Cannot group accounts with conflicting metadata: {', '.join(conflicts)}")

    history = await _history(session)
    for item in history:
        if item.status != ReviewStatus.open:
            continue
        account_id = item.payload.get("account_id")
        if account_id == source_account_id:
            item.status = ReviewStatus.resolved
            item.resolution = {"canonical_account_id": target, "resolved_by": "manual"}
        elif type(account_id) is int and account_id in proposed_roots:
            alternatives = item.payload.get("candidates")
            root_id = proposed_roots[account_id]
            if (
                isinstance(alternatives, list)
                and alternatives
                and all(
                    type(candidate) is int and proposed_roots.get(candidate) == root_id
                    for candidate in alternatives
                )
            ):
                item.status = ReviewStatus.resolved
                item.resolution = {
                    "canonical_account_id": root_id if account_id != root_id else None,
                    "resolved_by": "manual",
                }
    by_id[source_account_id].canonical_account_id = target
    session.add(
        ReviewItem(
            kind=ReviewKind.account_overlap,
            question="Explicit account identity decision",
            payload={
                "account_id": source_account_id,
                "candidates": [] if target is None else [target],
            },
            status=ReviewStatus.resolved,
            resolution={"canonical_account_id": target, "resolved_by": "manual"},
        )
    )
    await session.flush()


async def propose_account_links(session: AsyncSession) -> int:
    """Link uniquely corroborated groups; queue uncertainty; return new link count.

    Equal balances never participate in identity. Candidate uniqueness is checked
    in both directions before any mutation, including uncertain alternatives.
    Manual decisions protect both ends of their current group from automation.
    """
    accounts = list(await session.scalars(select(Account).order_by(Account.id)))
    roots = resolve_account_roots(accounts)
    by_id = {account.id: account for account in accounts}
    groups: dict[int, list[Account]] = {}
    for account in accounts:
        groups.setdefault(roots[account.id], []).append(account)
    history = await _history(session)
    financing = await _financing_decisions(session)
    protected: set[int] = set()
    open_items: dict[int, ReviewItem] = {}
    for item in history:
        account_id = item.payload.get("account_id")
        if type(account_id) is not int or account_id not in roots:
            continue
        if item.status == ReviewStatus.open:
            open_items[account_id] = item
        elif (
            item.resolution is not None
            and "canonical_account_id" in item.resolution
            and item.resolution.get("resolved_by") != "rule"
        ):
            protected.add(roots[account_id])

    candidates: dict[int, set[int]] = {root: set() for root in groups}
    metadata_conflicts: dict[int, set[str]] = {root: set() for root in groups}
    strong: set[tuple[int, int]] = set()
    for left_id, right_id in combinations(groups, 2):
        left, right = groups[left_id], groups[right_id]
        pairs = list(product(left, right))
        if not any(_plausible(a, b) for a, b in pairs):
            continue
        candidates[left_id].add(right_id)
        candidates[right_id].add(left_id)
        conflicts = _metadata_conflicts(left + right, financing)
        metadata_conflicts[left_id].update(conflicts)
        metadata_conflicts[right_id].update(conflicts)
        sources = [account.source for account in left + right]
        if (
            not conflicts
            and len(set(sources)) == len(sources)
            and all(_corroborated(a, b) for a, b in pairs)
        ):
            strong.add((left_id, right_id))

    linked: set[int] = set()
    for left_id, right_id in sorted(strong):
        if left_id in protected or right_id in protected:
            continue
        if candidates[left_id] != {right_id} or candidates[right_id] != {left_id}:
            continue
        # Keep an established group's root; otherwise keep the earliest source row.
        if len(groups[right_id]) > 1 and len(groups[left_id]) == 1:
            root_id, alias_id = right_id, left_id
        else:
            root_id, alias_id = left_id, right_id
        root, alias = by_id[root_id], by_id[alias_id]
        alias.canonical_account_id = root_id
        linked.update((root_id, alias_id))
        for item in history:
            if item.status == ReviewStatus.open and item.payload.get("account_id") in (
                root_id,
                alias_id,
            ):
                item.status = ReviewStatus.resolved
                item.resolution = {
                    "canonical_account_id": (
                        root_id if item.payload["account_id"] == alias_id else None
                    ),
                    "resolved_by": "rule",
                }
        session.add(
            ReviewItem(
                kind=ReviewKind.account_overlap,
                question="Corroborated cross-provider account identity",
                payload={
                    "account_id": alias_id,
                    "candidates": [root_id],
                    "evidence": {
                        "institution": normalize_institution(root.org_name),
                        "mask": root.mask,
                        "type": str(root.type),
                        "currency": root.currency,
                        "sources": sorted(
                            {account.source for account in groups[root_id] + groups[alias_id]}
                        ),
                        "products": [_product(root), _product(alias)],
                    },
                },
                status=ReviewStatus.resolved,
                resolution={"canonical_account_id": root_id, "resolved_by": "rule"},
            )
        )

    for account_id, alternatives in candidates.items():
        alternatives = alternatives - protected
        if not alternatives or account_id in linked or account_id in protected:
            continue
        payload = {"account_id": account_id, "candidates": sorted(alternatives)}
        if metadata_conflicts[account_id]:
            payload["metadata_conflicts"] = sorted(metadata_conflicts[account_id])
        if account_id in open_items:
            open_items[account_id].payload = payload
        else:
            session.add(
                ReviewItem(
                    kind=ReviewKind.account_overlap,
                    question=(
                        f"Does {by_id[account_id].name!r} represent the same account "
                        "as another source?"
                    ),
                    payload=payload,
                )
            )
    await session.flush()
    return len(linked) // 2
