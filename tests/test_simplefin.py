import base64
import json
from datetime import date
from decimal import Decimal

import httpx

from moneta.aggregator.simplefin import SimpleFINAdapter, claim_setup_token

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
    snap = await adapter.fetch()
    assert snap.accounts[0].id == "ACT-1"
    assert snap.accounts[0].org_name == "Chase"
    assert snap.accounts[0].balance == Decimal("1234.56")
    assert snap.accounts[0].balance_date == date(2025, 7, 1)
    # pending transaction skipped
    assert [t.id for t in snap.transactions] == ["TRN-1"]
    assert snap.transactions[0].amount == Decimal("-42.50")
    assert snap.transactions[0].account_id == "ACT-1"
    assert snap.holdings[0].symbol == "AAPL"
    assert snap.holdings[0].quantity == 10.5


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
