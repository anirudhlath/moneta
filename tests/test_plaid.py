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
    PlaidClient,
    PlaidError,
    PlaidItem,
    items_path,
    load_items,
    save_items,
)


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
