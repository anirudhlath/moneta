"""Plaid adapter. API docs: https://plaid.com/docs/api/

Two moneta-specific inversions (see the design spec §2):
- Plaid amounts are positive when money leaves the account; moneta stores
  negative = outflow, so every amount is negated.
- Plaid liability balances (credit/loan) are positive amounts owed; moneta
  stores owed balances negative (SimpleFIN convention).
"""

import asyncio
import json
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel

from moneta.aggregator.base import AccountDTO, Snapshot, TransactionDTO
from moneta.models import AccountType

_BASE_URLS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}


class PlaidError(Exception):
    def __init__(self, error_type: str, error_code: str, message: str) -> None:
        super().__init__(f"{error_code}: {message}")
        self.error_type = error_type
        self.error_code = error_code


class PlaidClient:
    def __init__(
        self,
        client_id: str,
        secret: str,
        env: str = "production",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if env not in _BASE_URLS:
            raise ValueError(f"unknown Plaid env {env!r}; expected one of {sorted(_BASE_URLS)}")
        self._base = _BASE_URLS[env]
        self._auth = {"client_id": client_id, "secret": secret}
        self._client = client

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        own = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            resp = await own.post(f"{self._base}{path}", json={**self._auth, **payload})
        finally:
            if self._client is None:
                await own.aclose()
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                raise PlaidError("API_ERROR", "UNKNOWN", resp.text) from None
            raise PlaidError(
                err.get("error_type", "API_ERROR"),
                err.get("error_code", "UNKNOWN"),
                err.get("error_message", resp.text),
            )
        data: dict[str, Any] = resp.json()
        return data


class PlaidItem(BaseModel):
    item_id: str
    access_token: str
    institution_name: str = ""
    products: list[str] = ["transactions"]


def items_path(config_dir: Path) -> Path:
    return config_dir / "plaid_items.json"


def load_items(path: Path) -> list[PlaidItem]:
    if not path.exists():
        return []
    return [PlaidItem.model_validate(x) for x in json.loads(path.read_text())]


def save_items(path: Path, items: list[PlaidItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([i.model_dump() for i in items], indent=2) + "\n")
    path.chmod(0o600)


async def create_hosted_link(
    client: PlaidClient, products: list[str], days_requested: int = 730
) -> tuple[str, str]:
    payload: dict[str, Any] = {
        "client_name": "moneta",
        "user": {"client_user_id": "moneta"},
        "products": products,
        "country_codes": ["US"],
        "language": "en",
        "hosted_link": {},
    }
    if "transactions" in products:
        payload["transactions"] = {"days_requested": days_requested}
    data = await client.post("/link/token/create", payload)
    return data["link_token"], data["hosted_link_url"]


async def poll_link_result(
    client: PlaidClient, link_token: str, timeout: float = 900.0, interval: float = 3.0
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while True:
        data = await client.post("/link/token/get", {"link_token": link_token})
        for session in data.get("link_sessions", []):
            for result in (session.get("results") or {}).get("item_add_results", []):
                institution = result.get("institution") or {}
                return result["public_token"], institution.get("name", "")
        if time.monotonic() >= deadline:
            raise TimeoutError("Plaid Link not completed in time — re-run: moneta setup plaid-link")
        await asyncio.sleep(interval)


async def exchange_public_token(client: PlaidClient, public_token: str) -> tuple[str, str]:
    data = await client.post("/item/public_token/exchange", {"public_token": public_token})
    return data["access_token"], data["item_id"]


_SYNC_PAGE_SIZE = 500
_MUTATION_RETRIES = 3
_LIABILITY_PLAID_TYPES = {"credit", "loan"}
_PLAID_TYPE_MAP = {
    "credit": AccountType.credit,
    "loan": AccountType.loan,
    "investment": AccountType.brokerage,
}


def _map_type(plaid_type: str, subtype: str | None) -> AccountType | None:
    if plaid_type == "depository":
        return AccountType.checking if subtype == "checking" else AccountType.savings
    return _PLAID_TYPE_MAP.get(plaid_type)


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _parse_account(acct: dict[str, Any], org_name: str) -> AccountDTO:
    balances = acct.get("balances") or {}
    current = balances.get("current")
    if current is None:
        current = balances.get("available") or 0
    balance = _to_decimal(current)
    if acct.get("type") in _LIABILITY_PLAID_TYPES:
        balance = -balance
    updated = balances.get("last_updated_datetime")
    balance_date = datetime.fromisoformat(updated).date() if updated else date.today()
    return AccountDTO(
        id=acct["account_id"],
        name=acct.get("name") or acct.get("official_name") or "Account",
        org_name=org_name,
        currency=balances.get("iso_currency_code") or "USD",
        balance=balance,
        balance_date=balance_date,
        type_hint=_map_type(acct.get("type", ""), acct.get("subtype")),
    )


class PlaidAdapter:
    def __init__(self, client: PlaidClient, items: list[PlaidItem]) -> None:
        self._client = client
        self._items = items

    async def fetch(self, since: date | None = None) -> Snapshot:
        # `since` is deliberately ignored: /transactions/sync replays from an empty
        # cursor every run (history capped at 730 days by the link token), and
        # ingest dedup absorbs the overlap. See design spec §3.
        snap = Snapshot(accounts=[], transactions=[], holdings=[])
        for item in self._items:
            await self._fetch_item(item, snap)
        return snap

    async def _fetch_item(self, item: PlaidItem, snap: Snapshot) -> None:
        data = await self._client.post("/accounts/get", {"access_token": item.access_token})
        org = (data.get("item") or {}).get("institution_name") or item.institution_name
        snap.accounts.extend(_parse_account(a, org) for a in data.get("accounts", []))
        if "transactions" in item.products:
            snap.transactions.extend(await self._fetch_transactions(item))

    async def _fetch_transactions(self, item: PlaidItem) -> list[TransactionDTO]:
        for attempt in range(_MUTATION_RETRIES):
            try:
                return await self._sync_pages(item)
            except PlaidError as exc:
                retryable = exc.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
                if not retryable or attempt == _MUTATION_RETRIES - 1:
                    raise
        raise AssertionError("unreachable")

    async def _sync_pages(self, item: PlaidItem) -> list[TransactionDTO]:
        txns: list[TransactionDTO] = []
        cursor = ""
        while True:
            data = await self._client.post(
                "/transactions/sync",
                {"access_token": item.access_token, "cursor": cursor, "count": _SYNC_PAGE_SIZE},
            )
            for txn in data.get("added", []):
                if txn.get("pending"):
                    continue
                txns.append(
                    TransactionDTO(
                        id=txn["transaction_id"],
                        account_id=txn["account_id"],
                        posted_on=date.fromisoformat(txn["date"]),
                        # Plaid: positive = money out; moneta: negative = outflow
                        amount=-_to_decimal(txn["amount"]),
                        description=txn.get("name") or "",
                        raw=txn,
                    )
                )
            cursor = data.get("next_cursor", "")
            if not data.get("has_more"):
                break
        if data.get("transactions_update_status") == "NOT_READY":
            logger.info(
                "Plaid item {}: transaction history still preparing; next sync picks it up",
                item.institution_name or item.item_id,
            )
        return txns
