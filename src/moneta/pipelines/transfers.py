"""Greedy, confidence-ordered transfer-pair linking.

Outflows are grouped with their candidate inflows, then the groups are processed
in descending order of each group's best confidence; within a group, candidates
whose inflow has already been consumed by a higher-confidence group are dropped.
Two edge cases (design 2026-07-16 §7) are handled deliberately rather than left
to fall through silently:

- **Greedy loser**: if every one of an outflow's candidates has already been
  consumed by a higher-confidence group, the outflow has nothing left to
  auto-link. Rather than vanishing, it opens a ``transfer_pair`` ReviewItem
  carrying its ORIGINAL candidate ids (the post-filter set is empty by
  definition, so there's nothing else to show).
- **Originally-ambiguous outflows never late-auto-link**: an outflow that
  started with 2+ candidate inflows never takes the single-candidate
  auto-link fast path, even when higher-confidence groups consuming their
  own inflows happen to leave exactly one candidate standing for this one.
  Its confidence was computed against the original, more ambiguous candidate
  set, not the reduced one, so treating "last one standing" as a confident
  rule-based match would be unearned. It falls through to the same
  LLM-pick / review-item path as any other multi-candidate group instead —
  conservative and auditable; an LLM, when configured, can still resolve it
  automatically.
"""

import re
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier
from moneta.models import AccountType, LinkMethod, ReviewItem, ReviewKind, Transaction, TransferLink
from moneta.queries import account_type_map

_TRANSFER_PAT = re.compile(
    r"payment|pymt|transfer|xfer|ach|autopay|epay|billpay|deposit", re.IGNORECASE
)
_MAX_DAYS = 6
_AUTO_LINK = 0.8

_LLM_PROMPT = """An outflow transaction may be an internal transfer. Pick its matching inflow.
Outflow: {out_desc!r} on {out_date}, amount {amount}
Candidate inflows: {candidates}
Respond with JSON: {{"inflow_id": <id of the matching inflow, or null if none match>}}"""


class TransferStats(BaseModel):
    linked: int = 0
    review: int = 0


@dataclass
class _Cand:
    outflow: Transaction
    inflow: Transaction
    confidence: float


def _confidence(out: Transaction, inn: Transaction, out_type: AccountType) -> float:
    conf = 0.5
    diff = abs((inn.posted_on - out.posted_on).days)
    if diff <= 1:
        conf += 0.2
    elif diff <= 3:
        conf += 0.1
    if _TRANSFER_PAT.search(out.description) or _TRANSFER_PAT.search(inn.description):
        conf += 0.2
    if out_type in (AccountType.checking, AccountType.savings):
        conf += 0.1
    return conf


async def link_transfers(session: AsyncSession, llm: Classifier | None) -> TransferStats:
    stats = TransferStats()
    linked_ids: set[int] = set()
    for link in (await session.execute(select(TransferLink))).scalars():
        linked_ids.update((link.outflow_id, link.inflow_id))
    reviewed: set[int] = {
        item.payload["outflow_id"]
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.transfer_pair)
            )
        ).scalars()
        if "outflow_id" in item.payload
    }
    acct_types = await account_type_map(session)
    txns = (await session.execute(select(Transaction))).scalars().all()
    outflows = [t for t in txns if t.amount_cents < 0 and t.id not in linked_ids]
    inflows = [t for t in txns if t.amount_cents > 0 and t.id not in linked_ids]

    by_outflow: dict[int, list[_Cand]] = {}
    for out in outflows:
        for inn in inflows:
            if (
                inn.account_id == out.account_id
                or inn.amount_cents != -out.amount_cents
                or abs((inn.posted_on - out.posted_on).days) > _MAX_DAYS
            ):
                continue
            conf = _confidence(out, inn, acct_types[out.account_id])
            by_outflow.setdefault(out.id, []).append(_Cand(out, inn, conf))

    ordered = sorted(
        by_outflow.values(), key=lambda cands: max(c.confidence for c in cands), reverse=True
    )
    used: set[int] = set()
    for original in ordered:
        out = original[0].outflow
        cands = [c for c in original if c.inflow.id not in used and c.outflow.id not in used]
        if not cands:
            # Greedy loser: every candidate was consumed by a higher-confidence
            # group. Nothing to show from the (now-empty) filtered set, so the
            # review item carries the original candidates instead of vanishing.
            _open_review(session, out, [c.inflow.id for c in original], reviewed, stats)
            continue
        if len(original) == 1 and cands[0].confidence >= _AUTO_LINK:
            _add_link(session, cands[0], LinkMethod.rule, used)
            stats.linked += 1
            continue
        picked = await _llm_pick(llm, cands) if llm else None
        if picked is not None:
            _add_link(session, picked, LinkMethod.llm, used)
            stats.linked += 1
        else:
            _open_review(session, out, [c.inflow.id for c in cands], reviewed, stats)
    await session.commit()
    return stats


def _add_link(session: AsyncSession, cand: _Cand, method: LinkMethod, used: set[int]) -> None:
    session.add(
        TransferLink(
            outflow_id=cand.outflow.id,
            inflow_id=cand.inflow.id,
            confidence=cand.confidence,
            method=method,
        )
    )
    used.update((cand.outflow.id, cand.inflow.id))


def _open_review(
    session: AsyncSession,
    out: Transaction,
    candidate_ids: list[int],
    reviewed: set[int],
    stats: TransferStats,
) -> None:
    if out.id in reviewed:
        return
    session.add(
        ReviewItem(
            kind=ReviewKind.transfer_pair,
            question=f"Which inflow matches outflow {out.description!r} "
            f"({out.posted_on}, {out.amount_cents / 100:.2f})?",
            payload={"outflow_id": out.id, "candidates": candidate_ids},
        )
    )
    stats.review += 1


async def _llm_pick(llm: Classifier, cands: list[_Cand]) -> _Cand | None:
    out = cands[0].outflow
    answer = await llm.classify_json(
        _LLM_PROMPT.format(
            out_desc=out.description,
            out_date=out.posted_on.isoformat(),
            amount=f"{out.amount_cents / 100:.2f}",
            candidates=[
                {
                    "inflow_id": c.inflow.id,
                    "description": c.inflow.description,
                    "date": c.inflow.posted_on.isoformat(),
                }
                for c in cands
            ],
        )
    )
    if not answer or not isinstance(answer.get("inflow_id"), int):
        return None
    return next((c for c in cands if c.inflow.id == answer["inflow_id"]), None)
