"""SQLAlchemy models. Money = integer cents; negative = outflow."""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def to_cents(d: Decimal) -> int:
    return int((d * 100).to_integral_value())


def dollars(cents: int) -> str:
    """Unsigned dollars for prose only (LLM prompts, review questions) — API fields carry cents."""
    return f"{abs(cents) / 100:.2f}"


class AccountType(StrEnum):
    checking = "checking"
    savings = "savings"
    credit = "credit"
    brokerage = "brokerage"
    loan = "loan"
    unknown = "unknown"


LIQUID_ACCOUNT_TYPES: tuple[AccountType, ...] = (AccountType.checking, AccountType.savings)
SPEND_ACCOUNT_TYPES: tuple[AccountType, ...] = (
    AccountType.checking,
    AccountType.savings,
    AccountType.credit,
)
LIABILITY_ACCOUNT_TYPES: tuple[AccountType, ...] = (AccountType.credit, AccountType.loan)


class Direction(StrEnum):
    inflow = "inflow"
    outflow = "outflow"


def series_key(merchant: object, direction: object) -> tuple[str, str] | None:
    """Normalized (merchant, direction) identity for recurring-series bookkeeping.

    Review-item payloads round-trip through JSON; this is the one place that
    decides whether such a payload addresses a series. Returns None for junk.
    """
    if not isinstance(merchant, str) or not isinstance(direction, str):
        return None
    try:
        return merchant, str(Direction(direction))
    except ValueError:
        return None


BILL_TOLERANCE = 0.20
# (merchant, direction value, unsigned amount) — amount None = merchant-level
BillRef = tuple[str, str, int | None]


def amounts_match(a: int, b: int) -> bool:
    """Two unsigned amounts name the same bill: within 20% of the larger."""
    return abs(a - b) <= max(a, b) * BILL_TOLERANCE


def bill_ref(payload: Mapping[str, object]) -> BillRef | None:
    """The bill a recurring_cluster payload addresses (design 2026-10-13 §4)."""
    key = series_key(payload.get("merchant"), payload.get("direction"))
    if key is None:
        return None
    amount = payload.get("amount_cents")
    return key[0], key[1], amount if type(amount) is int and amount > 0 else None


def same_bill(a: BillRef, b: BillRef) -> bool:
    """A merchant-level ref (amount None) covers every bill of its merchant+direction."""
    if a[:2] != b[:2]:
        return False
    if a[2] is None or b[2] is None:
        return True
    return amounts_match(a[2], b[2])


def decision_precedence(ref: BillRef, item_id: int) -> tuple[bool, int]:
    """Sort key for decisions covering one bill: amount-specific beats merchant-level,
    then the highest item id (design 2026-10-13 §4). The greatest key wins."""
    return ref[2] is not None, item_id


def recurring_cluster_item(
    merchant: str, direction: str, amount_cents: int | None = None
) -> "ReviewItem":
    """The one recurring_cluster ReviewItem construction — question text and payload
    shape are pinned by tests. Used by detection, verification, and the API ledger.
    An amount is named only when the merchant has several bills (design 2026-10-13 §4)."""
    payload: dict[str, Any] = {"merchant": merchant, "direction": direction}
    question = f"Is {merchant!r} a recurring bill?"
    if amount_cents is not None:
        payload["amount_cents"] = amount_cents
        question = f"Is {merchant!r} (${dollars(amount_cents)}) a recurring bill?"
    return ReviewItem(kind=ReviewKind.recurring_cluster, question=question, payload=payload)


class Cadence(StrEnum):
    weekly = "weekly"
    biweekly = "biweekly"
    monthly = "monthly"
    annual = "annual"


class SeriesStatus(StrEnum):
    active = "active"
    ended = "ended"


class EventKind(StrEnum):
    new_series = "new_series"
    missed = "missed"
    price_increase = "price_increase"


class ReviewStatus(StrEnum):
    open = "open"
    resolved = "resolved"


class LinkMethod(StrEnum):
    rule = "rule"
    llm = "llm"
    manual = "manual"


class AliasSource(StrEnum):
    rule = "rule"
    llm = "llm"
    manual = "manual"


class ReviewKind(StrEnum):
    merchant = "merchant"
    transfer_pair = "transfer_pair"
    recurring_cluster = "recurring_cluster"
    price_change = "price_change"
    financing_account = "financing_account"
    account_overlap = "account_overlap"
    transaction_overlap = "transaction_overlap"


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    aggregator_id: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str]
    org_name: Mapped[str] = mapped_column(default="")
    type: Mapped[AccountType] = mapped_column(String, default=AccountType.unknown)
    currency: Mapped[str] = mapped_column(default="USD")
    balance_cents: Mapped[int] = mapped_column(default=0)
    balance_date: Mapped[date] = mapped_column(Date)
    promo_expires_on: Mapped[date | None] = mapped_column(Date, default=None)
    financing_mode: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(default="")
    mask: Mapped[str | None] = mapped_column(default=None)
    canonical_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), default=None
    )
    # the provider's latest account record as fetched; latest observation only
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Transaction(Base):
    __tablename__ = "transactions"
    # AUTOINCREMENT (migration 0009): ingest deletes rows a complete Plaid replay
    # dropped, and review payloads name rows by id, so an id must never come back
    __table_args__ = (
        UniqueConstraint("account_id", "aggregator_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    aggregator_id: Mapped[str]
    posted_on: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int]
    description: Mapped[str]
    merchant: Mapped[str | None] = mapped_column(default=None)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_series.id"), default=None)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), default=None)


class TransferLink(Base):
    __tablename__ = "transfer_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    outflow_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True)
    inflow_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True)
    confidence: Mapped[float] = mapped_column(Float)
    method: Mapped[LinkMethod] = mapped_column(String)


class RecurringSeries(Base):
    __tablename__ = "recurring_series"

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant: Mapped[str]
    direction: Mapped[Direction] = mapped_column(String)
    cadence: Mapped[Cadence] = mapped_column(String)
    expected_cents: Mapped[int]
    next_expected_on: Mapped[date] = mapped_column(Date)
    status: Mapped[SeriesStatus] = mapped_column(String, default=SeriesStatus.active)
    discretionary: Mapped[bool] = mapped_column(default=False)


def series_ref(series: RecurringSeries) -> BillRef:
    return series.merchant, str(series.direction), abs(series.expected_cents)


class SeriesEvent(Base):
    __tablename__ = "series_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("recurring_series.id"))
    kind: Mapped[EventKind] = mapped_column(String)
    occurred_on: Mapped[date] = mapped_column(Date)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("account_id", "symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str]
    quantity: Mapped[float] = mapped_column(Float)
    market_value_cents: Mapped[int]
    vested_quantity: Mapped[float | None] = mapped_column(Float, default=None)
    unvested_quantity: Mapped[float | None] = mapped_column(Float, default=None)


class MerchantAlias(Base):
    __tablename__ = "merchant_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_descriptor: Mapped[str] = mapped_column(unique=True)
    merchant: Mapped[str]
    source: Mapped[AliasSource] = mapped_column(String)


class SyncRun(Base):
    """Audit row per run_sync invocation — the answer to 'did last night's sync work?'."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # naive local time, matching the app's local-calendar-day convention; a server
    # default would be CURRENT_TIMESTAMP (UTC) and silently mix the two
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    success: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(default=None)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)


class DigestState(Base):
    """Single-row (id=1) cursor for the notifications digest (design 2026-07-16 §1):
    the newest `SeriesEvent.id` already sent, plus the set of financing account ids
    already warned about deferred-interest risk (a cleared risk drops out so a
    re-appearance re-notifies)."""

    __tablename__ = "digest_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    last_event_id: Mapped[int] = mapped_column(default=0)
    warned_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)


class ReviewItem(Base):
    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ReviewKind] = mapped_column(String)
    question: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[ReviewStatus] = mapped_column(String, default=ReviewStatus.open)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_on: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
