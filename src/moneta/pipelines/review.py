"""Review-item context, resolution application, and LLM auto-review.

Shared by the API (human review) and the sync pipeline (LLM auto-review).
LLM answers are classifications only and are applied through the exact same
resolution path a human answer takes, tagged resolved_by="llm" for audit.
"""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier
from moneta.models import (
    Account,
    AliasSource,
    LinkMethod,
    MerchantAlias,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    Transaction,
    TransferLink,
)

_MERCHANT_PROMPT = """You are auto-resolving a personal-finance review question.
Name the merchant behind this bank descriptor.
Descriptor: {descriptor!r} (current guess: {suggested!r})
Respond with JSON: {{"merchant": "<canonical name, title case>" | null, "confident": true/false}}
Set confident=true ONLY if the merchant is clearly identifiable from the descriptor."""

_TRANSFER_PROMPT = """You are auto-resolving a personal-finance review question.
An outflow may be an internal transfer; pick its matching inflow.
Outflow: {outflow}
Candidate inflows: {candidates}
Respond with JSON: {{"inflow_id": <id> | null, "confident": true/false}}
Set confident=true ONLY if exactly one candidate clearly matches (same money movement).
Use inflow_id=null with confident=true if you are sure NONE match."""

_RECURRING_PROMPT = """You are auto-resolving a personal-finance review question.
Is this one recurring bill/subscription/income stream (vs. one-off spending)?
Merchant: {merchant!r}; direction: {direction}; recent charges: {samples}
Respond with JSON: {{"is_recurring": true/false, "confident": true/false}}
Set confident=true ONLY if the pattern is clear."""

_VERIFY_PROMPT = """You are double-checking automatic recurring-bill detection.
Is this one recurring bill/subscription/income stream (vs. habitual spending
like groceries, gas, or dining that merely happens on a regular rhythm)?
Merchant: {merchant!r}; direction: {direction}; cadence: {cadence}; \
expected amount: ${expected}; recent occurrences: {samples}
Respond with JSON: {{"is_recurring": true/false, "confident": true/false}}
Set confident=true ONLY if you are sure either way."""


async def txn_summaries(session: AsyncSession, txn_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not txn_ids:
        return {}
    rows = (
        await session.execute(
            select(Transaction, Account.name)
            .join(Account, Transaction.account_id == Account.id)
            .where(Transaction.id.in_(txn_ids))
        )
    ).all()
    return {
        txn.id: {
            "id": txn.id,
            "posted_on": txn.posted_on.isoformat(),
            "amount": f"{abs(txn.amount_cents) / 100:.2f}",
            "description": txn.description,
            "account": account_name,
        }
        for txn, account_name in rows
    }


async def review_context(session: AsyncSession, item: ReviewItem) -> dict[str, Any]:
    if item.kind == ReviewKind.transfer_pair:
        outflow_id = item.payload.get("outflow_id")
        candidate_ids = [c for c in item.payload.get("candidates", []) if isinstance(c, int)]
        ids = ([outflow_id] if isinstance(outflow_id, int) else []) + candidate_ids
        summaries = await txn_summaries(session, ids)
        context: dict[str, Any] = {
            "candidates": [summaries[c] for c in candidate_ids if c in summaries]
        }
        if isinstance(outflow_id, int) and outflow_id in summaries:
            context["outflow"] = summaries[outflow_id]
        return context
    if item.kind == ReviewKind.recurring_cluster:
        merchant = item.payload.get("merchant")
        if not isinstance(merchant, str):
            return {}
        txns = (
            (
                await session.execute(
                    select(Transaction)
                    .where(Transaction.merchant == merchant)
                    .order_by(Transaction.posted_on.desc())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        return {
            "samples": [
                {
                    "posted_on": t.posted_on.isoformat(),
                    "amount": f"{abs(t.amount_cents) / 100:.2f}",
                }
                for t in txns
            ],
            "direction": item.payload.get("direction"),
        }
    if item.kind == ReviewKind.merchant:
        return {
            "descriptor": item.payload.get("descriptor"),
            "suggested": item.payload.get("fallback"),
        }
    return {}


async def apply_resolution(
    session: AsyncSession,
    item: ReviewItem,
    resolution: dict[str, Any],
    resolved_by: str = "manual",
) -> None:
    """Apply a resolution's effects and mark the item resolved. Caller commits."""
    if item.kind == ReviewKind.merchant and isinstance(resolution.get("merchant"), str):
        merchant = resolution["merchant"]
        raw = item.payload["descriptor"]
        source = AliasSource.llm if resolved_by == "llm" else AliasSource.manual
        alias = (
            await session.execute(select(MerchantAlias).where(MerchantAlias.raw_descriptor == raw))
        ).scalar_one_or_none()
        if alias is None:
            session.add(MerchantAlias(raw_descriptor=raw, merchant=merchant, source=source))
        else:
            alias.merchant = merchant
            alias.source = source
        for txn in (
            await session.execute(select(Transaction).where(Transaction.description == raw))
        ).scalars():
            txn.merchant = merchant
    elif item.kind == ReviewKind.transfer_pair and isinstance(resolution.get("inflow_id"), int):
        session.add(
            TransferLink(
                outflow_id=item.payload["outflow_id"],
                inflow_id=resolution["inflow_id"],
                confidence=1.0 if resolved_by == "manual" else 0.9,
                method=LinkMethod.manual if resolved_by == "manual" else LinkMethod.llm,
            )
        )
    elif item.kind == ReviewKind.recurring_cluster and resolution.get("is_recurring") is False:
        # detection's force map suppresses future runs; end the live series now so
        # fixed costs stop counting it immediately instead of after the stale sweep
        stmt = select(RecurringSeries).where(
            RecurringSeries.merchant == item.payload.get("merchant"),
            RecurringSeries.status == SeriesStatus.active,
        )
        direction = item.payload.get("direction")
        if direction is not None:
            stmt = stmt.where(RecurringSeries.direction == direction)
        for series in (await session.execute(stmt)).scalars():
            series.status = SeriesStatus.ended
    item.status = ReviewStatus.resolved
    item.resolution = {**resolution, "resolved_by": resolved_by}


def _validated(item: ReviewItem, answer: dict[str, Any]) -> dict[str, Any] | None:
    """Return the resolution to apply, or None when the answer isn't trustworthy."""
    if answer.get("confident") is not True:
        return None
    if item.kind == ReviewKind.merchant:
        merchant = answer.get("merchant")
        merchant = merchant.strip() if isinstance(merchant, str) else None
        return {"merchant": merchant} if merchant else None
    if item.kind == ReviewKind.transfer_pair:
        inflow_id = answer.get("inflow_id")
        candidates = [c for c in item.payload.get("candidates", []) if isinstance(c, int)]
        if inflow_id is None:
            return {"inflow_id": None}  # confident no-match: resolve without a link
        if isinstance(inflow_id, int) and inflow_id in candidates:
            return {"inflow_id": inflow_id}
        return None
    if item.kind == ReviewKind.recurring_cluster:
        is_recurring = answer.get("is_recurring")
        return {"is_recurring": is_recurring} if isinstance(is_recurring, bool) else None
    return None


async def autoreview_items(session: AsyncSession, llm: Classifier) -> int:
    """Ask the LLM to resolve open review items it is confident about."""
    items = (
        (await session.execute(select(ReviewItem).where(ReviewItem.status == ReviewStatus.open)))
        .scalars()
        .all()
    )
    resolved = 0
    for item in items:
        if item.payload.get("llm_flagged"):
            continue  # opened because the LLM already looked — human-only
        context = await review_context(session, item)
        if item.kind == ReviewKind.merchant:
            prompt = _MERCHANT_PROMPT.format(
                descriptor=context.get("descriptor"), suggested=context.get("suggested")
            )
        elif item.kind == ReviewKind.transfer_pair:
            if not context.get("candidates"):
                continue
            prompt = _TRANSFER_PROMPT.format(
                outflow=context.get("outflow"), candidates=context["candidates"]
            )
        elif item.kind == ReviewKind.recurring_cluster:
            prompt = _RECURRING_PROMPT.format(
                merchant=item.payload.get("merchant"),
                direction=context.get("direction"),
                samples=context.get("samples"),
            )
        else:
            continue
        answer = await llm.classify_json(prompt)
        if not answer:
            continue
        resolution = _validated(item, answer)
        if resolution is None:
            continue
        await apply_resolution(session, item, resolution, resolved_by="llm")
        resolved += 1
    await session.commit()
    return resolved


class VerifyStats(BaseModel):
    verified: int = 0
    flagged: int = 0


async def verify_series(session: AsyncSession, llm: Classifier | None) -> VerifyStats:
    """LLM second opinion on deterministically detected series.

    A recurring_cluster ReviewItem (open or resolved) is the per-merchant
    verification ledger: confident "yes" is recorded resolved (which also feeds
    detect_recurring's force map); anything else opens a human item flagged
    llm_flagged so autoreview never re-asks the LLM. The LLM never suppresses a
    deterministic detection — flagged series stay active until a human rules.
    """
    stats = VerifyStats()
    if llm is None:
        return stats
    seen = {
        item.payload.get("merchant")
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalars()
    }
    series_list = (
        (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.active)
            )
        )
        .scalars()
        .all()
    )
    for series in series_list:
        if series.merchant in seen:
            continue
        txns = (
            (
                await session.execute(
                    select(Transaction)
                    .where(Transaction.series_id == series.id)
                    .order_by(Transaction.posted_on.desc())
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        answer = await llm.classify_json(
            _VERIFY_PROMPT.format(
                merchant=series.merchant,
                direction=series.direction,
                cadence=series.cadence,
                expected=f"{abs(series.expected_cents) / 100:.2f}",
                samples=[
                    (t.posted_on.isoformat(), f"{abs(t.amount_cents) / 100:.2f}") for t in txns
                ],
            )
        )
        payload: dict[str, Any] = {"merchant": series.merchant, "direction": series.direction}
        if answer and answer.get("is_recurring") is True and answer.get("confident") is True:
            session.add(
                ReviewItem(
                    kind=ReviewKind.recurring_cluster,
                    question=f"Is {series.merchant!r} a recurring bill?",
                    payload=payload,
                    status=ReviewStatus.resolved,
                    resolution={"is_recurring": True, "resolved_by": "llm"},
                )
            )
            stats.verified += 1
        else:
            session.add(
                ReviewItem(
                    kind=ReviewKind.recurring_cluster,
                    question=f"Is {series.merchant!r} a recurring bill?",
                    payload={**payload, "llm_flagged": True},
                )
            )
            stats.flagged += 1
    await session.commit()
    return stats
