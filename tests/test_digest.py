from collections.abc import Callable
from datetime import date

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import Account, AccountType, DigestState, TransferLink
from moneta.pipelines.digest import run_digest
from tests.factories import make_account, make_series, make_series_event, make_txn


def _fake_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_transport(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    return handle


def _failing_transport() -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    return handle


async def _at_risk_loan(
    session: AsyncSession, promo_expires_on: date, balance_cents: int = -90000
) -> Account:
    checking = await make_account(session, type=AccountType.checking)
    loan = await make_account(
        session,
        type=AccountType.loan,
        name="Synchrony CarCare",
        balance_cents=balance_cents,
        promo_expires_on=promo_expires_on,
    )
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            checking,
            amount_cents=-30000,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 5),
        )
        inn = await make_txn(
            session,
            loan,
            amount_cents=30000,
            merchant="Synchrony Bank Payment Web Id",
            posted_on=date(2026, month, 5),
        )
        session.add(
            TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule")
        )
    await session.flush()
    return loan


async def test_no_content_advances_cursor_without_sending(session: AsyncSession) -> None:
    result = await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7))
    assert result.model_dump() == {"sent": False, "events": 0, "warnings": 0}
    state = await session.get(DigestState, 1)
    assert state is not None and state.last_event_id == 0 and state.warned_account_ids == []


async def test_missing_topic_raises(session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await run_digest(session, None, today=date(2026, 7, 7))


async def test_new_event_sends_and_advances_cursor(session: AsyncSession) -> None:
    s = await make_series(session, merchant="Netflix")
    await make_series_event(session, s)  # id=1, kind=missed
    seen: list[httpx.Request] = []
    client = _fake_client(_ok_transport(seen))
    result = await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client)
    assert result.model_dump() == {"sent": True, "events": 1, "warnings": 0}
    assert len(seen) == 1
    assert seen[0].headers["title"] == "moneta digest"
    body = seen[0].content.decode()
    assert "Netflix" in body and "Missed payment" in body

    state = await session.get(DigestState, 1)
    assert state is not None and state.last_event_id == 1

    # second run: no new event, nothing sent
    seen.clear()
    result2 = await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client)
    assert result2.model_dump() == {"sent": False, "events": 0, "warnings": 0}
    assert seen == []


async def test_delivery_failure_does_not_advance_cursor(session: AsyncSession) -> None:
    s = await make_series(session, merchant="Netflix")
    await make_series_event(session, s)
    client = _fake_client(_failing_transport())
    result = await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client)
    assert result.model_dump() == {"sent": False, "events": 1, "warnings": 0}

    state = await session.get(DigestState, 1)
    # nothing committed yet on the very first (failed) run: the row is only
    # visible within this uncommitted transaction, still at its defaults
    assert state is None or state.last_event_id == 0

    # a retry with working delivery must still see and send the same event
    seen: list[httpx.Request] = []
    ok_client = _fake_client(_ok_transport(seen))
    result2 = await run_digest(
        session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=ok_client
    )
    assert result2.model_dump() == {"sent": True, "events": 1, "warnings": 0}
    assert len(seen) == 1


async def test_warned_set_adds_new_risk_and_suppresses_repeat(session: AsyncSession) -> None:
    loan = await _at_risk_loan(session, promo_expires_on=date(2026, 9, 1))
    seen: list[httpx.Request] = []
    client = _fake_client(_ok_transport(seen))
    result = await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client)
    assert result.model_dump() == {"sent": True, "events": 0, "warnings": 1}
    assert "Deferred-interest risk" in seen[0].content.decode()

    state = await session.get(DigestState, 1)
    assert state is not None and state.warned_account_ids == [loan.id]

    # second run: same risk, already warned -> no repeat notification
    seen.clear()
    result2 = await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client)
    assert result2.model_dump() == {"sent": False, "events": 0, "warnings": 0}
    assert seen == []


async def test_warned_set_removes_cleared_risk_and_renotifies(session: AsyncSession) -> None:
    loan = await _at_risk_loan(session, promo_expires_on=date(2026, 9, 1))
    seen: list[httpx.Request] = []
    client = _fake_client(_ok_transport(seen))
    await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client)
    state = await session.get(DigestState, 1)
    assert state is not None and state.warned_account_ids == [loan.id]

    # promo pushed out far enough that payoff now lands before it clears
    loan_row = await session.get(Account, loan.id)
    assert loan_row is not None
    loan_row.promo_expires_on = date(2027, 6, 1)
    await session.flush()
    seen.clear()
    cleared = await run_digest(session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client)
    assert cleared.model_dump() == {"sent": False, "events": 0, "warnings": 0}
    state = await session.get(DigestState, 1)
    assert state is not None and state.warned_account_ids == []

    # promo reverts back to at-risk -> re-notifies since it's no longer warned
    loan_row.promo_expires_on = date(2026, 9, 1)
    await session.flush()
    seen.clear()
    renotified = await run_digest(
        session, "https://ntfy.sh/x", today=date(2026, 7, 7), client=client
    )
    assert renotified.model_dump() == {"sent": True, "events": 0, "warnings": 1}
    assert len(seen) == 1
