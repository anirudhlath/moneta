"""SimpleFIN Bridge adapter. Protocol docs: https://www.simplefin.org/protocol.html"""

import base64
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from loguru import logger

from moneta.aggregator.base import AccountDTO, HoldingDTO, Snapshot, TransactionDTO


def _split_auth(access_url: str) -> tuple[str, tuple[str, str]]:
    parts = urlsplit(access_url)
    auth = (unquote(parts.username or ""), unquote(parts.password or ""))
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    bare = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return bare, auth


def _ts_to_date(ts: int) -> date:
    return datetime.fromtimestamp(ts).date()  # local tz: the user's calendar day


async def claim_setup_token(token: str, client: httpx.AsyncClient | None = None) -> str:
    claim_url = base64.b64decode(token).decode()
    own = client or httpx.AsyncClient()
    try:
        resp = await own.post(claim_url)
        resp.raise_for_status()
        return resp.text.strip()
    finally:
        if client is None:
            await own.aclose()


class SimpleFINAdapter:
    def __init__(self, access_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url, self._auth = _split_auth(access_url)
        self._client = client

    async def fetch(self, since: date | None = None) -> Snapshot:
        params: dict[str, Any] = {"pending": 1}
        if since is not None:
            params["start-date"] = int(
                datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp()
            )
        own = self._client or httpx.AsyncClient()
        try:
            resp = await own.get(f"{self._url}/accounts", params=params, auth=self._auth)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        finally:
            if self._client is None:
                await own.aclose()
        for err in data.get("errors", []):
            logger.warning("SimpleFIN error: {}", err)
        return _parse_snapshot(data)


def _parse_snapshot(data: dict[str, Any]) -> Snapshot:
    accounts: list[AccountDTO] = []
    transactions: list[TransactionDTO] = []
    holdings: list[HoldingDTO] = []
    for acct in data.get("accounts", []):
        accounts.append(
            AccountDTO(
                id=acct["id"],
                name=acct["name"],
                org_name=acct.get("org", {}).get("name", ""),
                currency=acct.get("currency", "USD"),
                balance=Decimal(acct["balance"]),
                balance_date=_ts_to_date(acct["balance-date"]),
            )
        )
        for txn in acct.get("transactions", []):
            if txn.get("pending"):
                continue
            transactions.append(
                TransactionDTO(
                    id=txn["id"],
                    account_id=acct["id"],
                    posted_on=_ts_to_date(txn["posted"]),
                    amount=Decimal(txn["amount"]),
                    description=txn.get("description", ""),
                    raw=txn,
                )
            )
        for h in acct.get("holdings") or []:
            holdings.append(
                HoldingDTO(
                    account_id=acct["id"],
                    symbol=h.get("symbol", h.get("description", "?")),
                    quantity=float(h.get("shares", 0)),
                    market_value=Decimal(h.get("market_value", "0")),
                )
            )
    return Snapshot(accounts=accounts, transactions=transactions, holdings=holdings)
