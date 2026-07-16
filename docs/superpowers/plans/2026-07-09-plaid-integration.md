# Plaid Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Plaid as a second aggregator (Hosted Link connect flow, `/transactions/sync` full-replay fetch) coexisting with SimpleFIN behind the existing `AggregatorAdapter` protocol.

**Architecture:** New `aggregator/plaid.py` holds a thin async `PlaidClient` (raw httpx, no SDK), a JSON item store (`plaid_items.json` in the config dir), Hosted Link helpers used by the CLI, and `PlaidAdapter` implementing `fetch(since) -> Snapshot`. A `MergedAdapter` composite in `aggregator/base.py` lets `run_sync` keep seeing exactly one adapter. Pipelines/views are untouched except a one-line ingest change to prefer Plaid's real account types over keyword inference.

**Tech Stack:** Python 3.13, httpx, pydantic v2, typer, pytest (`httpx.MockTransport`, no network).

**Spec:** `docs/superpowers/specs/2026-07-09-plaid-integration-design.md`

## Global Constraints

- Verification before every commit: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests` — all four pass, output pristine (a deprecation warning is a failure).
- Money is integer cents in the DB; `Decimal` only at boundaries. Parse Plaid's JSON floats via `Decimal(str(x))` — never `Decimal(float)`.
- Sign convention: moneta stores negative = outflow. **Plaid reports the inverse (positive = money out) — negate every transaction amount.** Liability (`credit`/`loan`) balances: Plaid positive-owed → store negated (SimpleFIN convention).
- LLM boundary: no LLM involvement anywhere in this feature.
- Tests must not hit the network: inject `httpx.AsyncClient(transport=httpx.MockTransport(...))`.
- House test style: `def test_x(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]`.
- Plaid API base URLs: `https://sandbox.plaid.com`, `https://production.plaid.com` (only two envs exist).

---

### Task 1: `AccountDTO.type_hint` + ingest precedence

Plaid supplies real account types; SimpleFIN doesn't. Give `AccountDTO` an optional
`type_hint` that ingest prefers over keyword inference.

**Files:**
- Modify: `src/moneta/aggregator/base.py` (AccountDTO)
- Modify: `src/moneta/pipelines/ingest.py:45`
- Test: `tests/test_ingest.py` (append)

**Interfaces:**
- Produces: `AccountDTO.type_hint: AccountType | None = None` (default None — SimpleFIN untouched); new accounts get `type_hint or infer_account_type(...)`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_ingest.py` (it already imports `ingest_snapshot`, `Snapshot`, `AccountDTO`-building helpers; follow its existing fixture style for a session — check top of file for the `session` fixture from `tests/conftest.py`):

```python
async def test_type_hint_beats_keyword_inference(session: AsyncSession) -> None:
    snap = Snapshot(
        accounts=[
            AccountDTO(
                id="plaid-1",
                name="Totally Ambiguous Name",
                org_name="Nowhere Bank",
                currency="USD",
                balance=Decimal("10.00"),
                balance_date=date(2026, 7, 1),
                type_hint=AccountType.credit,
            )
        ],
        transactions=[],
        holdings=[],
    )
    await ingest_snapshot(session, snap)
    acct = (await session.execute(select(Account))).scalar_one()
    assert acct.type == AccountType.credit


async def test_no_type_hint_falls_back_to_inference(session: AsyncSession) -> None:
    snap = Snapshot(
        accounts=[
            AccountDTO(
                id="sfin-1",
                name="Premier Checking",
                org_name="Chase",
                currency="USD",
                balance=Decimal("10.00"),
                balance_date=date(2026, 7, 1),
            )
        ],
        transactions=[],
        holdings=[],
    )
    await ingest_snapshot(session, snap)
    acct = (await session.execute(select(Account))).scalar_one()
    assert acct.type == AccountType.checking
```

No new imports needed — `tests/test_ingest.py` already imports `AccountDTO`, `Snapshot`, `AccountType`, `Account`, `select`, `Decimal`, `date`, and `AsyncSession`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ingest.py -q`
Expected: FAIL — `AccountDTO` has no field `type_hint` (pydantic ValidationError).

- [ ] **Step 3: Implement** — in `src/moneta/aggregator/base.py` add the import and field:

```python
from moneta.models import AccountType


class AccountDTO(BaseModel):
    id: str
    name: str
    org_name: str
    currency: str
    balance: Decimal
    balance_date: date
    type_hint: AccountType | None = None
```

In `src/moneta/pipelines/ingest.py`, the `Account(...)` construction changes one line:

```python
                type=dto.type_hint or infer_account_type(dto.name, dto.org_name),
```

- [ ] **Step 4: Run full verification**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/aggregator/base.py src/moneta/pipelines/ingest.py tests/test_ingest.py
git commit -m "feat: aggregator-supplied account type hint wins over keyword inference"
```

---

### Task 2: `MergedAdapter` composite

**Files:**
- Modify: `src/moneta/aggregator/base.py`
- Test: `tests/test_plaid.py` (create)

**Interfaces:**
- Produces: `MergedAdapter(adapters: Sequence[AggregatorAdapter])` with `async fetch(since: date | None = None) -> Snapshot` — concatenated accounts/transactions/holdings, `since` passed through to every child.

- [ ] **Step 1: Write the failing test** — create `tests/test_plaid.py`:

```python
from datetime import date
from decimal import Decimal

from moneta.aggregator.base import AccountDTO, AggregatorAdapter, MergedAdapter, Snapshot


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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plaid.py -q`
Expected: FAIL — `ImportError: cannot import name 'MergedAdapter'`.

- [ ] **Step 3: Implement** — append to `src/moneta/aggregator/base.py`:

```python
import asyncio
from collections.abc import Sequence


class MergedAdapter:
    """Fans fetch() out to several adapters and concatenates their snapshots."""

    def __init__(self, adapters: Sequence[AggregatorAdapter]) -> None:
        self._adapters = list(adapters)

    async def fetch(self, since: date | None = None) -> Snapshot:
        snaps = await asyncio.gather(*(a.fetch(since) for a in self._adapters))
        return Snapshot(
            accounts=[a for s in snaps for a in s.accounts],
            transactions=[t for s in snaps for t in s.transactions],
            holdings=[h for s in snaps for h in s.holdings],
        )
```

(Imports go at the top of the file, ruff `I` will enforce order.)

- [ ] **Step 4: Run full verification** — same four commands, all pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/aggregator/base.py tests/test_plaid.py
git commit -m "feat: MergedAdapter composite so several aggregators sync as one"
```

---

### Task 3: Plaid settings in config

**Files:**
- Modify: `src/moneta/config.py`
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Produces: `Settings.plaid_client_id: str | None`, `Settings.plaid_secret: str | None`, `Settings.plaid_env: str` (default `"production"`); all overridable via `MONETA_PLAID_*` env vars and `config.toml` (existing machinery — no `load_settings` change needed).

- [ ] **Step 1: Write the failing test** — append to `tests/test_config.py`:

```python
def test_plaid_settings_default_and_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    s = load_settings()
    assert s.plaid_client_id is None
    assert s.plaid_secret is None
    assert s.plaid_env == "production"
    save_config_value("plaid_client_id", "cid")
    save_config_value("plaid_secret", "sec")
    save_config_value("plaid_env", "sandbox")
    s = load_settings()
    assert (s.plaid_client_id, s.plaid_secret, s.plaid_env) == ("cid", "sec", "sandbox")
    monkeypatch.setenv("MONETA_PLAID_ENV", "production")
    assert load_settings().plaid_env == "production"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `Settings` has no attribute `plaid_client_id`.

- [ ] **Step 3: Implement** — add to `Settings` in `src/moneta/config.py`:

```python
    plaid_client_id: str | None = None
    plaid_secret: str | None = None
    plaid_env: str = "production"
```

- [ ] **Step 4: Run full verification** — all four commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/config.py tests/test_config.py
git commit -m "feat: plaid credentials/env settings"
```

---

### Task 4: `PlaidClient`, `PlaidError`, item store

**Files:**
- Create: `src/moneta/aggregator/plaid.py`
- Test: `tests/test_plaid.py` (append)

**Interfaces:**
- Produces:
  - `PlaidError(Exception)` with attrs `error_type: str`, `error_code: str`.
  - `PlaidClient(client_id: str, secret: str, env: str = "production", client: httpx.AsyncClient | None = None)`; raises `ValueError` for unknown env; `async post(path: str, payload: dict[str, Any]) -> dict[str, Any]` injects credentials into the JSON body, raises `PlaidError` on HTTP ≥ 400.
  - `PlaidItem(BaseModel)`: `item_id: str`, `access_token: str`, `institution_name: str = ""`, `products: list[str] = ["transactions"]`.
  - `items_path(config_dir: Path) -> Path` → `<config_dir>/plaid_items.json`.
  - `load_items(path: Path) -> list[PlaidItem]` (missing file → `[]`), `save_items(path: Path, items: list[PlaidItem]) -> None` (writes 0600).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_plaid.py`:

```python
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from moneta.aggregator.plaid import (
    PlaidClient,
    PlaidError,
    PlaidItem,
    items_path,
    load_items,
    save_items,
)


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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plaid.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'moneta.aggregator.plaid'`.

- [ ] **Step 3: Implement** — create `src/moneta/aggregator/plaid.py`:

```python
"""Plaid adapter. API docs: https://plaid.com/docs/api/

Two moneta-specific inversions (see the design spec §2):
- Plaid amounts are positive when money leaves the account; moneta stores
  negative = outflow, so every amount is negated.
- Plaid liability balances (credit/loan) are positive amounts owed; moneta
  stores owed balances negative (SimpleFIN convention).
"""

import json
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
```

- [ ] **Step 4: Run full verification** — all four commands pass. (Note: ruff `B` may flag the mutable default on `products` — pydantic fields are exempt via ruff's pydantic detection; if it fires anyway, use `Field(default_factory=lambda: ["transactions"])`.)

- [ ] **Step 5: Commit**

```bash
git add src/moneta/aggregator/plaid.py tests/test_plaid.py
git commit -m "feat: Plaid HTTP client, error type, and item store"
```

---

### Task 5: Hosted Link helpers

**Files:**
- Modify: `src/moneta/aggregator/plaid.py`
- Test: `tests/test_plaid.py` (append)

**Interfaces:**
- Consumes: `PlaidClient.post`.
- Produces:
  - `async create_hosted_link(client: PlaidClient, products: list[str], days_requested: int = 730) -> tuple[str, str]` — `(link_token, hosted_link_url)`.
  - `async poll_link_result(client: PlaidClient, link_token: str, timeout: float = 900.0, interval: float = 3.0) -> tuple[str, str]` — `(public_token, institution_name)`; raises `TimeoutError`.
  - `async exchange_public_token(client: PlaidClient, public_token: str) -> tuple[str, str]` — `(access_token, item_id)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_plaid.py`:

```python
from moneta.aggregator.plaid import (
    create_hosted_link,
    exchange_public_token,
    poll_link_result,
)

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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plaid.py -q`
Expected: FAIL — `ImportError: cannot import name 'create_hosted_link'`.

- [ ] **Step 3: Implement** — append to `src/moneta/aggregator/plaid.py` (add `import asyncio`, `import time` at top):

```python
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
            raise TimeoutError(
                "Plaid Link not completed in time — re-run: moneta setup plaid-link"
            )
        await asyncio.sleep(interval)


async def exchange_public_token(client: PlaidClient, public_token: str) -> tuple[str, str]:
    data = await client.post("/item/public_token/exchange", {"public_token": public_token})
    return data["access_token"], data["item_id"]
```

- [ ] **Step 4: Run full verification** — all four commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/aggregator/plaid.py tests/test_plaid.py
git commit -m "feat: Plaid hosted-link create/poll/exchange helpers"
```

---

### Task 6: `PlaidAdapter` — accounts

**Files:**
- Modify: `src/moneta/aggregator/plaid.py`
- Test: `tests/test_plaid.py` (append)

**Interfaces:**
- Consumes: `PlaidClient`, `PlaidItem`, DTOs from `aggregator/base.py`.
- Produces: `PlaidAdapter(client: PlaidClient, items: list[PlaidItem])` with `async fetch(since: date | None = None) -> Snapshot`. Accounts fetched for every item; transactions/holdings gated per item products (Tasks 7–8).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_plaid.py`:

```python
from moneta.aggregator.plaid import PlaidAdapter
from moneta.models import AccountType

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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plaid.py -q`
Expected: FAIL — `ImportError: cannot import name 'PlaidAdapter'`.

- [ ] **Step 3: Implement** — append to `src/moneta/aggregator/plaid.py` (extend top imports: `from datetime import date, datetime`, `from decimal import Decimal`, `from loguru import logger`, `from moneta.aggregator.base import AccountDTO, HoldingDTO, Snapshot, TransactionDTO`, `from moneta.models import AccountType`):

```python
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
```

- [ ] **Step 4: Run full verification** — all four commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/aggregator/plaid.py tests/test_plaid.py
git commit -m "feat: PlaidAdapter account fetch with type mapping and sign normalization"
```

---

### Task 7: `PlaidAdapter` — transactions sync

**Files:**
- Modify: `src/moneta/aggregator/plaid.py`
- Test: `tests/test_plaid.py` (append)

**Interfaces:**
- Consumes: Task 6's `PlaidAdapter._fetch_item`.
- Produces: items whose `products` include `"transactions"` get a `/transactions/sync` pagination loop (empty-cursor full replay, `count=500`), pending rows skipped, amounts negated, `description` = Plaid `name`, full txn dict in `raw`; bounded restart (3 attempts) on `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`; `NOT_READY` status logged, not fatal.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_plaid.py`:

```python
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
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)
        assert request.url.path == "/transactions/sync"
        return sync_responses.pop(0)

    return handle


async def test_fetch_transactions_paginates_negates_and_skips_pending() -> None:
    pages = [
        httpx.Response(
            200, json=_sync_page([_txn("t1", 12.5), _txn("t2", 3.0, pending=True)], True)
        ),
        httpx.Response(200, json=_sync_page([_txn("t3", -1000.0)], False)),
    ]
    cursors: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json=_ACCOUNTS_PAYLOAD)
        body = json.loads(request.content)
        cursors.append(body["cursor"])
        assert body["count"] == 500
        return pages.pop(0)

    adapter = PlaidAdapter(_plaid_client(handle), [_item(["transactions"])])
    snap = await adapter.fetch()
    assert [t.id for t in snap.transactions] == ["t1", "t3"]
    # Plaid positive = outflow -> moneta negative; deposits flip positive
    assert snap.transactions[0].amount == Decimal("-12.5")
    assert snap.transactions[1].amount == Decimal("1000")
    assert snap.transactions[0].description == "RAW DESCRIPTOR t1"
    assert snap.transactions[0].posted_on == date(2026, 7, 1)
    assert snap.transactions[0].raw["merchant_name"] == "Clean Name"
    assert cursors == ["", "cur-next"]


async def test_mutation_during_pagination_restarts_cleanly() -> None:
    mutation_error = httpx.Response(
        400,
        json={
            "error_type": "TRANSACTIONS_ERROR",
            "error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
            "error_message": "restart pagination",
        },
    )
    pages = [
        httpx.Response(200, json=_sync_page([_txn("t1", 1.0)], True)),
        mutation_error,
        httpx.Response(200, json=_sync_page([_txn("t1", 1.0)], True)),
        httpx.Response(200, json=_sync_page([_txn("t2", 2.0)], False)),
    ]
    adapter = PlaidAdapter(
        _plaid_client(_accounts_then_sync(pages)), [_item(["transactions"])]
    )
    snap = await adapter.fetch()
    # restart discarded the partial first attempt: no duplicate t1
    assert [t.id for t in snap.transactions] == ["t1", "t2"]


async def test_mutation_forever_raises_after_bounded_retries() -> None:
    def mutation() -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_type": "TRANSACTIONS_ERROR",
                "error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
                "error_message": "restart pagination",
            },
        )

    pages = [mutation(), mutation(), mutation()]
    adapter = PlaidAdapter(
        _plaid_client(_accounts_then_sync(pages)), [_item(["transactions"])]
    )
    with pytest.raises(PlaidError):
        await adapter.fetch()
    assert pages == []


async def test_not_ready_status_is_not_fatal() -> None:
    pages = [httpx.Response(200, json=_sync_page([], False, status="NOT_READY"))]
    adapter = PlaidAdapter(
        _plaid_client(_accounts_then_sync(pages)), [_item(["transactions"])]
    )
    snap = await adapter.fetch()
    assert snap.transactions == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plaid.py -q`
Expected: the new tests FAIL — transactions come back empty (`products` gate exists but no sync implementation yet), and the pagination assertions never see `/transactions/sync` requests.

- [ ] **Step 3: Implement** — in `src/moneta/aggregator/plaid.py`, add constants near the type map and extend the adapter:

```python
_SYNC_PAGE_SIZE = 500
_MUTATION_RETRIES = 3
```

Append to `PlaidAdapter._fetch_item`:

```python
        if "transactions" in item.products:
            snap.transactions.extend(await self._fetch_transactions(item))
```

Add methods:

```python
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
```

- [ ] **Step 4: Run full verification** — all four commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/aggregator/plaid.py tests/test_plaid.py
git commit -m "feat: PlaidAdapter transaction sync with full replay and mutation restart"
```

---

### Task 8: `PlaidAdapter` — holdings + per-item auth degradation

**Files:**
- Modify: `src/moneta/aggregator/plaid.py`
- Test: `tests/test_plaid.py` (append)

**Interfaces:**
- Consumes: Tasks 6–7.
- Produces: items with `"investments"` in `products` fetch `/investments/holdings/get` (symbol from securities lookup, `institution_value` as market value); `PRODUCTS_NOT_SUPPORTED` / `NO_INVESTMENT_ACCOUNTS` degrade to empty holdings; an item failing anywhere with `ITEM_LOGIN_REQUIRED` is skipped with a warning while other items still sync.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_plaid.py`:

```python
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
        return httpx.Response(
            400,
            json={
                "error_type": "ITEM_ERROR",
                "error_code": "NO_INVESTMENT_ACCOUNTS",
                "error_message": "none",
            },
        )

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


async def test_other_plaid_errors_propagate() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error_type": "INVALID_INPUT",
                "error_code": "INVALID_API_KEYS",
                "error_message": "bad keys",
            },
        )

    with pytest.raises(PlaidError):
        await PlaidAdapter(_plaid_client(handle), [_item()]).fetch()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_plaid.py -q`
Expected: holdings tests FAIL (empty holdings — no `/investments/holdings/get` call); `test_item_login_required_skips_item_but_not_sync` FAILs with a raised `PlaidError`.

- [ ] **Step 3: Implement** — in `PlaidAdapter`, wrap the per-item call in `fetch` and add holdings:

```python
    async def fetch(self, since: date | None = None) -> Snapshot:
        # `since` is deliberately ignored: /transactions/sync replays from an empty
        # cursor every run (history capped at 730 days by the link token), and
        # ingest dedup absorbs the overlap. See design spec §3.
        snap = Snapshot(accounts=[], transactions=[], holdings=[])
        for item in self._items:
            try:
                await self._fetch_item(item, snap)
            except PlaidError as exc:
                if exc.error_code != "ITEM_LOGIN_REQUIRED":
                    raise
                logger.warning(
                    "Plaid item {} needs re-linking (run: moneta setup plaid-link): {}",
                    item.institution_name or item.item_id,
                    exc,
                )
        return snap
```

Append to `_fetch_item`:

```python
        if "investments" in item.products:
            snap.holdings.extend(await self._fetch_holdings(item))
```

Add method:

```python
    async def _fetch_holdings(self, item: PlaidItem) -> list[HoldingDTO]:
        try:
            data = await self._client.post(
                "/investments/holdings/get", {"access_token": item.access_token}
            )
        except PlaidError as exc:
            if exc.error_code in ("PRODUCTS_NOT_SUPPORTED", "NO_INVESTMENT_ACCOUNTS"):
                logger.warning(
                    "Plaid item {}: no investment data ({})",
                    item.institution_name or item.item_id,
                    exc.error_code,
                )
                return []
            raise
        securities = {s["security_id"]: s for s in data.get("securities", [])}
        return [
            HoldingDTO(
                account_id=h["account_id"],
                symbol=(
                    (securities.get(h.get("security_id"), {})).get("ticker_symbol")
                    or (securities.get(h.get("security_id"), {})).get("name")
                    or "?"
                ),
                quantity=float(h.get("quantity", 0)),
                market_value=_to_decimal(h.get("institution_value") or 0),
            )
            for h in data.get("holdings", [])
        ]
```

(If the double lookup reads poorly, extract `sec = securities.get(...)` in a helper or loop — implementer's choice, behavior as tested.)

- [ ] **Step 4: Run full verification** — all four commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/aggregator/plaid.py tests/test_plaid.py
git commit -m "feat: PlaidAdapter holdings fetch and per-item auth degradation"
```

---

### Task 9: Server wiring — `_build_adapter`

**Files:**
- Modify: `src/moneta/api.py` (imports, `/sync` 400 detail, `build_app`)
- Test: `tests/test_api.py` (append), `tests/test_cli.py` (update one assertion)

**Interfaces:**
- Consumes: `SimpleFINAdapter`, `PlaidAdapter`, `PlaidClient`, `MergedAdapter`, `load_items`, `items_path`, `Settings`.
- Produces: `_build_adapter(settings: Settings) -> AggregatorAdapter | None` in `api.py`; `build_app` uses it; `/sync` with no adapter returns 400 detail `"No aggregator configured. Connect one with: moneta setup simplefin <token> or moneta setup plaid <client_id> <secret>"`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api.py` (check its existing imports; add what's missing):

```python
from pathlib import Path

from moneta.aggregator.base import MergedAdapter
from moneta.aggregator.plaid import PlaidAdapter, PlaidItem, items_path, save_items
from moneta.aggregator.simplefin import SimpleFINAdapter
from moneta.api import _build_adapter
from moneta.config import Settings


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(config_dir=tmp_path, db_path=tmp_path / "m.db", **kwargs)  # type: ignore[arg-type]


def test_build_adapter_none_when_nothing_configured(tmp_path: Path) -> None:
    # conftest's autouse _clean_moneta_env fixture already strips MONETA_* env vars
    assert _build_adapter(_settings(tmp_path)) is None


def test_build_adapter_simplefin_only(tmp_path: Path) -> None:
    adapter = _build_adapter(
        _settings(tmp_path, simplefin_access_url="https://u:p@bridge.example/simplefin")
    )
    assert isinstance(adapter, SimpleFINAdapter)


def test_build_adapter_plaid_requires_items(tmp_path: Path) -> None:
    s = _settings(tmp_path, plaid_client_id="cid", plaid_secret="sec", plaid_env="sandbox")
    assert _build_adapter(s) is None  # creds but no linked items
    save_items(items_path(tmp_path), [PlaidItem(item_id="it-1", access_token="a")])
    assert isinstance(_build_adapter(s), PlaidAdapter)


def test_build_adapter_merges_simplefin_and_plaid(tmp_path: Path) -> None:
    save_items(items_path(tmp_path), [PlaidItem(item_id="it-1", access_token="a")])
    adapter = _build_adapter(
        _settings(
            tmp_path,
            simplefin_access_url="https://u:p@bridge.example/simplefin",
            plaid_client_id="cid",
            plaid_secret="sec",
        )
    )
    assert isinstance(adapter, MergedAdapter)
```

Also update the existing CLI assertion in `tests/test_cli.py::test_sync_without_setup_fails_cleanly` — the error copy changes:

```python
def test_sync_without_setup_fails_cleanly(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "simplefin" in result.output
    assert "plaid" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_api.py tests/test_cli.py -q`
Expected: FAIL — `ImportError: cannot import name '_build_adapter'`.

- [ ] **Step 3: Implement** — in `src/moneta/api.py`, add imports (`MergedAdapter`, `PlaidAdapter`, `PlaidClient`, `items_path`, `load_items`, `Settings`) and:

```python
def _build_adapter(settings: Settings) -> AggregatorAdapter | None:
    adapters: list[AggregatorAdapter] = []
    if settings.simplefin_access_url:
        adapters.append(SimpleFINAdapter(settings.simplefin_access_url))
    if settings.plaid_client_id and settings.plaid_secret:
        items = load_items(items_path(settings.config_dir))
        if items:
            adapters.append(
                PlaidAdapter(
                    PlaidClient(
                        settings.plaid_client_id, settings.plaid_secret, settings.plaid_env
                    ),
                    items,
                )
            )
    if not adapters:
        return None
    return adapters[0] if len(adapters) == 1 else MergedAdapter(adapters)
```

`build_app` replaces its inline adapter expression with `adapter = _build_adapter(settings)`. The `/sync` guard's detail becomes:

```python
                detail=(
                    "No aggregator configured. Connect one with: "
                    "moneta setup simplefin <token> or moneta setup plaid <client_id> <secret>"
                ),
```

- [ ] **Step 4: Run full verification** — all four commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/api.py tests/test_api.py tests/test_cli.py
git commit -m "feat: wire Plaid (and SimpleFIN+Plaid merge) into app adapter construction"
```

---

### Task 10: CLI — `setup plaid`, `plaid-link`, `plaid-list`, `plaid-unlink`

**Files:**
- Modify: `src/moneta/cli/main.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `save_config_value`, `load_settings`, everything exported by `aggregator/plaid.py`.
- Produces: four `setup` subcommands (flat, matching `setup simplefin`); lazy imports inside command bodies (house style — keeps CLI startup light and lets tests monkeypatch `moneta.aggregator.plaid.*`).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py`:

```python
def test_setup_plaid_saves_credentials(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["setup", "plaid", "cid", "sec", "--env", "sandbox"])
    assert result.exit_code == 0
    assert "plaid-link" in result.output
    from moneta.config import load_settings

    s = load_settings()
    assert (s.plaid_client_id, s.plaid_secret, s.plaid_env) == ("cid", "sec", "sandbox")


def test_setup_plaid_rejects_bad_env(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["setup", "plaid", "cid", "sec", "--env", "development"])
    assert result.exit_code == 1
    assert "production" in result.output


def test_setup_plaid_link_requires_credentials(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    result = runner.invoke(app, ["setup", "plaid-link"])
    assert result.exit_code == 1
    assert "moneta setup plaid" in result.output


def test_setup_plaid_link_happy_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    runner.invoke(app, ["setup", "plaid", "cid", "sec", "--env", "sandbox"])

    import moneta.aggregator.plaid as plaid_mod

    async def fake_create(client: Any, products: list[str], days_requested: int = 730) -> Any:
        return "lt-1", "https://hosted.plaid.com/link/abc"

    async def fake_poll(
        client: Any, link_token: str, timeout: float = 900.0, interval: float = 3.0
    ) -> Any:
        assert link_token == "lt-1"
        return "public-1", "Chase"

    async def fake_exchange(client: Any, public_token: str) -> Any:
        assert public_token == "public-1"
        return "access-1", "it-1"

    monkeypatch.setattr(plaid_mod, "create_hosted_link", fake_create)
    monkeypatch.setattr(plaid_mod, "poll_link_result", fake_poll)
    monkeypatch.setattr(plaid_mod, "exchange_public_token", fake_exchange)

    result = runner.invoke(app, ["setup", "plaid-link"])
    assert result.exit_code == 0
    assert "hosted.plaid.com" in result.output
    assert "Linked Chase" in result.output

    items = plaid_mod.load_items(plaid_mod.items_path(tmp_path))
    assert len(items) == 1
    assert items[0].item_id == "it-1"
    assert items[0].access_token == "access-1"
    assert items[0].products == ["transactions"]


def test_setup_plaid_list_and_unlink(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _isolate(monkeypatch, tmp_path)
    runner.invoke(app, ["setup", "plaid", "cid", "sec", "--env", "sandbox"])

    import moneta.aggregator.plaid as plaid_mod

    plaid_mod.save_items(
        plaid_mod.items_path(tmp_path),
        [plaid_mod.PlaidItem(item_id="it-1", access_token="a", institution_name="Chase")],
    )

    result = runner.invoke(app, ["setup", "plaid-list"])
    assert result.exit_code == 0
    assert "Chase" in result.output

    removed: list[str] = []

    async def fake_post(self: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        removed.append(path)
        return {"request_id": "r"}

    monkeypatch.setattr(plaid_mod.PlaidClient, "post", fake_post)
    result = runner.invoke(app, ["setup", "plaid-unlink", "it-1"])
    assert result.exit_code == 0
    assert removed == ["/item/remove"]
    assert plaid_mod.load_items(plaid_mod.items_path(tmp_path)) == []

    result = runner.invoke(app, ["setup", "plaid-unlink", "nope"])
    assert result.exit_code == 1
    assert "plaid-list" in result.output
```

(`Any` is already imported in `tests/test_cli.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -q`
Expected: FAIL — `No such command 'plaid'` (typer exit code 2).

- [ ] **Step 3: Implement** — append to `src/moneta/cli/main.py` (near `setup_simplefin`; note the link flow calls helpers via the module so test monkeypatching works):

```python
def _plaid_client() -> tuple["PlaidClient", "Settings"]:  # noqa: F821 — see local imports
    from moneta.aggregator.plaid import PlaidClient
    from moneta.config import load_settings

    settings = load_settings()
    if not (settings.plaid_client_id and settings.plaid_secret):
        console.print(
            "[red]Error:[/red] Plaid credentials not set. "
            "Run: moneta setup plaid <client_id> <secret>"
        )
        raise typer.Exit(1)
    return (
        PlaidClient(settings.plaid_client_id, settings.plaid_secret, settings.plaid_env),
        settings,
    )


@setup_app.command("plaid")
def setup_plaid(
    client_id: str,
    secret: str,
    env: Annotated[str, typer.Option("--env", help="production or sandbox")] = "production",
) -> None:
    """Save Plaid API credentials (get them at https://dashboard.plaid.com)."""
    from moneta.config import save_config_value

    if env not in ("production", "sandbox"):
        console.print(f"[red]Error:[/red] --env must be production or sandbox, got {env!r}")
        raise typer.Exit(1)
    save_config_value("plaid_client_id", client_id)
    save_config_value("plaid_secret", secret)
    save_config_value("plaid_env", env)
    console.print("[green]Plaid credentials saved.[/green] Link a bank: moneta setup plaid-link")


@setup_app.command("plaid-link")
def setup_plaid_link(
    product: Annotated[
        list[str] | None,
        typer.Option("--product", help="Repeat to add products (default: transactions)."),
    ] = None,
) -> None:
    """Link a bank via Plaid Hosted Link: prints a URL, waits for you to finish."""
    import asyncio

    from moneta.aggregator import plaid

    client, settings = _plaid_client()
    products = product or ["transactions"]

    async def _link() -> str:
        link_token, url = await plaid.create_hosted_link(client, products)
        console.print(f"Open this link in your browser to connect your bank:\n[bold]{url}[/bold]")
        console.print("Waiting for you to finish (Ctrl-C aborts)…")
        public_token, institution = await plaid.poll_link_result(client, link_token)
        access_token, item_id = await plaid.exchange_public_token(client, public_token)
        path = plaid.items_path(settings.config_dir)
        items = plaid.load_items(path)
        items.append(
            plaid.PlaidItem(
                item_id=item_id,
                access_token=access_token,
                institution_name=institution,
                products=products,
            )
        )
        plaid.save_items(path, items)
        return institution or item_id

    name = asyncio.run(_link())
    console.print(f"[green]Linked {name}.[/green] Run: moneta sync")


@setup_app.command("plaid-list")
def setup_plaid_list() -> None:
    """List linked Plaid institutions."""
    from moneta.aggregator.plaid import items_path, load_items
    from moneta.config import load_settings

    items = load_items(items_path(load_settings().config_dir))
    if not items:
        console.print("No Plaid items linked. Run: moneta setup plaid-link")
        return
    table = Table("Institution", "Item ID", "Products")
    for it in items:
        table.add_row(it.institution_name or "?", it.item_id, ", ".join(it.products))
    console.print(table)


@setup_app.command("plaid-unlink")
def setup_plaid_unlink(item_id: str) -> None:
    """Unlink a Plaid item (stops Plaid billing for it); synced data stays in the db."""
    import asyncio

    from moneta.aggregator.plaid import items_path, load_items, save_items

    client, settings = _plaid_client()
    path = items_path(settings.config_dir)
    items = load_items(path)
    match = next((it for it in items if it.item_id == item_id), None)
    if match is None:
        console.print(
            f"[red]Error:[/red] no linked item {item_id!r} (see: moneta setup plaid-list)"
        )
        raise typer.Exit(1)
    asyncio.run(client.post("/item/remove", {"access_token": match.access_token}))
    save_items(path, [it for it in items if it.item_id != item_id])
    console.print(f"[green]Unlinked {match.institution_name or item_id}.[/green]")
```

Type-checking note: `_plaid_client`'s return annotation needs real imports under
`TYPE_CHECKING` at the top of `cli/main.py`:

```python
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from moneta.aggregator.plaid import PlaidClient
    from moneta.config import Settings
```

(then drop the `# noqa` comment — it was only a placeholder while reading this plan top-to-bottom).

- [ ] **Step 4: Run full verification** — all four commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/moneta/cli/main.py tests/test_cli.py
git commit -m "feat: plaid setup/link/list/unlink CLI commands"
```

---

### Task 11: Docs + backlog tickets

**Files:**
- Modify: `README.md`, `CLAUDE.md`
- Create: `docs/backlog/low/plaid-cursor-incremental-sync.md`, `docs/backlog/medium/surface-per-item-sync-warnings.md`, `docs/backlog/low/plaid-link-update-mode.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: README** — in the Quickstart section, after the simplefin line's block, document the Plaid alternative. Replace the quickstart code block with:

```bash
uv sync
uv run moneta setup simplefin <SETUP_TOKEN>   # get one at https://beta-bridge.simplefin.org
uv run moneta sync
uv run moneta power
```

and add right below the block:

```markdown
Prefer Plaid (or need an institution SimpleFIN lacks)? Both can be configured at
once — one `moneta sync` pulls from every configured source:

```bash
uv run moneta setup plaid <CLIENT_ID> <SECRET>   # keys from https://dashboard.plaid.com
uv run moneta setup plaid-link                   # prints a URL; finish linking in the browser
uv run moneta sync
```

`plaid-link` links one institution per run (re-run it for each bank);
`moneta setup plaid-list` / `plaid-unlink <item-id>` manage linked banks.
Plaid pulls up to 730 days of history on every sync, so no `--full` needed
for Plaid-only changes.
```

In the Configuration section, extend the env var list with:
`MONETA_PLAID_CLIENT_ID`, `MONETA_PLAID_SECRET`, `MONETA_PLAID_ENV`
(`production` (default) or `sandbox`). Mention linked banks live in
`~/.config/moneta/plaid_items.json`.

- [ ] **Step 2: CLAUDE.md** — add to the "Conventions that aren't obvious from the code" section, after the sign-convention bullet:

```markdown
- **Plaid inverts both signs** (aggregator/plaid.py): Plaid amounts are positive when money
  leaves the account and liability balances are positive-owed; the adapter negates both so
  stored data follows the SimpleFIN convention. `PlaidAdapter.fetch` ignores `since` — it
  replays `/transactions/sync` from an empty cursor every run (≤730 days; ingest dedup
  absorbs the overlap), so `sync --full` is a no-op for Plaid.
```

And in Layout, update the aggregator line to:
```markdown
- `aggregator/` — adapter protocol + SimpleFIN + Plaid (+ `MergedAdapter` when several are configured); DTOs stop at `pipelines/ingest.py`
```

- [ ] **Step 3: Backlog tickets** — create the three files, each with summary/context/acceptance criteria:

`docs/backlog/low/plaid-cursor-incremental-sync.md`:
```markdown
# Plaid: cursor-based incremental /transactions/sync

## Summary
PlaidAdapter replays full history every sync. Persist per-item `next_cursor` and resume.

## Context
v1 chose stateless replay (design spec 2026-07-09 §3): dedup absorbs overlap and history
is bounded at 730 days. Cursors must only advance after ingest commits, which the
`fetch() -> Snapshot` protocol can't express today.

## Acceptance criteria
- Cursor stored per item, advanced only after `ingest_snapshot` commits.
- `moneta sync --full` resets cursors.
- Mutation-during-pagination restarts from the last committed cursor.
```

`docs/backlog/medium/surface-per-item-sync-warnings.md`:
```markdown
# Surface per-item sync warnings in the sync report

## Summary
ITEM_LOGIN_REQUIRED (Plaid) and SimpleFIN bridge errors only reach server logs; the CLI
user never learns an institution went stale.

## Context
`PlaidAdapter.fetch` skips dead items with `logger.warning`; SimpleFIN logs error strings.
`SyncReport` could carry a `warnings: list[str]` the CLI prints after `moneta sync`.

## Acceptance criteria
- `moneta sync` prints a yellow warning naming the stale institution and the fix
  (`moneta setup plaid-link` / SimpleFIN re-claim).
- No behavior change when all sources are healthy.
```

`docs/backlog/low/plaid-link-update-mode.md`:
```markdown
# Plaid Link update mode for ITEM_LOGIN_REQUIRED

## Summary
Repair a broken item in place instead of unlink + re-link (which creates duplicate
accounts because Plaid account_ids are per-item).

## Context
Hosted Link supports update mode: create a link token with `access_token` set, complete
re-auth in the browser, keep the same item/account ids.

## Acceptance criteria
- `moneta setup plaid-relink <item-id>` runs hosted-link update mode for the item.
- Synced accounts keep their rows (no duplicates).
```

- [ ] **Step 4: Run full verification** — all four commands pass (docs don't affect them, but keep the habit).

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md docs/backlog
git commit -m "docs: Plaid setup/quickstart, conventions, and backlog tickets"
```

---

## Post-implementation pipeline (per global conventions)

1. `feature-dev:code-architect` review — fix every finding.
2. `/simplify` — apply every finding.
3. `claude-md-management:claude-md-improver` audit.
4. QA-backlog subagent → `docs/qa-backlog/` items for the real-Plaid flows (hosted link against sandbox, real sync, unlink billing stop).
5. Final full verification, PR to `main`.
