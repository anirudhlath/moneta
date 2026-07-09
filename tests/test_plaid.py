import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from moneta.aggregator.base import AccountDTO, AggregatorAdapter, MergedAdapter, Snapshot
from moneta.aggregator.plaid import (
    PlaidAdapter,
    PlaidClient,
    PlaidError,
    PlaidItem,
    create_hosted_link,
    exchange_public_token,
    items_path,
    load_items,
    poll_link_result,
    save_items,
)
from moneta.models import AccountType


def _snap(account_id: str) -> Snapshot:
    return Snapshot(
        accounts=[
            AccountDTO(
                id=account_id,
                name=f"acct {account_id}",
                org_name="org",
                currency="USD",
                balance=Decimal("1.00"),
                balance_date=date(2026, 7, 1),
            )
        ],
        transactions=[],
        holdings=[],
    )


class _StubAdapter:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.seen_since: date | None = None

    async def fetch(self, since: date | None = None) -> Snapshot:
        self.seen_since = since
        return _snap(self.account_id)


async def test_merged_adapter_concatenates_and_passes_since() -> None:
    a, b = _StubAdapter("A"), _StubAdapter("B")
    adapters: list[AggregatorAdapter] = [a, b]
    merged = MergedAdapter(adapters)
    snap = await merged.fetch(since=date(2026, 1, 1))
    assert [acct.id for acct in snap.accounts] == ["A", "B"]
    assert a.seen_since == b.seen_since == date(2026, 1, 1)


def _plaid_client(handler: Callable[[httpx.Request], httpx.Response]) -> PlaidClient:
    return PlaidClient(
        "cid",
        "sec",
        "sandbox",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_post_injects_credentials_and_env_base_url() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"request_id": "r1"})

    client = _plaid_client(handle)
    data = await client.post("/accounts/get", {"access_token": "tok"})
    assert data == {"request_id": "r1"}
    assert seen["url"] == "https://sandbox.plaid.com/accounts/get"
    assert seen["body"] == {"client_id": "cid", "secret": "sec", "access_token": "tok"}


async def test_post_error_raises_plaid_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_type": "ITEM_ERROR",
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_message": "user must re-link",
            },
        )

    client = _plaid_client(handle)
    with pytest.raises(PlaidError) as exc_info:
        await client.post("/accounts/get", {"access_token": "tok"})
    assert exc_info.value.error_code == "ITEM_LOGIN_REQUIRED"
    assert exc_info.value.error_type == "ITEM_ERROR"
    assert "re-link" in str(exc_info.value)


def test_unknown_env_rejected() -> None:
    with pytest.raises(ValueError, match="sandbox"):
        PlaidClient("cid", "sec", "development")


def test_items_roundtrip_and_permissions(tmp_path: Path) -> None:
    path = items_path(tmp_path)
    assert path == tmp_path / "plaid_items.json"
    assert load_items(path) == []
    items = [
        PlaidItem(
            item_id="it-1",
            access_token="access-1",
            institution_name="Chase",
            products=["transactions", "investments"],
        )
    ]
    save_items(path, items)
    assert load_items(path) == items
    assert (path.stat().st_mode & 0o777) == 0o600


_LINK_PENDING = {"link_token": "lt-1", "link_sessions": []}
_LINK_DONE = {
    "link_token": "lt-1",
    "link_sessions": [
        {
            "finished_at": "2026-07-09T00:00:00Z",
            "results": {
                "item_add_results": [
                    {
                        "public_token": "public-1",
                        "institution": {"institution_id": "ins_3", "name": "Chase"},
                    }
                ]
            },
        }
    ],
}


async def test_create_hosted_link_payload_and_result() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "link_token": "lt-1",
                "hosted_link_url": "https://hosted.plaid.com/link/abc",
                "expiration": "2026-07-09T04:00:00Z",
            },
        )

    client = _plaid_client(handle)
    link_token, url = await create_hosted_link(client, ["transactions"])
    assert (link_token, url) == ("lt-1", "https://hosted.plaid.com/link/abc")
    body = seen["body"]
    assert body["client_name"] == "moneta"
    assert body["user"] == {"client_user_id": "moneta"}
    assert body["products"] == ["transactions"]
    assert body["country_codes"] == ["US"]
    assert body["hosted_link"] == {}
    assert body["transactions"] == {"days_requested": 730}


async def test_create_hosted_link_omits_days_without_transactions_product() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"link_token": "lt", "hosted_link_url": "u"})

    await create_hosted_link(_plaid_client(handle), ["investments"])
    assert "transactions" not in seen["body"]


async def test_poll_link_result_waits_for_completion() -> None:
    responses = [_LINK_PENDING, _LINK_PENDING, _LINK_DONE]

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    client = _plaid_client(handle)
    public_token, institution = await poll_link_result(client, "lt-1", interval=0.0)
    assert (public_token, institution) == ("public-1", "Chase")
    assert responses == []


async def test_poll_link_result_times_out() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_LINK_PENDING)

    client = _plaid_client(handle)
    with pytest.raises(TimeoutError):
        await poll_link_result(client, "lt-1", timeout=0.0, interval=0.0)


async def test_exchange_public_token() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["public_token"] == "public-1"
        return httpx.Response(200, json={"access_token": "access-1", "item_id": "it-1"})

    client = _plaid_client(handle)
    assert await exchange_public_token(client, "public-1") == ("access-1", "it-1")


_ACCOUNTS_PAYLOAD = {
    "accounts": [
        {
            "account_id": "acc-chk",
            "name": "Plaid Checking",
            "official_name": "Plaid Gold Standard Checking",
            "mask": "0000",
            "type": "depository",
            "subtype": "checking",
            "balances": {
                "current": 110.94,
                "available": 100.0,
                "iso_currency_code": "USD",
                "last_updated_datetime": "2026-07-08T22:00:00Z",
            },
        },
        {
            "account_id": "acc-card",
            "name": "Plaid Credit Card",
            "type": "credit",
            "subtype": "credit card",
            "balances": {"current": 410.0, "iso_currency_code": "USD"},
        },
        {
            "account_id": "acc-brok",
            "name": "Plaid Brokerage",
            "type": "investment",
            "subtype": "brokerage",
            "balances": {"current": None, "available": 320.76},
        },
    ],
    "item": {"item_id": "it-1", "institution_id": "ins_3", "institution_name": "Chase"},
}


def _item(products: list[str] | None = None) -> PlaidItem:
    return PlaidItem(item_id="it-1", access_token="access-1", products=products or [])


async def test_fetch_parses_accounts() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/get"
        return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)

    adapter = PlaidAdapter(_plaid_client(handle), [_item()])
    snap = await adapter.fetch()
    chk, card, brok = snap.accounts
    assert chk.id == "acc-chk"
    assert chk.name == "Plaid Checking"
    assert chk.org_name == "Chase"
    assert chk.balance == Decimal("110.94")
    assert chk.balance_date == date(2026, 7, 8)
    assert chk.type_hint == AccountType.checking
    # liability balances negated: Plaid positive-owed -> moneta negative
    assert card.balance == Decimal("-410.00")
    assert card.type_hint == AccountType.credit
    # current missing -> available fallback; investment -> brokerage
    assert brok.balance == Decimal("320.76")
    assert brok.type_hint == AccountType.brokerage
    assert snap.transactions == []
    assert snap.holdings == []


async def test_depository_non_checking_maps_to_savings_and_other_to_none() -> None:
    payload = {
        "accounts": [
            {
                "account_id": "acc-mm",
                "name": "Money Market",
                "type": "depository",
                "subtype": "money market",
                "balances": {"current": 5.0},
            },
            {
                "account_id": "acc-other",
                "name": "Mystery",
                "type": "other",
                "subtype": None,
                "balances": {"current": 5.0},
            },
        ],
        "item": {"institution_name": "Bank"},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    snap = await PlaidAdapter(_plaid_client(handle), [_item()]).fetch()
    assert snap.accounts[0].type_hint == AccountType.savings
    assert snap.accounts[1].type_hint is None
