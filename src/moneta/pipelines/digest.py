"""Notifications digest (design 2026-07-16 §1): pushes new series events and
newly-at-risk financing obligations to an ntfy.sh topic, tracked by a
persisted cursor (`digest_state`).

A pipeline: it commits. "Nothing new" sends no notification (no empty pings)
but still advances the event cursor past what's been seen. A delivery
failure degrades to a warning and leaves the cursor untouched so nothing is
lost — the same content is re-attempted next run.
"""

from datetime import date

import httpx
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import DigestState, EventKind, RecurringSeries, SeriesEvent, dollars
from moneta.views.financing import Obligation, compute_obligations

_TIMEOUT = 5.0
_TITLE = "moneta digest"


class DigestResult(BaseModel):
    sent: bool
    events: int
    warnings: int


async def _state(session: AsyncSession) -> DigestState:
    state = await session.get(DigestState, 1)
    if state is None:
        state = DigestState(id=1, last_event_id=0, warned_account_ids=[])
        session.add(state)
        await session.flush()
    return state


def _event_line(kind: str, merchant: str, occurred_on: date, details: dict[str, object]) -> str:
    """Prose rendering matching `recurring --events` — `dollars()` is fine here (prose only)."""
    if kind == EventKind.missed:
        return f"Missed payment: {merchant!r} expected {occurred_on.isoformat()}"
    if kind == EventKind.price_increase:
        old, new = details.get("old_cents"), details.get("new_cents")
        old_s = f"${dollars(old)}" if isinstance(old, int) else "?"
        new_s = f"${dollars(new)}" if isinstance(new, int) else "?"
        return f"Price increase: {merchant!r} {old_s} -> {new_s} on {occurred_on.isoformat()}"
    return f"New recurring series: {merchant!r} on {occurred_on.isoformat()}"


def _obligation_line(ob: Obligation) -> str:
    payoff = ob.payoff_estimate.isoformat() if ob.payoff_estimate else "?"
    promo = ob.promo_expires_on.isoformat() if ob.promo_expires_on else "?"
    return (
        f"Deferred-interest risk: {ob.account_name!r} payoff est. {payoff} "
        f"is after promo ends {promo}"
    )


async def run_digest(
    session: AsyncSession,
    ntfy_topic: str | None,
    today: date,
    client: httpx.AsyncClient | None = None,
) -> DigestResult:
    # an unset topic must be rejected by the caller (/digest 400s with a setup
    # hint); a silent no-op here would swallow that hint entirely
    if not ntfy_topic:
        raise ValueError("run_digest requires ntfy_topic")

    state = await _state(session)
    rows = (
        await session.execute(
            select(SeriesEvent, RecurringSeries.merchant)
            .outerjoin(RecurringSeries, SeriesEvent.series_id == RecurringSeries.id)
            .where(SeriesEvent.id > state.last_event_id)
            .order_by(SeriesEvent.id)
        )
    ).all()
    max_id = (await session.execute(select(func.max(SeriesEvent.id)))).scalar_one_or_none()
    max_id = max_id if max_id is not None else state.last_event_id

    obligations = await compute_obligations(session, today)
    at_risk_ids = {ob.account_id for ob in obligations if ob.deferred_interest_risk}
    warned_before = set(state.warned_account_ids)
    new_risk = [
        ob for ob in obligations if ob.deferred_interest_risk and ob.account_id not in warned_before
    ]

    lines = [
        _event_line(e.kind, merchant or f"series {e.series_id}", e.occurred_on, e.details)
        for e, merchant in rows
    ]
    lines.extend(_obligation_line(ob) for ob in new_risk)

    if not lines:
        # nothing to notify — cursor still advances past nothing, and a
        # cleared risk (payoff before promo) still drops out of the warned set
        state.last_event_id = max_id
        state.warned_account_ids = sorted(at_risk_ids)
        await session.commit()
        return DigestResult(sent=False, events=0, warnings=0)

    body = "\n".join(lines)
    own = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        try:
            resp = await own.post(ntfy_topic, content=body, headers={"Title": _TITLE})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("digest: delivery to {} failed: {}", ntfy_topic, exc)
            # not delivered — the cursor/warned set must NOT advance, or these
            # events/warnings would silently never be sent
            return DigestResult(sent=False, events=len(rows), warnings=len(new_risk))
    finally:
        if client is None:
            await own.aclose()

    state.last_event_id = max_id
    state.warned_account_ids = sorted(at_risk_ids)
    await session.commit()
    return DigestResult(sent=True, events=len(rows), warnings=len(new_risk))
