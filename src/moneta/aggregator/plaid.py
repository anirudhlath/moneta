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
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

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
