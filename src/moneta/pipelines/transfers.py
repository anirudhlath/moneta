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
from moneta.logical import load_logical_data
from moneta.models import (
    AccountType,
    LinkMethod,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.transaction_overlap import effective_transfer_reviews

# Substring matches on purpose: descriptors glue words ("ONETIMEPAYMENT"). Only "ach"
# is a whole word, because as a substring it hides in "BEACH", "COACH" and "each".
_TRANSFER_PAT = re.compile(
    r"payment|pymt|transfer|xfer|\bach\b|autopay|epay|billpay|deposit", re.IGNORECASE
)
# A P2P rail names a person, not one of your accounts: in "Zelle payment from JANE
# DOE" the word "payment" belongs to the rail. The rail phrase is removed before the
# keyword search, so it can't vouch for a transfer on its own.
_P2P_RAILS = re.compile(
    r"zelle\s+payment\s+(?:from|to)|venmo\s+payment|paypal\s+inst\s+xfer", re.IGNORECASE
)
_MAX_DAYS = 6
# Scores are integer tenths: summed float tenths drift (0.5 + 0.2 + 0.1 is
# 0.7999999999999999), so a pair meant to score exactly the threshold never linked.
_AUTO_LINK = 8

_LLM_PROMPT = """An outflow transaction may be an internal transfer. Pick its matching inflow.
Outflow: {out_desc!r} on {out_date}, amount {amount}
Candidate inflows: {candidates}
Respond with JSON: {{"inflow_id": <id of the matching inflow, or null if none match>}}"""


class TransferStats(BaseModel):
    linked: int = 0
    review: int = 0


@dataclass(frozen=True)
class PairScore:
    """A candidate pair's score in integer tenths, and whether a transfer keyword
    appears in either leg's description."""

    tenths: int
    keyword: bool

    @property
    def auto_links(self) -> bool:
        """A rule link needs a keyword match, whatever the score."""
        return self.keyword and self.tenths >= _AUTO_LINK


def _has_keyword(description: str) -> bool:
    return _TRANSFER_PAT.search(_P2P_RAILS.sub(" ", description)) is not None


def score_pair(out: Transaction, inn: Transaction, out_type: AccountType) -> PairScore:
    """Base 5; +2 when the legs post 0-1 days apart, else +1 at 2-3 days; +2 for a
    transfer keyword; +1 when the outflow leaves a checking or savings account."""
    tenths = 5
    diff = abs((inn.posted_on - out.posted_on).days)
    if diff <= 1:
        tenths += 2
    elif diff <= 3:
        tenths += 1
    keyword = _has_keyword(out.description) or _has_keyword(inn.description)
    if keyword:
        tenths += 2
    if out_type in (AccountType.checking, AccountType.savings):
        tenths += 1
    return PairScore(tenths, keyword)


@dataclass
class _Cand:
    outflow: Transaction
    inflow: Transaction
    score: PairScore


@dataclass(frozen=True)
class EffectiveTransferChoice:
    inflow_id: int | None
    method: LinkMethod


def effective_transfer_choices(
    items: list[ReviewItem], roots: dict[int, int]
) -> tuple[dict[int, EffectiveTransferChoice], dict[int, list[ReviewItem]]]:
    """Reduce review provenance after mapping observations to the same occurrence.

    Latest human answers beat machine answers for each observation. Equal-strength
    answers on different observations must agree, or need a new human decision.
    A conflict resolution names the prior review IDs it adjudicated; old ledger
    rows remain untouched and a later, unrelated review can still raise a conflict.
    """

    def priority(item: ReviewItem) -> tuple[bool, int]:
        assert item.resolution is not None
        return item.resolution.get("resolved_by") != "llm", item.id

    groups: dict[int, list[ReviewItem]] = {}
    for row, item in effective_transfer_reviews(items).items():
        if row in roots:
            groups.setdefault(roots[row], []).append(item)
    selected: dict[int, EffectiveTransferChoice] = {}
    conflicts: dict[int, list[ReviewItem]] = {}
    for row, group in groups.items():
        strength = max(priority(item)[0] for item in group)
        strongest = [item for item in group if priority(item)[0] == strength]
        targets = {
            roots.get(item.resolution["inflow_id"], item.resolution["inflow_id"])
            if item.resolution["inflow_id"] is not None
            else None
            for item in strongest
            if item.resolution is not None
        }
        if len(targets) > 1:
            conflicts[row] = strongest
        else:
            selected[row] = EffectiveTransferChoice(
                next(iter(targets)), LinkMethod.manual if strength else LinkMethod.llm
            )
    return selected, conflicts


async def link_transfers(session: AsyncSession, llm: Classifier | None) -> TransferStats:
    stats = TransferStats()
    logical = await load_logical_data(session)
    linked_ids: set[int] = set()
    for link in (await session.execute(select(TransferLink))).scalars():
        linked_ids.update(
            logical.transaction_roots.get(i, i) for i in (link.outflow_id, link.inflow_id)
        )
    reviewed: set[int] = {
        logical.transaction_roots.get(item.payload["outflow_id"], item.payload["outflow_id"])
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.transfer_pair)
            )
        ).scalars()
        if "outflow_id" in item.payload
    }
    items = list(await session.scalars(select(ReviewItem)))
    choices, conflicts = effective_transfer_choices(items, logical.transaction_roots)
    selected = {row: choice.inflow_id for row, choice in choices.items()}
    for row, conflicting in conflicts.items():
        evidence = sorted(item.id for item in conflicting)
        if any(
            item.status == ReviewStatus.open and item.payload.get("transfer_review_ids") == evidence
            for item in items
        ):
            continue
        candidates = sorted(
            {
                logical.transaction_roots.get(
                    item.resolution["inflow_id"], item.resolution["inflow_id"]
                )
                for item in conflicting
                if item.resolution and item.resolution["inflow_id"] is not None
            }
        )
        for pending in items:
            if (
                pending.kind == ReviewKind.transfer_pair
                and pending.status == ReviewStatus.open
                and logical.transaction_roots.get(pending.payload.get("outflow_id", -1)) == row
            ):
                pending.status = ReviewStatus.resolved
                pending.resolution = {
                    "resolved_by": "reconciliation",
                    "superseded_reason": "Replaced by a review of the current conflicting choices",
                }
        pending = ReviewItem(kind=ReviewKind.transfer_pair, question="")
        session.add(pending)
        stats.review += 1
        pending.question = "Conflicting reviewed transfer choices; which is correct?"
        pending.payload = {
            "outflow_id": row,
            "candidates": candidates,
            "transfer_review_ids": evidence,
            "llm_flagged": True,
        }
    txns = logical.transactions
    outflows = [
        t
        for t in txns
        if t.amount_cents < 0
        and t.id not in linked_ids
        and t.id not in conflicts
        and not (t.id in selected and selected[t.id] is None)
    ]
    inflows = [t for t in txns if t.amount_cents > 0 and t.id not in linked_ids]

    by_outflow: dict[int, list[_Cand]] = {}
    for out in outflows:
        for inn in inflows:
            if (
                logical.account_roots[inn.account_id] == logical.account_roots[out.account_id]
                or logical.accounts[logical.account_roots[inn.account_id]].currency
                != logical.accounts[logical.account_roots[out.account_id]].currency
                or (out.id in selected and selected[out.id] != inn.id)
                or inn.amount_cents != -out.amount_cents
                or abs((inn.posted_on - out.posted_on).days) > _MAX_DAYS
            ):
                continue
            score = score_pair(
                out, inn, logical.accounts[logical.account_roots[out.account_id]].type
            )
            by_outflow.setdefault(out.id, []).append(_Cand(out, inn, score))

    # reviewed choices first, then the strongest group; equal scores are exact integer
    # ties, which the lower outflow id breaks
    ordered = sorted(
        by_outflow.values(),
        key=lambda cands: (
            cands[0].outflow.id not in selected,
            -max(c.score.tenths for c in cands),
            cands[0].outflow.id,
        ),
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
        if out.id in selected:
            manual = choices[out.id].method == LinkMethod.manual
            method = LinkMethod.manual if manual else LinkMethod.llm
            _add_link(session, cands[0], method, 10 if manual else 9, used)
            stats.linked += 1
            continue
        if len(original) == 1 and cands[0].score.auto_links:
            _add_link(session, cands[0], LinkMethod.rule, cands[0].score.tenths, used)
            stats.linked += 1
            continue
        picked = await _llm_pick(llm, cands) if llm else None
        if picked is not None:
            _add_link(session, picked, LinkMethod.llm, picked.score.tenths, used)
            stats.linked += 1
        else:
            _open_review(session, out, [c.inflow.id for c in cands], reviewed, stats)
    await session.commit()
    return stats


def _add_link(
    session: AsyncSession, cand: _Cand, method: LinkMethod, tenths: int, used: set[int]
) -> None:
    session.add(
        TransferLink(
            outflow_id=cand.outflow.id,
            inflow_id=cand.inflow.id,
            confidence=tenths / 10,
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
