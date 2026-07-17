import base64
import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from moneta.aggregator.simplefin import (
    SimpleFINAdapter,
    _split_auth,
    _ts_to_date,
    claim_setup_token,
)

SIMPLEFIN_PAYLOAD = {
    "errors": [],
    "accounts": [
        {
            "org": {"name": "Chase"},
            "id": "ACT-1",
            "name": "Checking",
            "currency": "USD",
            "balance": "1234.56",
            "balance-date": 1751328000,  # 2025-07-01 UTC
            "transactions": [
                {
                    "id": "TRN-1",
                    "posted": 1751414400,
                    "amount": "-42.50",
                    "description": "NETFLIX.COM",
                    "pending": False,
                },
                {
                    "id": "TRN-2",
                    "posted": 1751414400,
                    "amount": "-10.00",
                    "description": "PENDING THING",
                    "pending": True,
                },
            ],
            "holdings": [
                {
                    "id": "H-1",
                    "symbol": "AAPL",
                    "shares": "10.5",
                    "market_value": "2000.00",
                    "description": "Apple Inc",
                }
            ],
        }
    ],
}


def _mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_fetch_parses_snapshot() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/accounts")
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json=SIMPLEFIN_PAYLOAD)

    adapter = SimpleFINAdapter(
        "https://user:pass@bridge.example/simplefin",
        client=_mock_client(httpx.MockTransport(handle)),
    )
    assert adapter.source == "simplefin"
    snap = await adapter.fetch()
    assert snap.accounts[0].id == "ACT-1"
    assert snap.accounts[0].org_name == "Chase"
    assert snap.accounts[0].balance == Decimal("1234.56")
    assert snap.accounts[0].balance_date == date(2025, 7, 1)
    assert snap.accounts[0].source == "simplefin"
    # pending transaction skipped
    assert [t.id for t in snap.transactions] == ["TRN-1"]
    assert snap.transactions[0].amount == Decimal("-42.50")
    assert snap.transactions[0].account_id == "ACT-1"
    assert snap.holdings[0].symbol == "AAPL"
    assert snap.warnings == []
    assert snap.holdings[0].quantity == 10.5


async def test_bridge_errors_surface_as_warnings() -> None:
    payload = json.loads(json.dumps(SIMPLEFIN_PAYLOAD))
    payload["errors"] = ["ACT-1: re-authenticate at the institution"]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = SimpleFINAdapter(
        "https://u:p@bridge.example/simplefin",
        client=_mock_client(httpx.MockTransport(handle)),
    )
    snap = await adapter.fetch()
    assert snap.warnings == ["simplefin: ACT-1: re-authenticate at the institution"]


async def test_fetch_since_sends_start_date() -> None:
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"errors": [], "accounts": []})

    adapter = SimpleFINAdapter(
        "https://u:p@bridge.example/simplefin",
        client=_mock_client(httpx.MockTransport(handle)),
    )
    await adapter.fetch(since=date(2026, 4, 1))
    assert "start-date" in seen and int(seen["start-date"]) > 0


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _utc_today() -> date:
    """The adapter anchors windows on the UTC date — tests must use the same anchor."""
    return datetime.now(UTC).date()


def _windowed_bridge(
    today: date, txn_days_ago: list[int], balances: list[str], honor_window: bool = True
) -> tuple[httpx.AsyncClient, list[tuple[int, int]]]:
    """Fake bridge holding one account whose txns sit at the given days-ago offsets.

    Serves only the txns inside each request's [start-date, end-date) window (all of
    them regardless, when honor_window is False — a sloppy bridge) and records every
    requested window. Balance comes from `balances` per request (first response =
    freshest), repeating the last entry once exhausted.
    """
    requests: list[tuple[int, int]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        start, end = int(params["start-date"]), int(params["end-date"])
        requests.append((start, end))
        txns = [
            {
                "id": f"TRN-{days}",
                "posted": _ts(today - timedelta(days=days)),
                "amount": "-10.00",
                "description": f"CHARGE {days}",
                "pending": False,
            }
            for days in txn_days_ago
            if not honor_window or start <= _ts(today - timedelta(days=days)) < end
        ]
        balance = balances[min(len(requests) - 1, len(balances) - 1)]
        payload: dict[str, Any] = {
            "errors": [],
            "accounts": [
                {
                    "org": {"name": "Chase"},
                    "id": "ACT-1",
                    "name": "Checking",
                    "currency": "USD",
                    "balance": balance,
                    "balance-date": _ts(today),
                    "transactions": txns,
                }
            ],
        }
        return httpx.Response(200, json=payload)

    return _mock_client(httpx.MockTransport(handle)), requests


async def test_deep_since_windows_requests_and_merges() -> None:
    """The bridge caps ranges at 90d (recommends 45d); a deep pull must walk windows."""
    today = _utc_today()
    client, requests = _windowed_bridge(today, [10, 60, 100], balances=["100.00", "999.99"])
    adapter = SimpleFINAdapter("https://u:p@bridge.example/simplefin", client=client)
    snap = await adapter.fetch(since=today - timedelta(days=120))
    # all three txns retrieved even though they span >90 days
    assert sorted(t.id for t in snap.transactions) == ["TRN-10", "TRN-100", "TRN-60"]
    # windows: [t-44, t+1), [t-89, t-44), [t-120, t-89) — each ≤45 days, walking back to since
    assert len(requests) == 3
    assert requests[0] == (_ts(today - timedelta(days=44)), _ts(today + timedelta(days=1)))
    assert requests[1] == (_ts(today - timedelta(days=89)), _ts(today - timedelta(days=44)))
    assert requests[2] == (_ts(today - timedelta(days=120)), _ts(today - timedelta(days=89)))
    # accounts/balances come from the first (freshest) window
    assert snap.accounts[0].balance == Decimal("100.00")
    assert len(snap.accounts) == 1


async def test_fetch_logs_each_window_at_info() -> None:
    """A deep pull walks dozens of windows silently otherwise — sync progress
    feedback (design 2026-07-16 §5) reads these INFO lines."""
    from loguru import logger

    today = _utc_today()
    client, _requests = _windowed_bridge(today, [10, 60, 100], balances=["100.00"])
    adapter = SimpleFINAdapter("https://u:p@bridge.example/simplefin", client=client)

    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="INFO")
    try:
        await adapter.fetch(since=today - timedelta(days=120))
    finally:
        logger.remove(sink_id)

    fetch_lines = [m for m in messages if m.startswith("SimpleFIN: fetching")]
    assert len(fetch_lines) == 3  # one per window, matching the 3 requests made


async def test_epoch_since_stops_after_empty_window_streak() -> None:
    """An epoch pull must not walk to 1970 — stop once >1 year of windows comes back empty."""
    client, requests = _windowed_bridge(_utc_today(), [10], balances=["100.00"])
    adapter = SimpleFINAdapter("https://u:p@bridge.example/simplefin", client=client)
    snap = await adapter.fetch(since=date(1970, 1, 1))
    assert [t.id for t in snap.transactions] == ["TRN-10"]
    assert len(requests) == 10  # 1 window with data + 9 empty (>1 year) → stop


async def test_recent_since_is_a_single_request() -> None:
    today = _utc_today()
    client, requests = _windowed_bridge(today, [3], balances=["100.00"])
    adapter = SimpleFINAdapter("https://u:p@bridge.example/simplefin", client=client)
    snap = await adapter.fetch(since=today - timedelta(days=7))
    assert [t.id for t in snap.transactions] == ["TRN-3"]
    assert requests == [(_ts(today - timedelta(days=7)), _ts(today + timedelta(days=1)))]


async def test_future_since_still_refreshes_accounts() -> None:
    """Clock skew / post-dated txns can push `since` past today — balances must still land."""
    today = _utc_today()
    client, requests = _windowed_bridge(today, [], balances=["100.00"])
    adapter = SimpleFINAdapter("https://u:p@bridge.example/simplefin", client=client)
    snap = await adapter.fetch(since=today + timedelta(days=10))
    assert len(requests) == 1
    assert snap.accounts and snap.accounts[0].balance == Decimal("100.00")


async def test_bridge_ignoring_end_date_does_not_duplicate() -> None:
    """A sloppy bridge returning the same txns for every window must not produce dupes."""
    today = _utc_today()
    client, requests = _windowed_bridge(today, [5], balances=["100.00"], honor_window=False)
    adapter = SimpleFINAdapter("https://u:p@bridge.example/simplefin", client=client)
    snap = await adapter.fetch(since=today - timedelta(days=120))
    assert [t.id for t in snap.transactions] == ["TRN-5"]
    assert len(requests) == 3  # dupe-only windows count as empty; walk still reaches since


async def test_deep_since_warnings_merge_across_windows() -> None:
    """A bridge error on a non-freshest window must still surface, not get dropped
    when only the freshest window's Snapshot object is kept."""
    today = _utc_today()
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload: dict[str, Any] = {
            "errors": ["window error"] if calls["n"] == 2 else [],
            "accounts": [
                {
                    "org": {"name": "Chase"},
                    "id": "ACT-1",
                    "name": "Checking",
                    "currency": "USD",
                    "balance": "1.00",
                    "balance-date": _ts(today),
                    "transactions": [],
                }
            ],
        }
        return httpx.Response(200, json=payload)

    adapter = SimpleFINAdapter(
        "https://u:p@bridge.example/simplefin", client=_mock_client(httpx.MockTransport(handle))
    )
    snap = await adapter.fetch(since=today - timedelta(days=120))
    assert calls["n"] == 3  # 3 windows to walk from today back to `since`
    assert snap.warnings == ["simplefin: window error"]


async def test_claim_setup_token() -> None:
    claim_url = "https://bridge.example/claim/DEMO"
    token = base64.b64encode(claim_url.encode()).decode()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == claim_url
        return httpx.Response(200, text="https://u:p@bridge.example/simplefin")

    access = await claim_setup_token(token, client=_mock_client(httpx.MockTransport(handle)))
    assert access == "https://u:p@bridge.example/simplefin"


async def test_missing_holdings_key_ok() -> None:
    payload = json.loads(json.dumps(SIMPLEFIN_PAYLOAD))
    del payload["accounts"][0]["holdings"]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = SimpleFINAdapter(
        "https://u:p@bridge.example/simplefin",
        client=_mock_client(httpx.MockTransport(handle)),
    )
    snap = await adapter.fetch()
    assert snap.holdings == []


def test_split_auth_percent_decodes_credentials() -> None:
    bare, auth = _split_auth("https://user%40x:p%23ss@bridge.example/simplefin")
    assert auth == ("user@x", "p#ss")
    assert bare == "https://bridge.example/simplefin"


def test_ts_to_date_uses_local_timezone() -> None:
    os.environ["TZ"] = "America/Los_Angeles"  # the autouse fixture restores TZ afterwards
    time.tzset()
    # 1782963000 == 2026-07-02T03:30:00Z == 2026-07-01 20:30 in Los Angeles
    assert _ts_to_date(1782963000) == date(2026, 7, 1)


def test_ts_to_date_utc() -> None:  # TZ pinned to UTC by the autouse fixture
    assert _ts_to_date(1782963000) == date(2026, 7, 2)
