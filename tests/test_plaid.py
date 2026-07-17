import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

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


def _plaid_client(handler: Callable[[httpx.Request], httpx.Response]) -> PlaidClient:
    return PlaidClient(
        "cid",
        "sec",
        "sandbox",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _plaid_error(error_type: str, code: str, msg: str = "err") -> httpx.Response:
    return httpx.Response(
        400, json={"error_type": error_type, "error_code": code, "error_message": msg}
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
        return _plaid_error("ITEM_ERROR", "ITEM_LOGIN_REQUIRED", "user must re-link")

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


async def test_create_hosted_link_includes_access_token_when_passed() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"link_token": "lt-1", "hosted_link_url": "u"})

    client = _plaid_client(handle)
    await create_hosted_link(client, ["transactions"], access_token="access-1")
    assert seen["body"]["access_token"] == "access-1"


async def test_create_hosted_link_omits_access_token_when_absent() -> None:
    seen: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"link_token": "lt-1", "hosted_link_url": "u"})

    client = _plaid_client(handle)
    await create_hosted_link(client, ["transactions"])
    assert "access_token" not in seen["body"]


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
    assert adapter.source == "plaid"
    snap = await adapter.fetch()
    chk, card, brok = snap.accounts
    assert chk.id == "acc-chk"
    assert chk.name == "Plaid Checking"
    assert chk.org_name == "Chase"
    assert chk.balance == Decimal("110.94")
    assert chk.balance_date == date(2026, 7, 8)
    assert chk.type_hint == AccountType.checking
    assert chk.source == "plaid" and card.source == "plaid" and brok.source == "plaid"
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


def _txn(txn_id: str, amount: float, pending: bool = False) -> dict[str, Any]:
    return {
        "transaction_id": txn_id,
        "account_id": "acc-chk",
        "amount": amount,
        "iso_currency_code": "USD",
        "date": "2026-07-01",
        "name": f"RAW DESCRIPTOR {txn_id}",
        "merchant_name": "Clean Name",
        "pending": pending,
    }


def _sync_page(
    added: list[dict[str, Any]], has_more: bool, status: str = "HISTORICAL_UPDATE_COMPLETE"
) -> dict[str, Any]:
    return {
        "added": added,
        "modified": [],
        "removed": [],
        "next_cursor": "cur-next",
        "has_more": has_more,
        "transactions_update_status": status,
    }


def _accounts_then_sync(
    sync_responses: list[httpx.Response],
    sync_bodies: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)
        assert request.url.path == "/transactions/sync"
        if sync_bodies is not None:
            sync_bodies.append(json.loads(request.content))
        return sync_responses.pop(0)

    return handle


async def test_fetch_transactions_paginates_negates_and_skips_pending() -> None:
    pages = [
        httpx.Response(
            200, json=_sync_page([_txn("t1", 12.5), _txn("t2", 3.0, pending=True)], True)
        ),
        httpx.Response(200, json=_sync_page([_txn("t3", -1000.0)], False)),
    ]
    bodies: list[dict[str, Any]] = []
    adapter = PlaidAdapter(
        _plaid_client(_accounts_then_sync(pages, bodies)), [_item(["transactions"])]
    )
    snap = await adapter.fetch()
    assert [t.id for t in snap.transactions] == ["t1", "t3"]
    # Plaid positive = outflow -> moneta negative; deposits flip positive
    assert snap.transactions[0].amount == Decimal("-12.5")
    assert snap.transactions[1].amount == Decimal("1000")
    assert snap.transactions[0].description == "RAW DESCRIPTOR t1"
    assert snap.transactions[0].posted_on == date(2026, 7, 1)
    assert snap.transactions[0].raw["merchant_name"] == "Clean Name"
    assert [b["cursor"] for b in bodies] == ["", "cur-next"]
    assert all(b["count"] == 500 for b in bodies)


def _mutation_error() -> httpx.Response:
    return _plaid_error("TRANSACTIONS_ERROR", "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")


async def test_mutation_during_pagination_restarts_cleanly() -> None:
    pages = [
        httpx.Response(200, json=_sync_page([_txn("t1", 1.0)], True)),
        _mutation_error(),
        httpx.Response(200, json=_sync_page([_txn("t1", 1.0)], True)),
        httpx.Response(200, json=_sync_page([_txn("t2", 2.0)], False)),
    ]
    bodies: list[dict[str, Any]] = []
    adapter = PlaidAdapter(
        _plaid_client(_accounts_then_sync(pages, bodies)), [_item(["transactions"])]
    )
    snap = await adapter.fetch()
    # restart discarded the partial first attempt: no duplicate t1
    assert [t.id for t in snap.transactions] == ["t1", "t2"]
    # and each retry restarted from the first page, not the stale cursor
    assert [b["cursor"] for b in bodies] == ["", "cur-next", "", "cur-next"]


async def test_mutation_forever_raises_after_bounded_retries() -> None:
    pages = [_mutation_error(), _mutation_error(), _mutation_error()]
    adapter = PlaidAdapter(_plaid_client(_accounts_then_sync(pages)), [_item(["transactions"])])
    with pytest.raises(PlaidError):
        await adapter.fetch()
    assert pages == []


async def test_not_ready_status_is_not_fatal() -> None:
    pages = [httpx.Response(200, json=_sync_page([], False, status="NOT_READY"))]
    adapter = PlaidAdapter(_plaid_client(_accounts_then_sync(pages)), [_item(["transactions"])])
    snap = await adapter.fetch()
    assert snap.transactions == []


_HOLDINGS_PAYLOAD = {
    "holdings": [
        {
            "account_id": "acc-brok",
            "security_id": "sec-1",
            "quantity": 10.5,
            "institution_value": 2000.0,
        },
        {
            "account_id": "acc-brok",
            "security_id": "sec-2",
            "quantity": 1.0,
            "institution_value": 50.0,
        },
    ],
    "securities": [
        {"security_id": "sec-1", "ticker_symbol": "AAPL", "name": "Apple Inc"},
        {"security_id": "sec-2", "ticker_symbol": None, "name": "Mystery Fund"},
    ],
}


async def test_fetch_holdings_with_security_lookup() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)
        assert request.url.path == "/investments/holdings/get"
        return httpx.Response(200, json=_HOLDINGS_PAYLOAD)

    adapter = PlaidAdapter(_plaid_client(handle), [_item(["investments"])])
    snap = await adapter.fetch()
    assert [(h.symbol, h.quantity) for h in snap.holdings] == [
        ("AAPL", 10.5),
        ("Mystery Fund", 1.0),
    ]
    assert snap.holdings[0].market_value == Decimal("2000")
    assert snap.holdings[0].account_id == "acc-brok"


async def test_holdings_product_errors_degrade_to_empty() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)
        return _plaid_error("ITEM_ERROR", "NO_INVESTMENT_ACCOUNTS")

    snap = await PlaidAdapter(_plaid_client(handle), [_item(["investments"])]).fetch()
    assert snap.holdings == []
    assert len(snap.accounts) == 3  # accounts still ingested


async def test_item_login_required_skips_item_but_not_sync() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["access_token"]
        if token == "access-dead":
            return httpx.Response(
                400,
                json={
                    "error_type": "ITEM_ERROR",
                    "error_code": "ITEM_LOGIN_REQUIRED",
                    "error_message": "re-link",
                },
            )
        return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)

    dead = PlaidItem(item_id="it-dead", access_token="access-dead", institution_name="Old Bank")
    alive = PlaidItem(item_id="it-1", access_token="access-1", products=[])
    snap = await PlaidAdapter(_plaid_client(handle), [dead, alive]).fetch()
    assert len(snap.accounts) == 3  # only the healthy item's accounts
    assert snap.warnings == [
        "Plaid item Old Bank skipped (ITEM_LOGIN_REQUIRED: re-link)"
        " — repair with: moneta setup plaid-relink it-dead"
    ]


async def test_other_plaid_errors_propagate() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _plaid_error("INVALID_INPUT", "INVALID_API_KEYS", "bad keys")

    with pytest.raises(PlaidError):
        await PlaidAdapter(_plaid_client(handle), [_item()]).fetch()


async def test_login_required_mid_item_drops_partial_accounts() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)
        return httpx.Response(
            400,
            json={
                "error_type": "ITEM_ERROR",
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_message": "re-link",
            },
        )

    snap = await PlaidAdapter(_plaid_client(handle), [_item(["transactions"])]).fetch()
    # all-or-nothing per item: the cached /accounts/get result must not survive
    # the failed /transactions/sync
    assert snap.accounts == []
    assert snap.transactions == []


async def test_liability_null_current_defaults_to_zero_not_available() -> None:
    payload = {
        "accounts": [
            {
                "account_id": "acc-paid-off",
                "name": "Paid Off Card",
                "type": "credit",
                "subtype": "credit card",
                # available on credit accounts is the remaining credit line
                "balances": {"current": None, "available": 3000.0},
            }
        ],
        "item": {"institution_name": "Bank"},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    snap = await PlaidAdapter(_plaid_client(handle), [_item()]).fetch()
    assert snap.accounts[0].balance == Decimal("0")


def test_load_items_corrupt_file_raises_clean_error(tmp_path: Path) -> None:
    path = items_path(tmp_path)
    path.write_text("{not json")
    with pytest.raises(ValueError, match="plaid-link"):
        load_items(path)


def test_save_items_leaves_no_tmp_file(tmp_path: Path) -> None:
    path = items_path(tmp_path)
    save_items(path, [PlaidItem(item_id="it-1", access_token="a")])
    assert [p.name for p in tmp_path.iterdir()] == ["plaid_items.json"]
