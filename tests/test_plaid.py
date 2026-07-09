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
