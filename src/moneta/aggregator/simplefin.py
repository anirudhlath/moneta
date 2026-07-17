"""SimpleFIN Bridge adapter. Protocol docs: https://www.simplefin.org/protocol.html"""

import base64
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from loguru import logger

from moneta.aggregator.base import AccountDTO, HoldingDTO, Snapshot, TransactionDTO

# The protocol has no range limit, but the beta bridge hard-caps any request to the
# trailing 90 days of the range and recommends ≤45 ("in the future, this may be
# capped") — so a deep `since` must be fetched as a backward walk of ≤45-day windows.
_WINDOW_DAYS = 45
# A windowed walk stops after >1 year (9×45=405 days) of consecutive windows yielding no
# new txns, rather than walking to `since` (which may be 1970): institutions hand the
# bridge a bounded history. >365 so an annual-only cadence can't terminate the walk.
_MAX_EMPTY_WINDOWS = 9


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


def _date_to_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


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

    @property
    def source(self) -> str:
        return "simplefin"

    async def fetch(self, since: date | None = None) -> Snapshot:
        own = self._client or httpx.AsyncClient()
        try:
            if since is None:
                return _parse_snapshot(await self._get(own, {"pending": 1}))
            snap: Snapshot | None = None
            known: set[tuple[str, str]] = set()
            empty_streak = 0
            # UTC date, not local: bridge timestamps are UTC, and a local evening date
            # would put the exclusive end-date in the past, clipping today's txns.
            # max(…, since) guarantees at least one window even for a future `since`,
            # so account balances always refresh.
            end = max(datetime.now(UTC).date(), since) + timedelta(days=1)
            while end > since and empty_streak < _MAX_EMPTY_WINDOWS:
                start = max(since, end - timedelta(days=_WINDOW_DAYS))
                logger.info("SimpleFIN: fetching {} – {}", start, end)
                window = _parse_snapshot(
                    await self._get(
                        own,
                        {
                            "pending": 1,
                            "start-date": _date_to_ts(start),
                            "end-date": _date_to_ts(end),
                        },
                    )
                )
                fresh = [t for t in window.transactions if (t.account_id, t.id) not in known]
                known.update((t.account_id, t.id) for t in fresh)
                if snap is None:
                    snap = window  # freshest window wins: current accounts/balances/holdings
                    snap.transactions = fresh
                else:
                    snap.transactions.extend(fresh)
                    snap.warnings.extend(window.warnings)
                empty_streak = 0 if fresh else empty_streak + 1
                end = start
            return snap or Snapshot(accounts=[], transactions=[], holdings=[])
        finally:
            if self._client is None:
                await own.aclose()

    async def _get(self, client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
        resp = await client.get(f"{self._url}/accounts", params=params, auth=self._auth)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        for err in data.get("errors", []):
            logger.warning("SimpleFIN error: {}", err)
        return data


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
                source="simplefin",
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
    warnings = [f"simplefin: {err}" for err in data.get("errors", [])]
    return Snapshot(
        accounts=accounts, transactions=transactions, holdings=holdings, warnings=warnings
    )
