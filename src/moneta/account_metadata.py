"""Field-level user choices recovered from the existing durable review ledger."""

from dataclasses import dataclass, field
from datetime import date

from moneta.models import Account, ReviewItem, ReviewKind, ReviewStatus


@dataclass
class AccountDecisions:
    financing: dict[int, bool] = field(default_factory=dict)
    promotion: dict[int, date | None] = field(default_factory=dict)


def account_decisions(items: list[ReviewItem]) -> AccountDecisions:
    decisions = AccountDecisions()
    for item in sorted(items, key=lambda row: row.id):
        if item.kind != ReviewKind.financing_account or item.status != ReviewStatus.resolved:
            continue
        account_id = item.payload.get("effective_account_id", item.payload.get("account_id"))
        resolution = item.resolution or {}
        if type(account_id) is not int:
            continue
        financing = resolution.get("financing")
        if isinstance(financing, bool):
            decisions.financing[account_id] = financing
        if "promo_expires_on" in resolution:
            value = resolution["promo_expires_on"]
            if value is None:
                decisions.promotion[account_id] = None
            elif isinstance(value, str):
                decisions.promotion[account_id] = date.fromisoformat(value)
    return decisions


def effective_account_metadata(
    root: Account, group: list[Account], decisions: AccountDecisions
) -> tuple[bool, date | None]:
    """Root choices win; an uninformed default may inherit a sole alias choice.

    Historical nondefault values are evidence; historical defaults without a
    ledger entry are not evidence of an explicit rejection/clear.
    """
    financing = root.financing_mode
    promotion = root.promo_expires_on
    if root.id not in decisions.financing and not financing:
        values = {
            decisions.financing.get(a.id, a.financing_mode)
            for a in group
            if a.id in decisions.financing or a.financing_mode
        }
        if len(values) == 1:
            financing = next(iter(values))
    if root.id not in decisions.promotion and promotion is None:
        dates = {
            decisions.promotion.get(a.id, a.promo_expires_on)
            for a in group
            if a.id in decisions.promotion or a.promo_expires_on is not None
        }
        if len(dates) == 1:
            promotion = next(iter(dates))
    return financing, promotion


def open_financing_roots(
    items: list[ReviewItem], roots: dict[int, int], decisions: AccountDecisions
) -> set[int]:
    """Logical accounts with an open financing question that no one has ruled on.

    The LLM never answers financing questions, so until a human does, the account is
    treated as financing. A ruling recorded on any member of the logical account wins
    over a question still open on another member.
    """
    ruled = {roots[account_id] for account_id in decisions.financing if account_id in roots}
    pending: set[int] = set()
    for item in items:
        if item.kind != ReviewKind.financing_account or item.status != ReviewStatus.open:
            continue
        account_id = item.payload.get("account_id")
        if type(account_id) is int and account_id in roots:
            pending.add(roots[account_id])
    return pending - ruled
