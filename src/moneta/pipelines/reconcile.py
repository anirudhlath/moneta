"""Reconcile derived provider relationships without changing financial observations."""

from datetime import date

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier
from moneta.logical import LogicalData, load_logical_data
from moneta.models import (
    LinkMethod,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.account_overlap import propose_account_links
from moneta.pipelines.transaction_overlap import (
    human_recurring_keys,
    reconcile_transactions,
    reviewed_merchant_occurrences,
)
from moneta.pipelines.transfers import effective_transfer_choices


class OverlapStats(BaseModel):
    accounts_linked: int = 0
    transactions_suppressed: int = 0
    transactions_restored: int = 0
    reviews_opened: int = 0
    reviews_superseded: int = 0


def _supersede(item: ReviewItem, reason: str, today: date) -> None:
    item.status = ReviewStatus.resolved
    item.resolution = {
        "resolved_by": "reconciliation",
        "superseded_reason": reason,
        "superseded_on": today.isoformat(),
    }


async def _reconcile_links(session: AsyncSession, logical: LogicalData) -> set[int]:
    by_id = {t.id: t for t in logical.transactions}
    links = list(await session.scalars(select(TransferLink)))
    choices, conflicts = effective_transfer_choices(
        list(await session.scalars(select(ReviewItem))), logical.transaction_roots
    )
    rank = {LinkMethod.manual: 0, LinkMethod.llm: 1, LinkMethod.rule: 2}
    used: set[int] = set()
    keep: list[tuple[TransferLink, int, int]] = []
    for link in sorted(links, key=lambda row: (rank[row.method], -row.confidence, row.id)):
        out_id = logical.transaction_roots.get(link.outflow_id)
        in_id = logical.transaction_roots.get(link.inflow_id)
        out = by_id.get(out_id) if out_id is not None else None
        inn = by_id.get(in_id) if in_id is not None else None
        valid = bool(
            out is not None
            and inn is not None
            and out_id not in conflicts
            and (out_id not in choices or choices[out_id].inflow_id == in_id)
            and out.amount_cents < 0
            and inn.amount_cents == -out.amount_cents
            and abs((out.posted_on - inn.posted_on).days) <= 6
            and logical.account_roots[out.account_id] != logical.account_roots[inn.account_id]
            and logical.accounts[logical.account_roots[out.account_id]].currency
            == logical.accounts[logical.account_roots[inn.account_id]].currency
        )
        if not valid or out_id in used or in_id in used:
            await session.delete(link)
        else:
            assert out_id is not None and in_id is not None
            keep.append((link, out_id, in_id))
            used.update((out_id, in_id))
    # Clear conflicting unique endpoints before repointing the retained link.
    await session.flush()
    for link, out_id, in_id in keep:
        link.outflow_id, link.inflow_id = out_id, in_id
        if out_id in choices:
            link.method = choices[out_id].method
            link.confidence = 1.0 if link.method == LinkMethod.manual else 0.9
    await session.flush()
    return used


async def _retire_questions(
    session: AsyncSession,
    logical: LogicalData,
    linked: set[int],
    today: date,
) -> None:
    txns = list(await session.scalars(select(Transaction)))
    survivors = {t.id: t for t in logical.transactions}
    suppressed = [t for t in txns if t.id not in survivors]
    raw_descriptors = {t.description for t in suppressed}
    live_descriptors = {t.description for t in survivors.values()}
    reviewed_merchants = await reviewed_merchant_occurrences(session, logical.transaction_roots)

    def key(txn: Transaction) -> tuple[str | None, str]:
        return txn.merchant, "outflow" if txn.amount_cents < 0 else "inflow"

    raw_keys = {key(t) for t in suppressed}
    live_keys = {key(t) for t in survivors.values()}
    orphaned: set[int] = set()
    for series in await session.scalars(select(RecurringSeries)):
        if (
            any(t.series_id == series.id for t in suppressed)
            and not any(t.series_id == series.id for t in survivors.values())
            and (series.merchant, series.direction) not in live_keys
        ):
            series.status = SeriesStatus.ended
            orphaned.add(series.id)
    items = list(
        await session.scalars(select(ReviewItem).where(ReviewItem.status == ReviewStatus.open))
    )
    choices, _ = effective_transfer_choices(
        list(await session.scalars(select(ReviewItem))), logical.transaction_roots
    )
    recurring_choices = human_recurring_keys(list(await session.scalars(select(ReviewItem))))
    for item in items:
        if item.kind == ReviewKind.merchant:
            descriptor = item.payload.get("descriptor")
            if any(
                t.description == descriptor
                and logical.transaction_roots[t.id] in reviewed_merchants
                for t in txns
            ):
                _supersede(
                    item,
                    "Occurrence already has a human merchant or recurring classification",
                    today,
                )
            elif descriptor in raw_descriptors and descriptor not in live_descriptors:
                _supersede(item, "Descriptor is supported only by suppressed observations", today)
        elif item.kind == ReviewKind.recurring_cluster:
            series_key = (item.payload.get("merchant"), item.payload.get("direction"))
            if series_key in recurring_choices:
                _supersede(item, "Recurring cluster already has a human decision", today)
            elif series_key in raw_keys and series_key not in live_keys:
                _supersede(
                    item, "Recurring cluster is supported only by suppressed observations", today
                )
        elif item.kind == ReviewKind.price_change:
            if item.payload.get("series_id") in orphaned:
                _supersede(item, "Series is supported only by suppressed observations", today)
        elif item.kind == ReviewKind.transfer_pair:
            old_out = item.payload.get("outflow_id")
            out_id = logical.transaction_roots.get(old_out) if type(old_out) is int else None
            if out_id in choices and choices[out_id].method == LinkMethod.manual:
                _supersede(
                    item, "Occurrence already has an effective human transfer decision", today
                )
                continue
            out = survivors.get(out_id) if out_id is not None else None
            candidates: set[int] = set()
            for candidate in item.payload.get("candidates", []):
                target = (
                    logical.transaction_roots.get(candidate) if type(candidate) is int else None
                )
                inn = survivors.get(target) if target is not None else None
                if (
                    out is not None
                    and inn is not None
                    and inn.id not in linked
                    and out.amount_cents < 0
                    and inn.amount_cents == -out.amount_cents
                    and abs((out.posted_on - inn.posted_on).days) <= 6
                    and logical.account_roots[out.account_id]
                    != logical.account_roots[inn.account_id]
                    and logical.accounts[logical.account_roots[out.account_id]].currency
                    == logical.accounts[logical.account_roots[inn.account_id]].currency
                ):
                    candidates.add(inn.id)
            if out is None or out_id in linked or not candidates:
                _supersede(
                    item, "Transfer endpoints are matched, missing, or no longer compatible", today
                )
            else:
                item.payload = {
                    **item.payload,
                    "outflow_id": out_id,
                    "candidates": sorted(candidates),
                }
    # Repointed open transfer questions can coincide; keep the first and retire duplicates.
    seen: set[int] = set()
    for item in sorted(items, key=lambda row: row.id):
        if item.kind == ReviewKind.transfer_pair and item.status == ReviewStatus.open:
            out_id = item.payload.get("outflow_id")
            if type(out_id) is int:
                if out_id in seen:
                    _supersede(item, "Equivalent survivor transfer question already exists", today)
                seen.add(out_id)


async def reconcile_provider_overlap(session: AsyncSession, today: date) -> OverlapStats:
    """Own the commit for account matching, transaction flags, and derived cleanup."""
    before = {item.id: item.status for item in await session.scalars(select(ReviewItem))}
    stats = OverlapStats(accounts_linked=await propose_account_links(session))
    stats.transactions_suppressed, stats.transactions_restored = await reconcile_transactions(
        session
    )
    logical = await load_logical_data(session)
    linked = await _reconcile_links(session, logical)
    await _retire_questions(session, logical, linked, today)
    await session.flush()
    for item in await session.scalars(select(ReviewItem)):
        if item.id not in before and item.status == ReviewStatus.open:
            stats.reviews_opened += 1
        if (
            before.get(item.id) == ReviewStatus.open
            and item.status == ReviewStatus.resolved
            and item.resolution
            and item.resolution.get("superseded_reason")
        ):
            stats.reviews_superseded += 1
    await session.commit()
    return stats


async def rebuild_logical_derived(
    session: AsyncSession, llm: Classifier | None, today: date
) -> None:
    """Rebuild from reconciled observations; never fetch or advance sync freshness."""
    from moneta.pipelines.events import emit_series_events
    from moneta.pipelines.financing import detect_financing
    from moneta.pipelines.recurring import detect_recurring
    from moneta.pipelines.review import autoreview_items, verify_series
    from moneta.pipelines.transfers import link_transfers

    await link_transfers(session, llm)
    await detect_financing(session)
    if llm is not None:
        await autoreview_items(session, llm)
    await detect_recurring(session, llm, today)
    await verify_series(session, llm)
    await emit_series_events(session, llm, today)
