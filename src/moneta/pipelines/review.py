"""Review-item context, resolution application, and LLM auto-review.

Shared by the API (human review) and the sync pipeline (LLM auto-review).
LLM answers are classifications only and are applied through the exact same
resolution path a human answer takes, tagged resolved_by="llm" for audit.
"""

import statistics
from datetime import date
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import CLASSIFICATION_TAXONOMY, Classifier
from moneta.logical import load_logical_data
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
    amounts_match,
    bill_ref,
    dollars,
    recurring_cluster_item,
    same_bill,
    series_ref,
)
from moneta.pipelines.account_overlap import set_account_link
from moneta.pipelines.events import apply_price_change
from moneta.pipelines.recurring import newest_run
from moneta.pipelines.transaction_overlap import (
    human_recurring_refs,
    reviewed_merchant_occurrences,
    set_transaction_duplicate,
)
from moneta.pipelines.transfers import effective_transfer_choices
from moneta.queries import (
    bill_amount,
    foreign_series_ids,
    price_review_matches_currency,
    primary_currency,
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

_VERIFY_PROMPT = (
    "You are double-checking automatic recurring-bill detection.\n"
    f"{CLASSIFICATION_TAXONOMY}\n"
    "Merchant: {merchant!r}; direction: {direction}; cadence: {cadence}; "
    "expected amount: ${expected}; amount cents min/median/max: {lo}/{med}/{hi}; "
    "recent occurrences: {samples}\n"
    'Respond with JSON: {{"classification": "bill" | "habit" | "not_recurring", '
    '"confident": true/false}}\n'
    "Set confident=true ONLY if you are sure either way."
)


def _sample(txn: Transaction) -> dict[str, Any]:
    return {"posted_on": txn.posted_on.isoformat(), "amount_cents": txn.amount_cents}


def _prompt_txn(summary: dict[str, Any]) -> dict[str, Any]:
    """Context payloads carry machine cents; prompt text gets human dollars."""
    out: dict[str, Any] = {}
    for key, value in summary.items():
        if key.endswith("_cents") and isinstance(value, int):
            out[key.removesuffix("_cents")] = dollars(value)
        else:
            out[key] = value
    return out


async def _recent_occurrences(session: AsyncSession, series_id: int) -> list[Transaction]:
    logical = await load_logical_data(session)
    primary = await primary_currency(session)
    return sorted(
        (
            t
            for t in logical.transactions
            if t.series_id == series_id
            and logical.accounts[logical.account_roots[t.account_id]].currency == primary
        ),
        key=lambda t: (t.posted_on, t.id),
        reverse=True,
    )[:6]


async def txn_summaries(session: AsyncSession, txn_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not txn_ids:
        return {}
    logical = await load_logical_data(session)
    by_id = {t.id: t for t in logical.transactions}
    return {
        row_id: {
            "id": txn.id,
            **_sample(txn),
            "description": txn.description,
            "account": logical.accounts[logical.account_roots[txn.account_id]].name,
        }
        for row_id in txn_ids
        if (txn := by_id.get(logical.transaction_roots.get(row_id, -1))) is not None
    }


async def review_context(session: AsyncSession, item: ReviewItem) -> dict[str, Any]:
    if item.kind == ReviewKind.account_overlap:
        accounts = {a.id: a for a in await session.scalars(select(Account))}

        def account_summary(row_id: int) -> dict[str, Any]:
            a = accounts[row_id]
            return {
                "id": a.id,
                "name": a.name,
                "org_name": a.org_name,
                "source": a.source,
                "mask": a.mask,
                "currency": a.currency,
                "type": a.type,
                "canonical_account_id": a.canonical_account_id,
            }

        source = item.payload.get("account_id")
        return {
            "account": account_summary(source)
            if type(source) is int and source in accounts
            else None,
            "candidates": [
                account_summary(c)
                for c in item.payload.get("candidates", [])
                if type(c) is int and c in accounts
            ],
            "metadata_conflicts": item.payload.get("metadata_conflicts", []),
        }
    if item.kind == ReviewKind.transaction_overlap:
        accounts = {a.id: a for a in await session.scalars(select(Account))}
        physical_txns = {t.id: t for t in await session.scalars(select(Transaction))}

        def observation(row_id: int) -> dict[str, Any]:
            t = physical_txns[row_id]
            a = accounts[t.account_id]
            return {
                "id": t.id,
                **_sample(t),
                "description": t.description,
                "account": a.name,
                "source": a.source,
                "duplicate_of_id": t.duplicate_of_id,
            }

        source = item.payload.get("transaction_id")
        return {
            "transaction": observation(source)
            if type(source) is int and source in physical_txns
            else None,
            "candidates": [
                observation(c)
                for c in item.payload.get("candidates", [])
                if type(c) is int and c in physical_txns
            ],
        }
    if item.kind == ReviewKind.transfer_pair:
        outflow_id = item.payload.get("outflow_id")
        logical = await load_logical_data(session)
        survivors = {t.id: t for t in logical.transactions}
        out_root = logical.transaction_roots.get(outflow_id) if type(outflow_id) is int else None
        out = survivors.get(out_root) if out_root is not None else None
        candidate_ids: list[int] = []
        for candidate in item.payload.get("candidates", []):
            target = logical.transaction_roots.get(candidate) if type(candidate) is int else None
            inn = survivors.get(target) if target is not None else None
            if (
                out is not None
                and inn is not None
                and out.amount_cents < 0
                and inn.amount_cents == -out.amount_cents
                and abs((out.posted_on - inn.posted_on).days) <= 6
                and logical.account_roots[out.account_id] != logical.account_roots[inn.account_id]
                and logical.accounts[logical.account_roots[out.account_id]].currency
                == logical.accounts[logical.account_roots[inn.account_id]].currency
            ):
                candidate_ids.append(candidate)
        ids = ([outflow_id] if type(outflow_id) is int else []) + candidate_ids
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
        logical = await load_logical_data(session)
        primary = await primary_currency(session)
        ref = bill_ref(item.payload)
        amount = ref[2] if ref is not None else None
        matching = [
            t
            for t in logical.transactions
            if t.merchant == merchant
            and (amount is None or amounts_match(abs(t.amount_cents), amount))
        ]
        domestic = [
            t
            for t in matching
            if logical.accounts[logical.account_roots[t.account_id]].currency == primary
        ]
        if matching and not domestic:
            return {}
        txns = sorted(
            domestic,
            key=lambda t: (t.posted_on, t.id),
            reverse=True,
        )[:6]
        return {
            "samples": [_sample(t) for t in txns],
            "direction": item.payload.get("direction"),
        }
    if item.kind == ReviewKind.merchant:
        return {
            "descriptor": item.payload.get("descriptor"),
            "suggested": item.payload.get("fallback"),
        }
    if item.kind == ReviewKind.price_change:
        logical = await load_logical_data(session)
        if not price_review_matches_currency(item, logical, await primary_currency(session)):
            return {}
        old, new = item.payload.get("old_cents"), item.payload.get("new_cents")
        series_id = item.payload.get("series_id")
        samples = (
            [_sample(t) for t in await _recent_occurrences(session, series_id)]
            if isinstance(series_id, int)
            else []
        )
        return {
            "merchant": item.payload.get("merchant"),
            "old_amount_cents": old if isinstance(old, int) else None,
            "new_amount_cents": new if isinstance(new, int) else None,
            "occurred_on": item.payload.get("occurred_on"),
            "samples": samples,
        }
    return {}


async def _human_decision_covers(session: AsyncSession, item: ReviewItem) -> bool:
    ref = bill_ref(item.payload)
    protected = human_recurring_refs(list(await session.scalars(select(ReviewItem))))
    return ref is not None and any(same_bill(ref, p) for p in protected)


async def apply_resolution(
    session: AsyncSession,
    item: ReviewItem,
    resolution: dict[str, Any],
    resolved_by: str = "manual",
) -> None:
    """Apply a resolution's effects and mark the item resolved. Caller commits."""
    if (
        item.kind == ReviewKind.recurring_cluster
        and resolved_by != "manual"
        and await _human_decision_covers(session, item)
    ):
        raise ValueError("Automatic recurring resolution cannot override a human decision")
    if item.kind in (ReviewKind.account_overlap, ReviewKind.transaction_overlap):
        key = (
            "canonical_account_id" if item.kind == ReviewKind.account_overlap else "duplicate_of_id"
        )
        if key not in resolution:
            raise ValueError(f"resolution.{key} is required (integer candidate ID or null)")
        target = resolution[key]
        candidates = item.payload.get("candidates", [])
        if target is not None and (type(target) is not int or target not in candidates):
            raise ValueError(f"resolution.{key} must be a candidate integer ID or null")
        if resolved_by != "manual":
            raise ValueError("Provider overlap decisions require human review")
        source_id = item.payload.get(
            "account_id" if item.kind == ReviewKind.account_overlap else "transaction_id"
        )
        if type(source_id) is not int:
            raise ValueError("Provider observation ID must be an integer")
        if item.kind == ReviewKind.account_overlap:
            await set_account_link(session, source_id, target)
        else:
            await set_transaction_duplicate(session, source_id, target)
    elif item.kind == ReviewKind.merchant and isinstance(resolution.get("merchant"), str):
        merchant = resolution["merchant"]
        raw = item.payload["descriptor"]
        if resolved_by != "manual":
            logical = await load_logical_data(session)
            protected = await reviewed_merchant_occurrences(session, logical.transaction_roots)
            if any(
                logical.transaction_roots[t.id] in protected
                for t in await session.scalars(
                    select(Transaction).where(Transaction.description == raw)
                )
            ):
                raise ValueError(
                    "Automatic merchant resolution cannot override a human classification"
                )
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
    elif item.kind == ReviewKind.transfer_pair and "inflow_id" in resolution:
        logical = await load_logical_data(session)
        raw_out = item.payload.get("outflow_id")
        target = resolution["inflow_id"]
        if type(raw_out) is not int or raw_out not in logical.transaction_roots:
            raise ValueError("Unknown transfer outflow")
        candidates = item.payload.get("candidates", [])
        if target is not None and (
            type(target) is not int
            or target not in candidates
            or target not in logical.transaction_roots
        ):
            raise ValueError("resolution.inflow_id must be a candidate integer ID or null")
        out_id = logical.transaction_roots[raw_out]
        in_id = logical.transaction_roots[target] if target is not None else None
        history = list(await session.scalars(select(ReviewItem)))
        choices, conflicts = effective_transfer_choices(history, logical.transaction_roots)
        manual_links = {
            logical.transaction_roots.get(link.outflow_id)
            for link in await session.scalars(
                select(TransferLink).where(TransferLink.method == LinkMethod.manual)
            )
        }
        if resolved_by != "manual" and (
            out_id in conflicts
            or out_id in manual_links
            or (out_id in choices and choices[out_id].method == LinkMethod.manual)
        ):
            raise ValueError("Automatic transfer resolution cannot override a human decision")
        if in_id is not None:
            by_id = {t.id: t for t in logical.transactions}
            out, inn = by_id[out_id], by_id[in_id]
            out_root = logical.account_roots[out.account_id]
            in_root = logical.account_roots[inn.account_id]
            if (
                out.amount_cents >= 0
                or inn.amount_cents != -out.amount_cents
                or abs((out.posted_on - inn.posted_on).days) > 6
                or out_root == in_root
                or logical.accounts[out_root].currency != logical.accounts[in_root].currency
            ):
                raise ValueError(
                    "Transfer requires distinct accounts, one currency, opposite cents "
                    "and dates within six days"
                )
        if resolved_by == "manual":
            # Explicit new adjudication supersedes the occurrence's prior answers,
            # including answers saved against a different provider observation.
            prior_ids = [
                old.id
                for old in history
                if old is not item
                and old.kind == ReviewKind.transfer_pair
                and old.status == ReviewStatus.resolved
                and old.resolution
                and "inflow_id" in old.resolution
                and logical.transaction_roots.get(old.payload.get("outflow_id", -1)) == out_id
            ]
            if prior_ids:
                item.payload = {
                    **item.payload,
                    "transfer_review_ids": sorted(set(prior_ids)),
                }
        for link in await session.scalars(select(TransferLink)):
            if logical.transaction_roots.get(link.outflow_id) == out_id:
                await session.delete(link)
        await session.flush()
        if in_id is not None:
            session.add(
                TransferLink(
                    outflow_id=out_id,
                    inflow_id=in_id,
                    confidence=1.0 if resolved_by == "manual" else 0.9,
                    method=LinkMethod.manual if resolved_by == "manual" else LinkMethod.llm,
                )
            )
    elif item.kind == ReviewKind.transfer_pair:
        raise ValueError("resolution.inflow_id is required (integer candidate ID or null)")
    elif item.kind == ReviewKind.recurring_cluster and isinstance(
        resolution.get("is_recurring"), bool
    ):
        stmt = select(RecurringSeries).where(
            RecurringSeries.merchant == item.payload.get("merchant")
        )
        direction = item.payload.get("direction")
        if direction is not None:
            stmt = stmt.where(RecurringSeries.direction == direction)
        ref = bill_ref(item.payload)
        amount = ref[2] if ref is not None else None
        for series in (await session.execute(stmt)).scalars():
            if amount is not None and not amounts_match(abs(series.expected_cents), amount):
                continue  # a per-bill answer leaves the merchant's other bills alone
            if resolution["is_recurring"] is False:
                # detection's force map suppresses future runs; end the live series now
                # so fixed costs stop counting it immediately, not after the stale sweep
                if series.status == SeriesStatus.active:
                    series.status = SeriesStatus.ended
            else:
                series.discretionary = bool(resolution.get("discretionary"))
    elif item.kind == ReviewKind.price_change and resolution.get("is_price_change") is True:
        logical = await load_logical_data(session)
        if not price_review_matches_currency(item, logical, await primary_currency(session)):
            raise ValueError("Price review currency does not match the current primary currency")
        series_row = await session.get(RecurringSeries, item.payload["series_id"])
        if series_row is not None:
            apply_price_change(
                session,
                series_row,
                item.payload["new_cents"],
                date.fromisoformat(item.payload["occurred_on"]),
            )
    elif item.kind == ReviewKind.financing_account and isinstance(
        resolution.get("financing"), bool
    ):
        logical = await load_logical_data(session)
        account_id = item.payload["account_id"]
        acct = await session.get(Account, logical.account_roots.get(account_id, account_id))
        if acct is not None:
            acct.financing_mode = resolution["financing"]
            if acct.id != account_id:
                item.payload = {**item.payload, "effective_account_id": acct.id}
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
        if type(inflow_id) is int and inflow_id in candidates:
            return {"inflow_id": inflow_id}
        return None
    if item.kind == ReviewKind.recurring_cluster:
        is_recurring = answer.get("is_recurring")
        if not isinstance(is_recurring, bool):
            return None
        resolution: dict[str, Any] = {"is_recurring": is_recurring}
        if isinstance(answer.get("discretionary"), bool):
            resolution["discretionary"] = answer["discretionary"]
        return resolution
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
        if item.kind == ReviewKind.recurring_cluster and await _human_decision_covers(
            session, item
        ):
            item.status = ReviewStatus.resolved
            item.resolution = {
                "resolved_by": "reconciliation",
                "superseded_reason": "Recurring cluster already has a human decision",
            }
            continue
        if item.kind == ReviewKind.transfer_pair:
            logical = await load_logical_data(session)
            choices, conflicts = effective_transfer_choices(
                list(await session.scalars(select(ReviewItem))), logical.transaction_roots
            )
            out_id = logical.transaction_roots.get(item.payload.get("outflow_id", -1))
            manual_links = {
                logical.transaction_roots.get(link.outflow_id)
                for link in await session.scalars(
                    select(TransferLink).where(TransferLink.method == LinkMethod.manual)
                )
            }
            if out_id in manual_links or (
                out_id in choices and choices[out_id].method == LinkMethod.manual
            ):
                item.status = ReviewStatus.resolved
                item.resolution = {
                    "resolved_by": "reconciliation",
                    "superseded_reason": (
                        "Occurrence already has an effective human transfer decision"
                    ),
                }
                continue
            if out_id in conflicts:
                continue
        context = await review_context(session, item)
        if item.kind == ReviewKind.merchant:
            prompt = _MERCHANT_PROMPT.format(
                descriptor=context.get("descriptor"), suggested=context.get("suggested")
            )
        elif item.kind == ReviewKind.transfer_pair:
            if not context.get("candidates"):
                continue
            prompt = _TRANSFER_PROMPT.format(
                outflow=_prompt_txn(context["outflow"]) if context.get("outflow") else None,
                candidates=[_prompt_txn(c) for c in context["candidates"]],
            )
        elif item.kind == ReviewKind.recurring_cluster:
            if not context:
                continue
            prompt = _RECURRING_PROMPT.format(
                merchant=item.payload.get("merchant"),
                direction=context.get("direction"),
                samples=[_prompt_txn(s) for s in context.get("samples", [])],
            )
        else:
            continue
        answer = await llm.classify_json(prompt)
        if not answer:
            continue
        resolution = _validated(item, answer)
        if resolution is None:
            continue
        if (
            item.kind == ReviewKind.transfer_pair
            and resolution["inflow_id"] is not None
            and resolution["inflow_id"] not in {c["id"] for c in context["candidates"]}
        ):
            continue
        try:
            async with session.begin_nested():
                await apply_resolution(session, item, resolution, resolved_by="llm")
        except ValueError:
            # Observations may have changed after the prompt. A classification
            # rejection leaves this item open without discarding earlier answers.
            continue
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
    seen = [
        ref
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalars()
        if (ref := bill_ref(item.payload)) is not None
    ]
    series_list = (
        (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.active)
            )
        )
        .scalars()
        .all()
    )
    logical = await load_logical_data(session)
    primary = await primary_currency(session)
    foreign_series = foreign_series_ids(logical, primary)
    for series in series_list:
        if series.id in foreign_series:
            continue
        refs = [series_ref(series)]
        _, run = newest_run(
            [
                t
                for t in logical.transactions
                if t.series_id == series.id
                and logical.accounts[logical.account_roots[t.account_id]].currency == primary
            ]
        )
        if run:
            # detection matches decisions by the bill's median, and a variable bill's
            # expected amount (its typical charge) can sit far from it
            med = round(statistics.median([abs(t.amount_cents) for t in run]))
            refs.append((series.merchant, str(series.direction), med))
        if any(same_bill(ref, r) for ref in refs for r in seen):
            continue
        occurrences = await _recent_occurrences(session, series.id)
        amounts = [abs(t.amount_cents) for t in occurrences] or [abs(series.expected_cents)]
        answer = await llm.classify_json(
            _VERIFY_PROMPT.format(
                merchant=series.merchant,
                direction=series.direction,
                cadence=series.cadence,
                expected=dollars(series.expected_cents),
                lo=min(amounts),
                med=round(statistics.median(amounts)),
                hi=max(amounts),
                samples=[_prompt_txn(_sample(t)) for t in occurrences],
            )
        )
        amount = await bill_amount(session, series)
        item = recurring_cluster_item(series.merchant, series.direction, amount)
        seen.append((series.merchant, str(series.direction), amount))
        classification = answer.get("classification") if answer else None
        if classification not in ("bill", "habit", "not_recurring"):
            classification = None
        confident = bool(answer and answer.get("confident") is True)
        if classification == "bill" and confident:
            item.status = ReviewStatus.resolved
            item.resolution = {"is_recurring": True, "resolved_by": "llm"}
            stats.verified += 1
        else:
            # never suppresses or demotes a deterministic detection on its own —
            # confident habit/not_recurring, unconfident, and malformed answers all
            # land here as a human-only flagged item recording what the LLM thought
            item.payload = {
                **item.payload,
                "llm_flagged": True,
                "llm_leaning": classification or "unparseable",
            }
            stats.flagged += 1
        session.add(item)
    await session.commit()
    return stats
